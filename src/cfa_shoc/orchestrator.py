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
    utcnow,
    ActionProposal, CommitDecision, CommitRequest, DispatchLease, DispatchRequest,
    ObservationEnvelope, PolicyDecision, PreflightResult, RawResult, ResultVerification,
    ToolTransaction, TransactionEvent, TransactionState,
)
from .state_machine import TransactionStateMachine, IllegalTransitionError
from .storage import LeaseConflictError, StorageManager
from .services import (
    CapabilitySnapshotService, ActionNormalizer, PreflightService, PolicyDecisionPoint,
    ToolAdapter, ResultVerifier, CommitMembrane, AuditLedger, to_raw_result,
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
        contract: Optional[Dict[str, Any]] = None,
    ):
        self.proposal_id = proposal_id
        self.episode_id = episode_id
        self.task_node_id = task_node_id
        self.principal_id = principal_id
        self.tenant_id = tenant_id
        self.requested_by = requested_by  # "model", "user", "policy", "recovery"
        # Episode contract: {"scopes": [...], "pre_authorized": [...], "max_risk": "..."}
        self.contract: Dict[str, Any] = contract if contract is not None else {}
        self.created_at = utcnow()
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
    9. Commit (CommitMembrane re-reads ledger and contract, then gates the commit)

    Every state change goes through TransactionStateMachine.transition and is persisted
    together with its audit event in one database transaction.
    """

    # How long reconciliation keeps asking the provider before giving up on an outcome
    RECONCILE_WINDOW = timedelta(hours=1)
    ASYNC_WINDOW = timedelta(hours=24)

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
        # Contract each transaction started with (fallback when the registry has none)
        self._run_contracts: Dict[str, Dict[str, Any]] = {}
        # Services that need storage or the capability ledger get this run's, if unset
        if getattr(self.preflight, "storage", "absent") is None:
            self.preflight.storage = storage
        if getattr(self.verifier, "capability_service", "absent") is None:
            self.verifier.capability_service = capability_service

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
            (ToolTransaction, PreflightResult) - the result's `allowed` is True only when
            the transaction COMMITTED or was accepted asynchronously (PENDING_EXTERNAL).

        Raises:
            IllegalTransitionError: On invalid state transition (a bug, never expected)
        """
        session = self.storage.get_session()
        transaction: Optional[ToolTransaction] = None
        try:
            # Step 1: Ledger refresh - get current capability snapshot
            ledger_snapshot = self._ledger_refresh(context)
            context.ledger_snapshot = ledger_snapshot
            contract = self._current_contract(context)
            contract_hash = self.capability_service.contract_hash(contract)

            # Step 2: Proposal normalization - convert to canonical ActionProposal
            action_proposal, error = self._normalize_proposal(
                model_proposal, context, ledger_snapshot, contract_hash
            )
            if action_proposal is None:
                # Refused before any transaction could be dispatched; still audited.
                transaction = self._refused_transaction(
                    model_proposal, context, ledger_snapshot, contract_hash
                )
                context.transaction_id = transaction.transaction_id
                self._record(
                    session, transaction, "orchestration_started",
                    payload={"context": context.to_dict(), "model_proposal": _safe(model_proposal)},
                )
                self._transition(
                    session, transaction, "preflight_failed",
                    {"reason": f"Normalization failed: {error}"},
                )
                return transaction, PreflightResult(
                    allowed=False, reason=f"Normalization failed: {error}",
                    checks_performed=["normalization"], failures=[error],
                )

            # Idempotent replay: same tenant + idempotency key already committed
            committed = self.storage.find_by_idempotency_key(
                session, context.tenant_id, action_proposal.idempotency_key,
                states=[TransactionState.COMMITTED.value],
            )
            if committed:
                replay = self._tx_from_row(committed[0])
                return replay, PreflightResult(
                    allowed=True,
                    reason=f"Idempotent replay: already committed as {replay.transaction_id}",
                )

            # Create durable transaction record in PROPOSED state
            transaction = self._create_transaction(action_proposal, context, ledger_snapshot)
            context.transaction_id = transaction.transaction_id
            self._run_contracts[str(transaction.transaction_id)] = context.contract
            self._record(
                session, transaction, "orchestration_started",
                payload={"context": context.to_dict(), "action_proposal": self._ap_to_dict(action_proposal)},
            )

            # Step 3: Preflight check - 8 deterministic checks
            preflight_result = self.preflight.check(action_proposal, ledger_snapshot, contract)
            if not preflight_result.allowed:
                self._transition(session, transaction, "preflight_failed",
                                 {"reason": preflight_result.reason,
                                  "failures": preflight_result.failures})
                return transaction, preflight_result
            self._transition(session, transaction, "preflight_passed",
                             {"checks": preflight_result.checks_performed})

            # Step 4: Policy evaluation - PolicyDecisionPoint yields signed decision
            policy_decision = self.policy_point.evaluate(action_proposal, ledger_snapshot, contract)
            transaction.policy_decision = self._pd_to_dict(policy_decision)
            if policy_decision.requires_confirmation:
                # Lane C: stays PREFLIGHTED until confirm() or decline()
                self._record(
                    session, transaction, "confirmation_requested",
                    TransactionState.PREFLIGHTED.value, TransactionState.PREFLIGHTED.value,
                    {"decision_id": str(policy_decision.decision_id),
                     "expires_at": policy_decision.expires_at.isoformat()},
                )
                return transaction, PreflightResult(
                    allowed=False, requires_confirmation=True,
                    reason="Awaiting explicit confirmation (lane C); call confirm() or decline()",
                )
            if not policy_decision.approved:
                self._transition(session, transaction, "policy_denied",
                                 {"reason": policy_decision.reason})
                return transaction, PreflightResult(
                    allowed=False, reason=f"Policy denied: {policy_decision.reason}"
                )
            self._transition(session, transaction, "policy_approved",
                             {"decision_id": str(policy_decision.decision_id)})

            return self._authorize_and_run(session, transaction, policy_decision, contract)

        except Exception as e:
            session.rollback()
            if transaction is not None and not isinstance(e, IllegalTransitionError):
                self._record_best_effort(
                    transaction, "orchestration_error",
                    {"error": str(e), "exception_type": type(e).__name__},
                )
            raise
        finally:
            session.close()

    def confirm(
        self, transaction_id: Any, approver_id: str
    ) -> Tuple[ToolTransaction, PreflightResult]:
        """
        Lane C: an approver confirms a transaction waiting in PREFLIGHTED. The policy point
        issues a signed approval naming the approver, `policy_approved` fires, and steps 5
        to 9 run. An expired request ends REJECTED.

        Raises:
            ValueError: unknown transaction, or it is not waiting for confirmation
        """
        session = self.storage.get_session()
        try:
            transaction, pending = self._pending_confirmation(session, transaction_id)
            try:
                decision = self.policy_point.confirm(pending, approver_id)
            except ValueError as exc:
                if "expired" not in str(exc):
                    raise
                self._transition(session, transaction, "policy_denied",
                                 {"reason": "Confirmation request expired"})
                return transaction, PreflightResult(allowed=False, reason="Confirmation request expired")
            transaction.policy_decision = self._pd_to_dict(decision)
            self._transition(session, transaction, "policy_approved",
                             {"decision_id": str(decision.decision_id),
                              "approver_type": "human", "approver_id": approver_id})
            contract = self.capability_service.get_contract(
                transaction.episode_id, self._run_contracts.get(str(transaction.transaction_id), {})
            )
            return self._authorize_and_run(session, transaction, decision, contract)
        finally:
            session.close()

    def decline(
        self, transaction_id: Any, approver_id: str, reason: str = "Declined by approver"
    ) -> Tuple[ToolTransaction, PreflightResult]:
        """Lane C: an approver declines; the transaction ends REJECTED without dispatch."""
        session = self.storage.get_session()
        try:
            transaction, _ = self._pending_confirmation(session, transaction_id)
            self._transition(session, transaction, "policy_denied",
                             {"reason": reason, "approver_type": "human", "approver_id": approver_id})
            return transaction, PreflightResult(allowed=False, reason=reason)
        finally:
            session.close()

    def reconcile(
        self, transaction_id: Any, now: Optional[datetime] = None
    ) -> Tuple[ToolTransaction, str]:
        """
        Move one UNKNOWN, PENDING_EXTERNAL, COMPENSATION_REQUIRED or stuck DISPATCHED
        transaction forward using the provider's answer. Returns (transaction, action),
        where action is the event fired or "wait:<why>" when nothing could be decided yet.

        Provider "succeeded" -> verify, VERIFIED, then commit through the action gate.
        Provider "failed"    -> FAILED.
        Provider "unknown"   -> wait until the deadline; then PENDING_EXTERNAL -> UNKNOWN,
                                and UNKNOWN -> FAILED (no side effect) or
                                COMPENSATION_REQUIRED (side effect possible).
        COMPENSATION_REQUIRED -> the tool's compensator decides COMPENSATED or
                                COMPENSATION_FAILED; without one a human must act.
        """
        now = now or utcnow()
        session = self.storage.get_session()
        try:
            row = self.storage.get_transaction(session, str(transaction_id))
            if row is None:
                raise ValueError(f"Unknown transaction {transaction_id}")
            transaction = self._tx_from_row(row)
            state = transaction.state

            if state == TransactionState.DISPATCHED:
                if self.storage.has_live_lease(session, str(transaction.transaction_id)):
                    return transaction, "wait:dispatch in progress"
                transaction.deadline_at = now + self.RECONCILE_WINDOW
                self._transition(session, transaction, "deadline_exceeded",
                                 {"reason": "Dispatch lease expired without a recorded outcome"})
                return transaction, "deadline_exceeded"

            if state == TransactionState.COMPENSATION_REQUIRED:
                outcome = self.tool_adapter.compensate(
                    transaction.tool_id, transaction.transaction_id, transaction.external_operation_id
                )
                if outcome is None:
                    return transaction, "wait:no compensator registered, needs a human"
                event = "compensation_succeeded" if outcome == "succeeded" else "compensation_failed"
                self._transition(session, transaction, event, {"compensator_outcome": outcome})
                return transaction, event

            if state not in (TransactionState.PENDING_EXTERNAL, TransactionState.UNKNOWN):
                return transaction, f"wait:not reconcilable in state {state.value}"

            pending = state == TransactionState.PENDING_EXTERNAL
            if transaction.external_operation_id:
                status = self.tool_adapter.query_status(
                    transaction.tool_id, transaction.external_operation_id
                )
            else:
                status = {"status": "unknown", "result": None}
            answer = status.get("status", "unknown")

            if answer == "succeeded":
                raw = to_raw_result({
                    "transport_status": "ok",
                    "tool_id": status.get("tool_id"),
                    "connector_id": transaction.connector_id,
                    "output": status.get("result"),
                    "external_operation_id": transaction.external_operation_id,
                    "idempotency_key": status.get("idempotency_key"),
                })
                verification = self.verifier.verify(raw, transaction)
                transaction.verification = self._rv_to_dict(verification)
                if not verification.passed:
                    event = "status_failed" if pending else "reconcile_failed"
                    self._transition(session, transaction, event,
                                     {"reason": f"Provider result failed verification: {verification.reason}"})
                    return transaction, event
                observation = self._observation(transaction, raw, verification)
                transaction.result_envelope = {"observation": self._oe_to_dict(observation), "verified": True}
                event = "status_succeeded" if pending else "reconcile_succeeded"
                self._transition(session, transaction, event,
                                 {"output_hash": observation.integrity_signature})
                committed, _ = self._commit(session, transaction, observation)
                if committed.state == TransactionState.COMMITTED:
                    return committed, "action_commit_approved"
                return committed, "action_commit_denied"

            if answer == "failed":
                event = "status_failed" if pending else "reconcile_failed"
                self._transition(session, transaction, event, {"reason": "Provider reports failure"})
                return transaction, event

            # Still unknown
            deadline = transaction.deadline_at
            if deadline is not None and now < deadline:
                return transaction, "wait:provider status unknown, deadline not reached"
            if pending:
                transaction.deadline_at = now + self.RECONCILE_WINDOW
                self._transition(session, transaction, "status_unknown",
                                 {"reason": "Provider status unknown past the async deadline"})
                return transaction, "status_unknown"
            if transaction.side_effect_class in ("", "none"):
                self._transition(session, transaction, "reconcile_failed",
                                 {"reason": "Outcome unknown past deadline; no side effect, safe to retry"})
                return transaction, "reconcile_failed"
            self._transition(session, transaction, "compensation_required",
                             {"reason": "Outcome unknown past deadline; side effect possible"})
            return transaction, "compensation_required"
        finally:
            session.close()

    def _pending_confirmation(self, session, transaction_id: Any) -> Tuple[ToolTransaction, PolicyDecision]:
        row = self.storage.get_transaction(session, str(transaction_id))
        if row is None:
            raise ValueError(f"Unknown transaction {transaction_id}")
        transaction = self._tx_from_row(row)
        pending = transaction.policy_decision or {}
        if transaction.state != TransactionState.PREFLIGHTED or not pending.get("requires_confirmation"):
            raise ValueError(f"Transaction {transaction_id} is not waiting for confirmation")
        return transaction, self._pd_from_dict(pending)

    # Steps 5 to 9 (shared with flows that authorize later, e.g. explicit confirmation)

    def _authorize_and_run(
        self,
        session,
        transaction: ToolTransaction,
        policy_decision: PolicyDecision,
        contract: Dict[str, Any],
    ) -> Tuple[ToolTransaction, PreflightResult]:
        """Run steps 5 to 9 for a transaction that is AUTHORIZED."""
        # Step 5: Authorization - verify policy signature and expiry
        if not self._verify_authorization(policy_decision):
            reason = "Policy decision expired or invalid signature"
            self._transition(session, transaction, "authorization_revoked", {"reason": reason})
            return transaction, PreflightResult(allowed=False, reason=reason)

        # Step 6: Lease acquisition - acquire short-lived idempotency lock
        lease = self._acquire_lease(transaction, session)
        if lease is None:
            reason = "Could not acquire idempotency lock (concurrent execution)"
            self._transition(session, transaction, "authorization_revoked", {"reason": reason})
            return transaction, PreflightResult(allowed=False, reason=reason)

        try:
            # Step 7: Dispatch - invoke tool via ToolAdapter/connector boundary
            self._transition(session, transaction, "dispatch_started",
                             {"lease_token_hash": _sha(lease.lease_token)})
            self.capability_service.consume_quota(transaction.tenant_id, transaction.tool_id)
            try:
                raw_result = to_raw_result(self._dispatch(transaction))
            except TimeoutError as exc:
                reason = f"Dispatch deadline exceeded: {exc}"
                transaction.deadline_at = utcnow() + self.RECONCILE_WINDOW
                self._transition(session, transaction, "deadline_exceeded", {"reason": reason})
                return transaction, PreflightResult(allowed=False, reason=reason)

            if raw_result is None or raw_result.transport_status == "error":
                detail = raw_result.output if raw_result is not None else None
                self._transition(session, transaction, "connector_error",
                                 {"reason": "Tool dispatch failed (connector error)",
                                  "detail": _safe(detail)})
                return transaction, PreflightResult(
                    allowed=False, reason="Tool dispatch failed (connector error)"
                )

            if raw_result.external_operation_id:
                transaction.external_operation_id = raw_result.external_operation_id

            # Asynchronous acceptance: reconciliation finishes the transaction later
            if raw_result.transport_status == "async_accepted":
                transaction.deadline_at = utcnow() + self.ASYNC_WINDOW
                self._transition(session, transaction, "async_accepted",
                                 {"external_operation_id": transaction.external_operation_id})
                return transaction, PreflightResult(
                    allowed=True,
                    reason="Tool dispatch accepted asynchronously; awaiting external status query",
                )

            self._transition(session, transaction, "response_received",
                             {"status_code": raw_result.status_code})

            # Step 8: Result verification - check signature, schema, consistency
            verification = self.verifier.verify(raw_result, transaction)
            transaction.verification = self._rv_to_dict(verification)
            if not verification.passed:
                self._transition(session, transaction, "verification_failed",
                                 {"reason": verification.reason})
                return transaction, PreflightResult(
                    allowed=False, reason=f"Result verification failed: {verification.reason}"
                )

            observation = self._observation(transaction, raw_result, verification)
            transaction.result_envelope = {
                "observation": self._oe_to_dict(observation),
                "verified": True,
            }
            self._transition(session, transaction, "verification_passed",
                             {"output_hash": observation.integrity_signature})

            # Step 9: Commit through the CommitMembrane action gate
            return self._commit(session, transaction, observation)
        finally:
            self._release_lease(lease, session)

    # Private orchestration steps

    def _ledger_refresh(self, context: OrchestrationContext) -> Dict[str, Any]:
        """Step 1: Refresh ledger snapshot with current capability state."""
        snapshot = self.capability_service.refresh_snapshot(context.principal_id, context.tenant_id)
        return self.capability_service.publish_snapshot(snapshot)

    def _current_contract(self, context: OrchestrationContext) -> Dict[str, Any]:
        """The episode contract as it is right now (registry first, then the context)."""
        return self.capability_service.get_contract(context.episode_id, context.contract)

    def _normalize_proposal(
        self,
        model_proposal: Any,
        context: OrchestrationContext,
        ledger_snapshot: Dict[str, Any],
        contract_hash: str,
    ) -> Tuple[Optional[ActionProposal], Optional[str]]:
        """Step 2: Normalize model proposal to canonical ActionProposal."""
        return self.normalizer.normalize(
            model_proposal,
            {**ledger_snapshot, "contract_hash": contract_hash},
            proposal_id=context.proposal_id,
            episode_id=context.episode_id,
            task_node_id=context.task_node_id,
            principal_id=context.principal_id,
            tenant_id=context.tenant_id,
        )

    def _create_transaction(
        self,
        action_proposal: ActionProposal,
        context: OrchestrationContext,
        ledger_snapshot: Dict[str, Any],
    ) -> ToolTransaction:
        """Create initial durable transaction record."""
        return ToolTransaction(
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

    def _refused_transaction(
        self,
        model_proposal: Any,
        context: OrchestrationContext,
        ledger_snapshot: Dict[str, Any],
        contract_hash: str,
    ) -> ToolTransaction:
        """Record for a proposal that failed normalization (it is never dispatched)."""
        raw = model_proposal if isinstance(model_proposal, dict) else {}
        arguments = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {}
        return ToolTransaction(
            transaction_id=uuid4(),
            proposal_id=context.proposal_id,
            episode_id=context.episode_id,
            task_node_id=context.task_node_id,
            tool_id=str(raw.get("tool_id") or ""),
            operation=str(raw.get("operation") or ""),
            connector_id=raw.get("connector_id") if isinstance(raw.get("connector_id"), str) else None,
            principal_id=context.principal_id,
            tenant_id=context.tenant_id,
            ledger_hash=ledger_snapshot["hash"],
            contract_hash=contract_hash,
            input_payload=_safe(arguments),
            input_schema_hash="",
            idempotency_key=str(raw.get("idempotency_key") or ""),
            risk_class=str(raw.get("risk_class") or ""),
            side_effect_class=str(raw.get("side_effect_class") or ""),
            rollback_strategy=str(raw.get("rollback_strategy") or ""),
            state=TransactionState.PROPOSED,
        )

    def _verify_authorization(self, policy_decision: PolicyDecision) -> bool:
        """Step 5: Verify policy signature and check expiry."""
        if not policy_decision.approved:
            return False
        if policy_decision.expires_at < utcnow():
            return False
        verify = getattr(self.policy_point, "verify_signature", None)
        if verify is not None:
            return bool(verify(policy_decision))
        return True

    def _acquire_lease(self, transaction: ToolTransaction, session) -> Optional[DispatchLease]:
        """Step 6: Acquire short-lived idempotency lock. None if another holder has it."""
        lease = DispatchLease(
            transaction_id=transaction.transaction_id,
            owner_id=transaction.principal_id,
            lease_token=str(uuid4()),
            acquired_at=utcnow(),
            expires_at=utcnow() + timedelta(minutes=5),
        )
        try:
            self.storage.acquire_lease(session, self._dl_to_dict(lease))
            return lease
        except LeaseConflictError:
            session.rollback()
            return None

    def _dispatch(self, transaction: ToolTransaction) -> Dict[str, Any]:
        """Step 7: Dispatch tool invocation via ToolAdapter."""
        dispatch_request = DispatchRequest(
            transaction_id=transaction.transaction_id,
            tool_id=transaction.tool_id,
            operation=transaction.operation,
            connector_id=transaction.connector_id,
            arguments=transaction.input_payload,
            idempotency_key=transaction.idempotency_key,
            deadline_at=utcnow() + timedelta(minutes=5),
            auth_context_ref=transaction.principal_id,
        )
        return self.tool_adapter.dispatch(dispatch_request)

    def _observation(
        self, transaction: ToolTransaction, raw_result: RawResult, verification: ResultVerification
    ) -> ObservationEnvelope:
        """Wrap verified tool output; it stays non-authoritative content."""
        content = raw_result.output
        return ObservationEnvelope(
            source_class="tool_output",
            authority_level="non_authoritative_content",
            content=content,
            transaction_id=transaction.transaction_id,
            connector_id=transaction.connector_id or "",
            integrity_signature=_sha(json.dumps(content, sort_keys=True, default=str)),
        )

    def _commit(
        self, session, transaction: ToolTransaction, observation: ObservationEnvelope
    ) -> Tuple[ToolTransaction, PreflightResult]:
        """Step 9: re-read ledger and contract, then commit only through the action gate."""
        commit_request = CommitRequest(
            transaction_id=transaction.transaction_id,
            verified_result_ref=observation.integrity_signature,
            contract_hash=transaction.contract_hash,
            ledger_hash=transaction.ledger_hash,
            risk_class=transaction.risk_class,
            side_effect_class=transaction.side_effect_class,
            rollback_strategy=transaction.rollback_strategy,
            evidence_refs=[f"tx:{transaction.transaction_id}",
                           f"output:{observation.integrity_signature}"],
        )
        current_ledger = self.capability_service.refresh_snapshot(
            transaction.principal_id, transaction.tenant_id
        )
        current_contract = self.capability_service.get_contract(
            transaction.episode_id, self._run_contracts.get(str(transaction.transaction_id), {})
        )
        approved, decision = self.commit_membrane.action_gate(
            commit_request,
            transaction,
            current_ledger,
            {"hash": self.capability_service.contract_hash(current_contract)},
        )

        if approved and decision is not None:
            self.storage.save_commit_decision(session, self._cd_to_dict(decision))
            self._transition(session, transaction, "action_commit_approved",
                             {"commit_decision_id": str(decision.decision_id)})
            return transaction, PreflightResult(allowed=True, reason="Transaction committed successfully")

        reason = "Commit membrane rejected result (ledger, contract or precondition changed)"
        self._transition(session, transaction, "action_commit_denied", {"reason": reason})
        return transaction, PreflightResult(allowed=False, reason=reason)

    def _release_lease(self, lease: Optional[DispatchLease], session) -> None:
        """Release idempotency lock after dispatch (success or failure)."""
        if lease:
            self.storage.release_lease(session, lease.lease_token)

    def _transition(
        self,
        session,
        transaction: ToolTransaction,
        event: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        The only place the orchestrator changes state: state machine transition, then
        the new state and its audit event are stored in one database transaction.
        """
        from_state = transaction.state
        try:
            next_state, reason = TransactionStateMachine.transition(from_state, event)
        except IllegalTransitionError as e:
            self._record_best_effort(
                transaction, "illegal_transition_attempt",
                {"error": str(e), "from_state": from_state.value, "event": event},
            )
            raise
        transaction.state = next_state
        transaction.updated_at = utcnow()
        self._record(
            session, transaction, event, from_state.value, next_state.value,
            {"reason": reason, **(payload or {})},
        )

    def _record(
        self,
        session,
        transaction: ToolTransaction,
        event_type: str,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> TransactionEvent:
        """Append the audit event and persist the transaction row atomically."""
        if event_type == "orchestration_started":
            to_state = transaction.state.value
        event = self._make_event(transaction, event_type, from_state, to_state, payload or {})
        row = self.storage.record_transition(
            session, self._tx_to_dict(transaction), self._event_to_dict(event)
        )
        event.sequence, event.prior_hash, event.event_hash = row.sequence, row.prior_hash, row.event_hash
        self.audit_ledger.append(event)
        return event

    def _record_best_effort(
        self, transaction: ToolTransaction, event_type: str, payload: Dict[str, Any]
    ) -> None:
        """Store a non-transition event (errors) in its own DB transaction, if possible."""
        event = self._make_event(transaction, event_type, None, None, payload)
        session = self.storage.get_session()
        try:
            if self.storage.get_transaction(session, str(transaction.transaction_id)) is None:
                return
            row = self.storage.append_event(session, self._event_to_dict(event))
            event.sequence, event.prior_hash, event.event_hash = row.sequence, row.prior_hash, row.event_hash
            self.audit_ledger.append(event)
        except Exception:
            pass  # never mask the original failure
        finally:
            session.close()

    def _make_event(
        self,
        transaction: ToolTransaction,
        event_type: str,
        from_state: Optional[str],
        to_state: Optional[str],
        payload: Dict[str, Any],
    ) -> TransactionEvent:
        return TransactionEvent(
            transaction_id=transaction.transaction_id,
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            actor_type="system",
            actor_id="run_tool_node",
            payload=_safe(payload),
        )

    # Serialization helpers

    def _tx_from_row(self, row) -> ToolTransaction:
        """Rebuild a ToolTransaction from its stored row."""
        return ToolTransaction(
            transaction_id=UUID(row.transaction_id),
            proposal_id=UUID(row.proposal_id),
            episode_id=UUID(row.episode_id),
            task_node_id=UUID(row.task_node_id),
            tool_id=row.tool_id,
            operation=row.operation,
            connector_id=row.connector_id,
            principal_id=row.principal_id,
            tenant_id=row.tenant_id,
            ledger_hash=row.ledger_hash,
            contract_hash=row.contract_hash,
            input_payload=row.input_payload,
            input_schema_hash=row.input_schema_hash,
            idempotency_key=row.idempotency_key,
            risk_class=row.risk_class,
            side_effect_class=row.side_effect_class,
            rollback_strategy=row.rollback_strategy,
            state=TransactionState(row.state),
            external_operation_id=row.external_operation_id,
            result_envelope=row.result_envelope,
            policy_decision=row.policy_decision,
            verification=row.verification,
            created_at=row.created_at,
            updated_at=row.updated_at,
            deadline_at=row.deadline_at,
            version=row.version,
        )

    def _tx_to_dict(self, tx: ToolTransaction) -> Dict[str, Any]:
        """Convert ToolTransaction to dict for storage (state is the transaction's own)."""
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
            "state": tx.state.value,
            "external_operation_id": tx.external_operation_id,
            "result_envelope": _safe(tx.result_envelope),
            "policy_decision": tx.policy_decision,
            "verification": tx.verification,
            "deadline_at": tx.deadline_at,
        }

    def _event_to_dict(self, event: TransactionEvent) -> Dict[str, Any]:
        return {
            "event_id": str(event.event_id),
            "transaction_id": str(event.transaction_id),
            "event_type": event.event_type,
            "from_state": event.from_state,
            "to_state": event.to_state,
            "actor_type": event.actor_type,
            "actor_id": event.actor_id,
            "payload": event.payload,
            "prior_hash": event.prior_hash,
            "event_hash": event.event_hash,
            "created_at": event.created_at,
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
            "rollback_strategy": ap.rollback_strategy.value,
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
            "lane": pd.lane,
            "requires_confirmation": pd.requires_confirmation,
        }

    def _pd_from_dict(self, data: Dict[str, Any]) -> PolicyDecision:
        """Rebuild a PolicyDecision stored with _pd_to_dict."""
        return PolicyDecision(
            decision_id=UUID(data["decision_id"]),
            approved=data["approved"],
            reason=data["reason"],
            subject_type=data["subject_type"],
            subject_ref=data["subject_ref"],
            contract_hash=data["contract_hash"],
            ledger_hash=data["ledger_hash"],
            evidence_refs=list(data.get("evidence_refs", [])),
            checks=dict(data.get("checks", {})),
            signature=data.get("signature", ""),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            approver_type=data.get("approver_type", ""),
            approver_id=data.get("approver_id", ""),
            lane=data.get("lane", ""),
            requires_confirmation=data.get("requires_confirmation", False),
        )

    def _cd_to_dict(self, cd: CommitDecision) -> Dict[str, Any]:
        """Convert CommitDecision to dict for storage."""
        return {
            "decision_id": str(cd.decision_id),
            "transaction_id": str(cd.transaction_id),
            "subject_type": cd.subject_type,
            "subject_ref": cd.subject_ref,
            "contract_hash": cd.contract_hash,
            "ledger_hash": cd.ledger_hash,
            "evidence_refs": list(cd.evidence_refs),
            "checks": dict(cd.checks),
            "decision": cd.decision,
            "signature": cd.signature,
            "expires_at": cd.expires_at,
            "approver_type": cd.approver_type,
            "approver_id": cd.approver_id,
            "created_at": cd.created_at,
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
            "evidence": _safe(rv.evidence),
        }

    def _oe_to_dict(self, oe: ObservationEnvelope) -> Dict[str, Any]:
        """Convert ObservationEnvelope to dict for storage."""
        return {
            "source_class": oe.source_class,
            "authority_level": oe.authority_level,
            "content": _safe(oe.content),
            "transaction_id": str(oe.transaction_id),
            "connector_id": oe.connector_id,
            "integrity_signature": oe.integrity_signature,
        }

    def _dl_to_dict(self, dl: DispatchLease) -> Dict[str, Any]:
        """Convert DispatchLease to dict for storage."""
        return {
            "lease_id": str(uuid4()),
            "transaction_id": str(dl.transaction_id),
            "owner_id": dl.owner_id,
            "lease_token": dl.lease_token,
            "acquired_at": dl.acquired_at,
            "expires_at": dl.expires_at,
        }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _safe(value: Any) -> Any:
    """Make a value JSON-compatible for storage (bytes, UUIDs and datetimes become strings)."""
    return json.loads(json.dumps(value, default=str)) if value is not None else None
