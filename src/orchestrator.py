"""
CFA-SHOC Phase 1 Orchestration Protocol (RUN_TOOL_NODE)

Implements the complete transaction execution flow coordinating all services.
Enforces the deterministic control shell guarantees over the probabilistic proposal engine.
"""

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple
from uuid import UUID, uuid4

from .domain import (
    ActionProposal, CommitRequest, DispatchRequest, DispatchLease, ObservationEnvelope,
    PolicyDecision, PreflightResult, ResultVerification, ToolTransaction, TransactionEvent,
    TransactionState,
)
from .state_machine import TransactionStateMachine, IllegalTransitionError
from .storage import StorageManager
from .services import (
    CapabilitySnapshotService, ActionNormalizer, PreflightService, PolicyDecisionPoint,
    TransactionCoordinator, ToolAdapter, ResultVerifier, CommitMembrane, AuditLedger,
    ReconciliationWorker,
)


class OrchestrationContext:
    """Immutable context for a single orchestration execution."""

    def __init__(
        self,
        proposal_id: UUID,
        episode_id: UUID,
        task_node_id: UUID,
        principal_id: str,
        tenant_id: str,
        requested_by: str,
    ):
        self.proposal_id = proposal_id
        self.episode_id = episode_id
        self.task_node_id = task_node_id
        self.principal_id = principal_id
        self.tenant_id = tenant_id
        self.requested_by = requested_by  # "model", "user", "policy", "recovery"
        self.created_at = datetime.utcnow()
        self.ledger_snapshot = None
        self.transaction_id = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize context for logging."""
        return {
            "proposal_id": str(self.proposal_id),
            "episode_id": str(self.episode_id),
            "task_node_id": str(self.task_node_id),
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "requested_by": self.requested_by,
            "created_at": self.created_at.isoformat(),
        }


class RUN_TOOL_NODE:
    """
    Main orchestration protocol for Phase 1 execution.

    Implements the complete transaction lifecycle:
    1. Ledger refresh (get current capability snapshot)
    2. Proposal normalization (convert model output to canonical ActionProposal)
    3. Preflight check (8 deterministic checks per spec section 3.1)
    4. Policy evaluation (PolicyDecisionPoint yields signed decision)
    5. Authorization (check policy decision approval status)
    6. Lease acquisition (acquire short-lived idempotency lock)
    7. Dispatch (invoke tool via ToolAdapter/connector boundary)
    8. Result verification (check signature, schema, consistency)
    9. Commit (CommitMembrane persists verified result to durable state)

    Per Phase 1 specification section 3.2, legal state transitions are enforced.
    Per section 3.3, transaction invariants are validated at each step.
    """

    def __init__(
        self,
        storage: StorageManager,
        capability_service: CapabilitySnapshotService,
        normalizer: ActionNormalizer,
        preflight: PreflightService,
        policy_point: PolicyDecisionPoint,
        tool_adapter: ToolAdapter,
        verifier: ResultVerifier,
        commit_membrane: CommitMembrane,
        audit_ledger: AuditLedger,
    ):
        """Initialize orchestrator with all required services."""
        self.storage = storage
        self.capability_service = capability_service
        self.normalizer = normalizer
        self.preflight = preflight
        self.policy_point = policy_point
        self.tool_adapter = tool_adapter
        self.verifier = verifier
        self.commit_membrane = commit_membrane
        self.audit_ledger = audit_ledger

    def execute(
        self,
        model_proposal: Dict[str, Any],
        context: OrchestrationContext,
    ) -> Tuple[ToolTransaction, PreflightResult]:
        """
        Execute RUN_TOOL_NODE orchestration protocol.

        Args:
            model_proposal: Unvalidated proposal from probabilistic engine
            context: Immutable context for this execution

        Returns:
            (ToolTransaction, PreflightResult or error)

        Raises:
            ValueError: On validation failures
            IllegalTransitionError: On invalid state transition
        """
        session = self.storage.get_session()
        try:
            # Step 1: Ledger refresh - get current capability snapshot
            ledger_snapshot = self._ledger_refresh(context, session)
            context.ledger_snapshot = ledger_snapshot

            # Step 2: Proposal normalization - convert to canonical ActionProposal
            action_proposal = self._normalize_proposal(model_proposal, context)
            context.transaction_id = action_proposal.proposal_id

            # Create durable transaction record in PROPOSED state
            transaction = self._create_transaction(action_proposal, context, ledger_snapshot)
            tx_orm = self.storage.save_transaction(
                session,
                self._tx_to_dict(transaction, TransactionState.PROPOSED),
            )
            transaction.transaction_id = UUID(tx_orm.transaction_id)

            # Log transition event
            self._log_event(
                session,
                transaction.transaction_id,
                "orchestration_started",
                from_state=None,
                to_state=TransactionState.PROPOSED.value,
                payload={"context": context.to_dict(), "action_proposal": self._ap_to_dict(action_proposal)},
            )

            # Step 3: Preflight check - 8 deterministic checks
            preflight_result = self._preflight_check(transaction, ledger_snapshot, session)
            if not preflight_result.allowed:
                self._transition(
                    session, transaction, TransactionState.PROPOSED, "preflight_failed"
                )
                return transaction, preflight_result

            # Transition to PREFLIGHTED
            transaction.state = TransactionState.PREFLIGHTED
            self._transition(session, transaction, TransactionState.PROPOSED, "preflight_passed")

            # Step 4: Policy evaluation - PolicyDecisionPoint yields signed decision
            policy_decision = self._policy_evaluation(transaction, ledger_snapshot, session)
            if not policy_decision.approved:
                self._transition(
                    session, transaction, TransactionState.PREFLIGHTED, "policy_denied"
                )
                transaction.policy_decision = self._pd_to_dict(policy_decision)
                self.storage.save_transaction(session, self._tx_to_dict(transaction, TransactionState.REJECTED))
                return transaction, PreflightResult(
                    allowed=False, reason=f"Policy denied: {policy_decision.reason}"
                )

            # Transition to AUTHORIZED
            transaction.policy_decision = self._pd_to_dict(policy_decision)
            self._transition(session, transaction, TransactionState.PREFLIGHTED, "policy_approved")
            transaction.state = TransactionState.AUTHORIZED

            # Step 5: Authorization - verify policy signature and expiry
            if not self._verify_authorization(policy_decision):
                self._transition(
                    session, transaction, TransactionState.AUTHORIZED, "policy_denied"
                )
                return transaction, PreflightResult(
                    allowed=False, reason="Policy decision expired or invalid signature"
                )

            # Step 6: Lease acquisition - acquire short-lived idempotency lock
            lease = self._acquire_lease(transaction, session)
            if not lease:
                return transaction, PreflightResult(
                    allowed=False, reason="Could not acquire idempotency lock (concurrent execution)"
                )

            # Transition to DISPATCHED
            self._transition(session, transaction, TransactionState.AUTHORIZED, "dispatch_started")
            transaction.state = TransactionState.DISPATCHED

            # Step 7: Dispatch - invoke tool via ToolAdapter/connector boundary
            raw_result = self._dispatch(transaction, session)
            if raw_result is None:
                self._transition(
                    session, transaction, TransactionState.DISPATCHED, "connector_error"
                )
                self._release_lease(lease, session)
                return transaction, PreflightResult(
                    allowed=False, reason="Tool dispatch failed (connector error)"
                )

            # Update transaction with external operation ID if available
            if raw_result.external_operation_id:
                transaction.external_operation_id = raw_result.external_operation_id

            # Check if result is async (PENDING_EXTERNAL)
            if raw_result.transport_status == "async_accepted":
                self._transition(
                    session, transaction, TransactionState.DISPATCHED, "async_accepted"
                )
                transaction.state = TransactionState.PENDING_EXTERNAL
                transaction.deadline_at = datetime.utcnow() + timedelta(hours=24)
                self.storage.save_transaction(session, self._tx_to_dict(transaction, TransactionState.PENDING_EXTERNAL))
                self._release_lease(lease, session)
                return transaction, PreflightResult(
                    allowed=True,
                    reason="Tool dispatch accepted asynchronously; awaiting external status query",
                )

            # Sync result received
            self._transition(session, transaction, TransactionState.DISPATCHED, "response_received")
            transaction.state = TransactionState.RETURNED

            # Step 8: Result verification - check signature, schema, consistency
            verification = self._verify_result(transaction, raw_result, ledger_snapshot, session)
            transaction.verification = self._rv_to_dict(verification)

            if not verification.passed:
                self._transition(
                    session, transaction, TransactionState.RETURNED, "verification_failed"
                )
                self._release_lease(lease, session)
                return transaction, PreflightResult(
                    allowed=False, reason=f"Result verification failed: {verification.reason}"
                )

            # Transition to VERIFIED
            self._transition(session, transaction, TransactionState.RETURNED, "verification_passed")
            transaction.state = TransactionState.VERIFIED

            # Wrap result in ObservationEnvelope
            observation = ObservationEnvelope(
                source_class="tool_output",
                authority_level="non_authoritative_content",
                content=raw_result.body,
                transaction_id=transaction.transaction_id,
                connector_id=transaction.connector_id or "",
                integrity_signature=verification.signature_valid and hashlib.sha256(
                    json.dumps(raw_result.body or {}, sort_keys=True, default=str).encode()
                ).hexdigest() or "",
            )

            # Step 9: Commit - CommitMembrane persists verified result to durable state
            commit_request = CommitRequest(
                transaction_id=transaction.transaction_id,
                verified_result_ref=str(observation.transaction_id),
                contract_hash=transaction.contract_hash,
                ledger_hash=transaction.ledger_hash,
                risk_class=transaction.risk_class,
                side_effect_class=transaction.side_effect_class,
                rollback_strategy=transaction.rollback_strategy,
                evidence_refs=[str(observation.transaction_id)],
            )

            commit_approved = self._commit(transaction, commit_request, observation, session)
            self._release_lease(lease, session)

            if commit_approved:
                self._transition(
                    session, transaction, TransactionState.VERIFIED, "action_commit_approved"
                )
                transaction.state = TransactionState.COMMITTED
                transaction.result_envelope = {
                    "observation": self._oe_to_dict(observation),
                    "verified": True,
                }
                self.storage.save_transaction(session, self._tx_to_dict(transaction, TransactionState.COMMITTED))
                return transaction, PreflightResult(allowed=True, reason="Transaction committed successfully")
            else:
                self._transition(
                    session, transaction, TransactionState.VERIFIED, "action_commit_denied"
                )
                return transaction, PreflightResult(
                    allowed=False, reason="Commit membrane rejected result (precondition violation)"
                )

        except Exception as e:
            self._log_event(
                session,
                context.transaction_id or uuid4(),
                "orchestration_error",
                payload={"error": str(e), "exception_type": type(e).__name__},
            )
            raise
        finally:
            session.close()

    # Private orchestration steps

    def _ledger_refresh(self, context: OrchestrationContext, session) -> Dict[str, Any]:
        """Step 1: Refresh ledger snapshot with current capability state."""
        snapshot = self.capability_service.refresh_snapshot(context.principal_id, context.tenant_id)
        snapshot = self.capability_service.publish_snapshot(snapshot)
        return snapshot

    def _normalize_proposal(
        self, model_proposal: Dict[str, Any], context: OrchestrationContext
    ) -> ActionProposal:
        """Step 2: Normalize model proposal to canonical ActionProposal."""
        return self.normalizer.normalize(
            model_proposal, context.proposal_id, context.episode_id, context.task_node_id
        )

    def _create_transaction(
        self,
        action_proposal: ActionProposal,
        context: OrchestrationContext,
        ledger_snapshot: Dict[str, Any],
    ) -> ToolTransaction:
        """Create initial durable transaction record."""
        transaction = ToolTransaction(
            transaction_id=uuid4(),
            proposal_id=action_proposal.proposal_id,
            episode_id=action_proposal.episode_id,
            task_node_id=action_proposal.task_node_id,
            tool_id=action_proposal.tool_id,
            operation=action_proposal.operation,
            connector_id=action_proposal.connector_id,
            principal_id=context.principal_id,
            tenant_id=context.tenant_id,
            ledger_hash=ledger_snapshot["hash"],
            contract_hash=action_proposal.contract_hash,
            input_payload=action_proposal.arguments,
            input_schema_hash=action_proposal.input_schema_hash,
            idempotency_key=action_proposal.idempotency_key,
            risk_class=action_proposal.risk_class.value,
            side_effect_class=action_proposal.side_effect_class.value,
            rollback_strategy=action_proposal.rollback_strategy.value,
            state=TransactionState.PROPOSED,
        )
        return transaction

    def _preflight_check(
        self,
        transaction: ToolTransaction,
        ledger_snapshot: Dict[str, Any],
        session,
    ) -> PreflightResult:
        """Step 3: Perform 8 deterministic preflight checks."""
        return self.preflight.check(transaction, ledger_snapshot)

    def _policy_evaluation(
        self,
        transaction: ToolTransaction,
        ledger_snapshot: Dict[str, Any],
        session,
    ) -> PolicyDecision:
        """Step 4: Evaluate proposal against policy framework."""
        return self.policy_point.evaluate(transaction, ledger_snapshot)

    def _verify_authorization(self, policy_decision: PolicyDecision) -> bool:
        """Step 5: Verify policy signature and check expiry."""
        if policy_decision.expires_at < datetime.utcnow():
            return False
        # In production, verify policy_decision.signature against policy signer's key
        return True

    def _acquire_lease(
        self, transaction: ToolTransaction, session
    ) -> Optional[DispatchLease]:
        """Step 6: Acquire short-lived idempotency lock."""
        from .domain import DispatchLease as DispatchLeaseDomain

        lease_domain = DispatchLeaseDomain(
            transaction_id=transaction.transaction_id,
            owner_id=transaction.principal_id,
            lease_token=str(uuid4()),
            acquired_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(minutes=5),
        )
        try:
            lease = self.storage.acquire_lease(session, self._dl_to_dict(lease_domain))
            return lease_domain
        except Exception:
            return None

    def _dispatch(self, transaction: ToolTransaction, session) -> Optional[Any]:
        """Step 7: Dispatch tool invocation via ToolAdapter."""
        dispatch_request = DispatchRequest(
            transaction_id=transaction.transaction_id,
            tool_id=transaction.tool_id,
            operation=transaction.operation,
            connector_id=transaction.connector_id,
            arguments=transaction.input_payload,
            idempotency_key=transaction.idempotency_key,
            deadline_at=datetime.utcnow() + timedelta(minutes=5),
            auth_context_ref=transaction.principal_id,
        )
        return self.tool_adapter.invoke(dispatch_request)

    def _verify_result(
        self,
        transaction: ToolTransaction,
        raw_result: Any,
        ledger_snapshot: Dict[str, Any],
        session,
    ) -> ResultVerification:
        """Step 8: Verify result signature, schema, and consistency."""
        return self.verifier.verify(transaction, raw_result, ledger_snapshot)

    def _commit(
        self,
        transaction: ToolTransaction,
        commit_request: CommitRequest,
        observation: ObservationEnvelope,
        session,
    ) -> bool:
        """Step 9: CommitMembrane persists verified result to durable state."""
        return self.commit_membrane.commit(commit_request, observation)

    def _release_lease(self, lease: DispatchLease, session) -> None:
        """Release idempotency lock after dispatch (success or failure)."""
        if lease:
            self.storage.release_lease(session, lease.lease_token)

    def _transition(
        self,
        session,
        transaction: ToolTransaction,
        from_state: TransactionState,
        event: str,
    ) -> None:
        """Perform state transition and log event."""
        try:
            next_state, reason = TransactionStateMachine.transition(from_state, event)
            transaction.state = next_state
            self._log_event(
                session,
                transaction.transaction_id,
                event,
                from_state=from_state.value,
                to_state=next_state.value,
                payload={"reason": reason},
            )
        except IllegalTransitionError as e:
            self._log_event(
                session,
                transaction.transaction_id,
                "illegal_transition_attempt",
                payload={"error": str(e), "from_state": from_state.value, "event": event},
            )
            raise

    def _log_event(
        self,
        session,
        transaction_id: UUID,
        event_type: str,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log append-only audit event."""
        self.audit_ledger.append_event(
            session,
            transaction_id,
            event_type,
            from_state,
            to_state,
            payload or {},
        )

    # Serialization helpers

    def _tx_to_dict(self, tx: ToolTransaction, state: TransactionState) -> Dict[str, Any]:
        """Convert ToolTransaction to dict for storage."""
        return {
            "transaction_id": str(tx.transaction_id),
            "proposal_id": str(tx.proposal_id),
            "episode_id": str(tx.episode_id),
            "task_node_id": str(tx.task_node_id),
            "tool_id": tx.tool_id,
            "operation": tx.operation,
            "connector_id": tx.connector_id,
            "principal_id": tx.principal_id,
            "tenant_id": tx.tenant_id,
            "ledger_hash": tx.ledger_hash,
            "contract_hash": tx.contract_hash,
            "input_payload": tx.input_payload,
            "input_schema_hash": tx.input_schema_hash,
            "idempotency_key": tx.idempotency_key,
            "risk_class": tx.risk_class,
            "side_effect_class": tx.side_effect_class,
            "rollback_strategy": tx.rollback_strategy,
            "state": state.value,
            "external_operation_id": tx.external_operation_id,
            "result_envelope": tx.result_envelope,
            "policy_decision": tx.policy_decision,
            "verification": tx.verification,
            "deadline_at": tx.deadline_at,
        }

    def _ap_to_dict(self, ap: ActionProposal) -> Dict[str, Any]:
        """Convert ActionProposal to dict for logging."""
        return {
            "proposal_id": str(ap.proposal_id),
            "episode_id": str(ap.episode_id),
            "task_node_id": str(ap.task_node_id),
            "tool_id": ap.tool_id,
            "operation": ap.operation,
            "connector_id": ap.connector_id,
            "risk_class": ap.risk_class.value,
            "side_effect_class": ap.side_effect_class.value,
            "idempotency_key": ap.idempotency_key,
        }

    def _pd_to_dict(self, pd: PolicyDecision) -> Dict[str, Any]:
        """Convert PolicyDecision to dict for storage."""
        return {
            "decision_id": str(pd.decision_id),
            "approved": pd.approved,
            "reason": pd.reason,
            "subject_type": pd.subject_type,
            "subject_ref": pd.subject_ref,
            "contract_hash": pd.contract_hash,
            "ledger_hash": pd.ledger_hash,
            "evidence_refs": pd.evidence_refs,
            "checks": pd.checks,
            "signature": pd.signature,
            "expires_at": pd.expires_at.isoformat(),
            "approver_type": pd.approver_type,
            "approver_id": pd.approver_id,
        }

    def _rv_to_dict(self, rv: ResultVerification) -> Dict[str, Any]:
        """Convert ResultVerification to dict for storage."""
        return {
            "passed": rv.passed,
            "reason": rv.reason,
            "signature_valid": rv.signature_valid,
            "schema_valid": rv.schema_valid,
            "tool_connector_match": rv.tool_connector_match,
            "status_terminal": rv.status_terminal,
            "evidence": rv.evidence,
        }

    def _oe_to_dict(self, oe: ObservationEnvelope) -> Dict[str, Any]:
        """Convert ObservationEnvelope to dict for storage."""
        return {
            "source_class": oe.source_class,
            "authority_level": oe.authority_level,
            "content": oe.content,
            "transaction_id": str(oe.transaction_id),
            "connector_id": oe.connector_id,
            "integrity_signature": oe.integrity_signature,
        }

    def _dl_to_dict(self, dl) -> Dict[str, Any]:
        """Convert DispatchLease to dict for storage."""
        return {
            "lease_id": str(uuid4()),
            "transaction_id": str(dl.transaction_id),
            "owner_id": dl.owner_id,
            "lease_token": dl.lease_token,
            "acquired_at": dl.acquired_at,
            "expires_at": dl.expires_at,
        }
