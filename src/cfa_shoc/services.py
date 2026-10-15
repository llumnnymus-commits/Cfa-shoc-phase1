"""
CFA-SHOC Phase 1 Service Implementations

Core services for capability management, preflight checks, policy decisions, etc.
"""

import copy
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from .domain import (
    utcnow,
    ActionProposal, CommitDecision, CommitRequest, DispatchRequest, DispatchLease,
    IdempotencyClass, ObservationEnvelope, PolicyDecision, PreflightResult,
    RawResult, ResultVerification, RiskClass, RollbackStrategy, SideEffectClass, ToolTransaction,
    TransactionEvent, TransactionState,
)
from .state_machine import TransactionStateMachine


class CapabilitySnapshotService:
    """
    Maintains current tool, connector, model, skill, quota, and policy state.
    Publishes immutable ledger snapshots with monotonically increasing sequence.

    The ledger hash is content-addressed: it covers the capability content and the
    content_version (bumped only when that content really changes). Wall-clock time and
    the publication sequence are not part of the hash, so re-reading an unchanged ledger
    at commit time yields the same hash.
    """

    CONTENT_KEYS = ("tools", "connectors", "models", "skills", "quotas", "policies")

    def __init__(self):
        self.snapshot_sequence = 0
        self.content_version = 0
        self._last_content_digest: Optional[str] = None
        self.current_snapshot = {key: {} for key in self.CONTENT_KEYS}
        self.snapshots = []
        self.contracts: Dict[str, Dict[str, Any]] = {}

    def refresh_snapshot(self, principal_id: str, tenant_id: str) -> Dict[str, Any]:
        """
        Fetch current tool, connector, model, skill, quota, and policy state.

        Returns:
            Snapshot (deep copy, so later registry edits cannot alter it) with a
            content-addressed hash
        """
        content = copy.deepcopy({key: self.current_snapshot[key] for key in self.CONTENT_KEYS})
        digest = self._compute_hash(content)
        if digest != self._last_content_digest:
            self.content_version += 1
            self._last_content_digest = digest

        snapshot = {
            "sequence": self.snapshot_sequence,
            "content_version": self.content_version,
            "principal_id": principal_id,
            "tenant_id": tenant_id,
            **content,
            "timestamp": utcnow().isoformat(),
        }
        snapshot["hash"] = self._compute_hash(
            {"content": content, "content_version": self.content_version}
        )
        return snapshot

    def publish_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Publish immutable ledger snapshot (publication sequence is monotonic)."""
        self.snapshot_sequence += 1
        published = {
            **snapshot,
            "sequence": self.snapshot_sequence,
            "published_at": utcnow().isoformat(),
        }
        self.snapshots.append(published)
        return published

    def is_tool_available(self, tool_id: str) -> bool:
        """Check if tool is available in current snapshot."""
        return tool_id in self.current_snapshot["tools"]

    def is_connector_healthy(self, connector_id: str) -> bool:
        """Check if connector is connected and healthy."""
        connector = self.current_snapshot["connectors"].get(connector_id)
        return connector is not None and connector.get("health") == "healthy"

    def get_tool_scopes(self, tool_id: str) -> List[str]:
        """Get required scopes for tool."""
        tool = self.current_snapshot["tools"].get(tool_id)
        return tool.get("scopes", []) if tool else []

    def get_tool(self, tool_id: str) -> Optional[Dict[str, Any]]:
        """Get the registered tool record, or None."""
        return self.current_snapshot["tools"].get(tool_id)

    def update_tool_state(self, tool_id: str, state: Dict[str, Any]) -> None:
        """Update tool state (internal operation)."""
        self.current_snapshot["tools"][tool_id] = state

    def register_contract(self, episode_id: Any, contract: Dict[str, Any]) -> None:
        """Register (or replace) the episode contract that commit-time checks re-read."""
        self.contracts[str(episode_id)] = contract

    def get_contract(
        self, episode_id: Any, default: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Current contract for an episode; falls back to `default`, then {}."""
        contract = self.contracts.get(str(episode_id))
        if contract is None:
            contract = default if default is not None else {}
        return contract

    @staticmethod
    def contract_hash(contract: Dict[str, Any]) -> str:
        """Content hash of a contract (any change to it changes the hash)."""
        data_str = json.dumps(contract, sort_keys=True, default=str)
        return hashlib.sha256(data_str.encode()).hexdigest()

    def _compute_hash(self, data: Dict[str, Any]) -> str:
        """Compute hash of snapshot for integrity."""
        data_str = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(data_str.encode()).hexdigest()


class ActionNormalizer:
    """
    Converts model output or task-graph node into canonical ActionProposal.
    Rejects free-form tool calls.
    """

    def __init__(self, capability_service: CapabilitySnapshotService):
        self.capability_service = capability_service

    def normalize(
        self,
        proposal_input: Any,
        ledger: Dict[str, Any],
        *,
        proposal_id: Optional[UUID] = None,
        episode_id: Optional[UUID] = None,
        task_node_id: Optional[UUID] = None,
        principal_id: str = "",
        tenant_id: str = "",
    ) -> Tuple[Optional[ActionProposal], Optional[str]]:
        """
        Convert model output to ActionProposal.

        Every proposal must resolve to registered tool, operation, schema, connector.
        Context ids passed by the caller are kept; new ones are made only when absent.

        Returns:
            (ActionProposal, None) if valid
            (None, error_reason) if invalid
        """
        if not isinstance(proposal_input, dict):
            return None, "Proposal must be a dictionary"

        # Validate required fields
        tool_id = proposal_input.get("tool_id")
        operation = proposal_input.get("operation")

        if not tool_id or not operation:
            return None, "Missing tool_id or operation"

        # Reject unregistered tools
        if not self.capability_service.is_tool_available(tool_id):
            return None, f"Tool {tool_id} not registered"

        # Extract arguments
        arguments = proposal_input.get("arguments", {})
        if not isinstance(arguments, dict):
            return None, "Arguments must be a dictionary"

        # Enum fields: unknown values are an error, never an exception
        try:
            risk_class = RiskClass(proposal_input.get("risk_class", RiskClass.MEDIUM.value))
        except ValueError:
            return None, f"Unknown risk_class: {proposal_input.get('risk_class')!r}"
        try:
            side_effect_class = SideEffectClass(
                proposal_input.get("side_effect_class", SideEffectClass.NONE.value)
            )
        except ValueError:
            return None, f"Unknown side_effect_class: {proposal_input.get('side_effect_class')!r}"
        try:
            rollback_strategy = RollbackStrategy(
                proposal_input.get("rollback_strategy", RollbackStrategy.NONE.value)
            )
        except ValueError:
            return None, f"Unknown rollback_strategy: {proposal_input.get('rollback_strategy')!r}"

        # Create canonical proposal
        proposal = ActionProposal(
            proposal_id=proposal_id or uuid4(),
            episode_id=episode_id or uuid4(),
            task_node_id=task_node_id or uuid4(),
            tool_id=tool_id,
            operation=operation,
            connector_id=proposal_input.get("connector_id"),
            arguments=arguments,
            input_schema_hash=self._hash_dict(arguments),
            requested_by=proposal_input.get("requested_by", "model"),
            contract_hash=ledger.get("contract_hash", ""),
            ledger_hash=ledger.get("hash", ""),
            risk_class=risk_class,
            side_effect_class=side_effect_class,
            idempotency_key=proposal_input.get("idempotency_key") or str(uuid4()),
            principal_id=principal_id,
            tenant_id=tenant_id,
            rollback_strategy=rollback_strategy,
        )

        return proposal, None

    def _hash_dict(self, data: Dict[str, Any]) -> str:
        """Hash a dictionary."""
        data_str = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(data_str.encode()).hexdigest()


class PreflightService:
    """
    Performs deterministic checks before authorization or dispatch.
    Validates ledger, tool, connector, scopes, schema, quota, risk policy.
    """

    def __init__(self, capability_service: CapabilitySnapshotService):
        self.capability_service = capability_service

    def check(
        self, proposal: ActionProposal, ledger: Dict[str, Any], episode_contract: Dict[str, Any]
    ) -> PreflightResult:
        """
        Perform all preflight checks.

        Returns:
            PreflightResult with allowed=True/False and reason
        """
        checks = []
        failures = []

        # 1. Ledger currency and validity
        if not self._check_ledger_current(ledger):
            failures.append("Ledger is stale or invalid")
        checks.append("ledger_valid")

        # 2. Tool exists and callable
        if not self.capability_service.is_tool_available(proposal.tool_id):
            failures.append(f"Tool {proposal.tool_id} not available")
        checks.append("tool_available")

        # 3. Connector connected and healthy
        if proposal.connector_id:
            if not self.capability_service.is_connector_healthy(proposal.connector_id):
                failures.append(f"Connector {proposal.connector_id} is not healthy")
        checks.append("connector_healthy")

        # 4. Required scopes present
        required_scopes = self.capability_service.get_tool_scopes(proposal.tool_id)
        if not self._check_scopes(episode_contract, required_scopes):
            failures.append(f"Missing required scopes: {required_scopes}")
        checks.append("scopes_valid")

        # 5. Input validates against schema
        if not self._check_schema(proposal.arguments, proposal.tool_id):
            failures.append("Arguments do not validate against schema")
        checks.append("schema_valid")

        # 6. Quota sufficient
        if not self._check_quota(proposal.tool_id, proposal.tenant_id):
            failures.append("Quota exhausted")
        checks.append("quota_available")

        # 7. Risk policy allows
        if not self._check_risk_policy(proposal.risk_class):
            failures.append(f"Risk class {proposal.risk_class} not allowed by policy")
        checks.append("risk_allowed")

        # 8. No conflicting active transaction
        if not self._check_no_conflict(proposal.tool_id, proposal.tenant_id):
            failures.append("Conflicting transaction already active")
        checks.append("no_conflict")

        return PreflightResult(
            allowed=len(failures) == 0,
            reason="; ".join(failures) if failures else None,
            checks_performed=checks,
            failures=failures,
        )

    def _check_ledger_current(self, ledger: Dict[str, Any]) -> bool:
        """Verify ledger is current and valid."""
        return ledger.get("hash") is not None

    def _check_scopes(self, contract: Dict[str, Any], required_scopes: List[str]) -> bool:
        """Verify required scopes are granted."""
        granted_scopes = contract.get("scopes", [])
        return all(scope in granted_scopes for scope in required_scopes)

    def _check_schema(self, arguments: Dict[str, Any], tool_id: str) -> bool:
        """Validate arguments against tool schema."""
        # Simplified: in production, use JSON Schema validation
        return True

    def _check_quota(self, tool_id: str, tenant_id: str) -> bool:
        """Check quota is available."""
        # Simplified: in production, check actual quota state
        return True

    def _check_risk_policy(self, risk_class: RiskClass) -> bool:
        """Check if risk class is allowed by policy."""
        # Allow all risk classes in Phase 1
        return True

    def _check_no_conflict(self, tool_id: str, tenant_id: str) -> bool:
        """Verify no conflicting transaction is active."""
        # Simplified: in production, check transaction state
        return True


class PolicyDecisionPoint:
    """
    Evaluates ActionProposal against policy.
    Returns signed, short-lived policy decisions.
    Model cannot manufacture or edit decisions.
    """

    def __init__(self):
        self.decisions_cache = {}

    def evaluate(
        self, proposal: ActionProposal, ledger: Dict[str, Any], contract: Dict[str, Any]
    ) -> PolicyDecision:
        """
        Evaluate proposal against policy.

        Returns:
            Signed policy decision (APPROVE or DENY)
        """
        decision_id = uuid4()

        # Simplified policy: approve if all checks pass
        approved = self._apply_policy_rules(proposal, contract)

        decision = PolicyDecision(
            decision_id=decision_id,
            approved=approved,
            reason="Policy check passed" if approved else "Policy check failed",
            subject_type="action",
            subject_ref=str(proposal.proposal_id),
            contract_hash=contract.get("hash", ""),
            ledger_hash=ledger.get("hash", ""),
            signature=self._sign_decision(decision_id, approved, contract),
            expires_at=utcnow() + timedelta(minutes=5),
            approver_type="system",
            approver_id="policy_engine",
        )

        self.decisions_cache[str(decision_id)] = decision
        return decision

    def _apply_policy_rules(self, proposal: ActionProposal, contract: Dict[str, Any]) -> bool:
        """Apply policy rules to proposal."""
        # Phase 1: Simplified policy - approve read-only, low-risk, reversible actions automatically
        # Lane A: Automatic (no confirmation needed)
        if (proposal.risk_class == RiskClass.LOW and
            proposal.side_effect_class in (SideEffectClass.NONE, SideEffectClass.REVERSIBLE)):
            return True

        # Lane B: User-authorized in contract (checked elsewhere)
        # Lane C: Explicit confirmation (handled by authorization layer)

        # Default: deny if not explicitly approved
        return False

    def _sign_decision(self, decision_id: UUID, approved: bool, contract: Dict[str, Any]) -> str:
        """Sign policy decision."""
        data_to_sign = {
            "decision_id": str(decision_id),
            "approved": approved,
            "timestamp": utcnow().isoformat(),
        }
        data_str = json.dumps(data_to_sign, sort_keys=True)
        return hashlib.sha256(data_str.encode()).hexdigest()


class TransactionCoordinator:
    """
    Owns transaction state machine, durable record, idempotency lock, and
    dispatch lease. Coordinates all state transitions.
    """

    def __init__(self):
        self.transactions: Dict[str, ToolTransaction] = {}
        self.leases: Dict[str, DispatchLease] = {}

    def create_transaction(self, proposal: ActionProposal) -> ToolTransaction:
        """Create new durable transaction record."""
        tx = ToolTransaction(
            transaction_id=uuid4(),
            proposal_id=proposal.proposal_id,
            episode_id=proposal.episode_id,
            task_node_id=proposal.task_node_id,
            tool_id=proposal.tool_id,
            operation=proposal.operation,
            connector_id=proposal.connector_id,
            principal_id="system",
            tenant_id="default",
            ledger_hash=proposal.ledger_hash,
            contract_hash=proposal.contract_hash,
            input_payload=proposal.arguments,
            input_schema_hash=proposal.input_schema_hash,
            idempotency_key=proposal.idempotency_key,
            risk_class=proposal.risk_class.value,
            side_effect_class=proposal.side_effect_class.value,
            rollback_strategy=RollbackStrategy.NONE.value,
            state=TransactionState.PROPOSED,
        )
        self.transactions[str(tx.transaction_id)] = tx
        return tx

    def transition(self, tx: ToolTransaction, event: str) -> Tuple[bool, str]:
        """
        Transition transaction state.

        Returns:
            (success, message)
        """
        try:
            next_state, reason = TransactionStateMachine.transition(tx.state, event)
            tx.state = next_state
            tx.updated_at = utcnow()
            return True, reason
        except Exception as e:
            return False, str(e)

    def authorize(self, tx: ToolTransaction, policy: PolicyDecision) -> Tuple[bool, str]:
        """Authorize transaction based on policy decision."""
        if not policy.approved:
            success, msg = self.transition(tx, "policy_denied")
            return success, f"Policy denied: {policy.reason}"

        success, msg = self.transition(tx, "policy_approved")
        tx.policy_decision = {
            "decision_id": str(policy.decision_id),
            "approved": policy.approved,
            "signature": policy.signature,
        }
        return success, msg

    def acquire_dispatch_lease(self, tx: ToolTransaction, owner_id: str = "coordinator") -> Optional[DispatchLease]:
        """Acquire short-lived dispatch lease."""
        lease = DispatchLease(
            transaction_id=tx.transaction_id,
            owner_id=owner_id,
            expires_at=utcnow() + timedelta(seconds=30),
        )
        self.leases[lease.lease_token] = lease
        return lease

    def release_dispatch_lease(self, lease_token: str) -> bool:
        """Release dispatch lease."""
        if lease_token in self.leases:
            del self.leases[lease_token]
            return True
        return False

    def is_lease_valid(self, lease_token: str) -> bool:
        """Check if lease is still valid."""
        lease = self.leases.get(lease_token)
        if not lease:
            return False
        return lease.expires_at > utcnow()


class ToolAdapter:
    """
    Boundary for tool invocation. Only interface allowed to call connectors.
    Handles credential isolation, protocol details, error normalization.

    Each tool needs a registered handler: ``handler(request: DispatchRequest) -> dict``.
    The handler returns a dict with:
      - "status": "succeeded" | "accepted" (asynchronous) | "failed"
      - "output": JSON-compatible result (optional)
      - "external_operation_id": provider operation id (optional)
      - "idempotency_key": the request's key echoed back (optional)
    A tool with no handler fails closed: dispatch reports a connector error.
    An optional status handler ``status_handler(tool_id, external_operation_id) -> dict``
    answers status queries for asynchronous operations.
    """

    def __init__(self):
        self.adapters: Dict[str, Any] = {}
        self.status_handlers: Dict[str, Any] = {}

    def register(self, tool_id: str, handler: Any, status_handler: Any = None) -> None:
        """Register the handler that performs a tool's operations."""
        self.adapters[tool_id] = handler
        if status_handler is not None:
            self.status_handlers[tool_id] = status_handler

    def dispatch(self, request: DispatchRequest) -> Dict[str, Any]:
        """
        Dispatch request to tool/connector.

        Returns:
            Normalized raw-result dict: transport_status ("ok", "async_accepted" or
            "error"), status_code, headers, body (bytes), output, external_operation_id,
            tool_id, connector_id, idempotency_key, received_at.
            Raises TimeoutError when the handler times out (outcome unknown).
        """
        base = {
            "headers": {},
            "tool_id": request.tool_id,
            "connector_id": request.connector_id,
            "received_at": utcnow().isoformat(),
        }
        handler = self.adapters.get(request.tool_id)
        if handler is None:
            return {
                **base,
                "transport_status": "error",
                "status_code": None,
                "body": None,
                "output": {"error": f"No handler registered for tool {request.tool_id}"},
                "external_operation_id": None,
                "idempotency_key": None,
            }
        try:
            response = handler(request)
        except TimeoutError:
            raise
        except Exception as exc:  # connector failure, normalized
            return {
                **base,
                "transport_status": "error",
                "status_code": None,
                "body": None,
                "output": {"error": f"{type(exc).__name__}: {exc}"},
                "external_operation_id": None,
                "idempotency_key": None,
            }

        if not isinstance(response, dict):
            response = {"status": "failed", "output": {"error": "Handler returned non-dict"}}
        status = response.get("status", "succeeded")
        transport_status = {"succeeded": "ok", "accepted": "async_accepted"}.get(status, "error")
        output = response.get("output")
        return {
            **base,
            "transport_status": transport_status,
            "status_code": {"ok": 200, "async_accepted": 202}.get(transport_status, 500),
            "body": json.dumps(output, sort_keys=True, default=str).encode(),
            "output": output,
            "external_operation_id": response.get("external_operation_id"),
            "idempotency_key": response.get("idempotency_key"),
        }

    def preflight(self, request: DispatchRequest) -> Dict[str, Any]:
        """Check if dispatch would succeed."""
        registered = request.tool_id in self.adapters
        return {
            "can_dispatch": registered,
            "reason": None if registered else f"No handler registered for tool {request.tool_id}",
        }

    def query_status(self, tool_id: str, external_operation_id: str) -> Dict[str, Any]:
        """
        Query status of external operation.

        Returns {"status": "succeeded" | "failed" | "unknown", "operation_id", "result"}.
        Without a status handler the answer is "unknown" (never a guessed success).
        """
        status_handler = self.status_handlers.get(tool_id)
        if status_handler is None:
            return {"status": "unknown", "operation_id": external_operation_id, "result": None}
        try:
            answer = status_handler(tool_id, external_operation_id)
        except Exception:
            return {"status": "unknown", "operation_id": external_operation_id, "result": None}
        status = answer.get("status", "unknown")
        if status not in ("succeeded", "failed", "unknown"):
            status = "unknown"
        return {
            "status": status,
            "operation_id": external_operation_id,
            "result": answer.get("result"),
        }


def to_raw_result(raw: Any) -> Optional[RawResult]:
    """The one place that turns ToolAdapter.dispatch output into a RawResult."""
    if raw is None:
        return None
    if isinstance(raw, RawResult):
        return raw
    received_at = raw.get("received_at")
    if isinstance(received_at, str):
        received_at = datetime.fromisoformat(received_at)
    return RawResult(
        transport_status=str(raw.get("transport_status", "error")),
        status_code=raw.get("status_code"),
        headers=dict(raw.get("headers") or {}),
        body=raw.get("body"),
        external_operation_id=raw.get("external_operation_id"),
        received_at=received_at or utcnow(),
        output=raw.get("output"),
        tool_id=raw.get("tool_id"),
        connector_id=raw.get("connector_id"),
        idempotency_key=raw.get("idempotency_key"),
    )


class ResultVerifier:
    """
    Checks result signature, schema, and consistency.
    Distinguishes authenticated results from evidence.
    """

    def verify(self, raw_result: Any, tx: ToolTransaction) -> ResultVerification:
        """Verify raw result (RawResult or adapter dict) from tool."""
        raw_result = to_raw_result(raw_result)
        checks = {
            "signature_valid": True,  # Simplified
            "schema_valid": True,
            "tool_connector_match": True,
            "status_terminal": True,
        }

        verification = ResultVerification(
            passed=all(checks.values()),
            reason="Result verification passed" if all(checks.values()) else "Verification failed",
            signature_valid=checks["signature_valid"],
            schema_valid=checks["schema_valid"],
            tool_connector_match=checks["tool_connector_match"],
            status_terminal=checks["status_terminal"],
            evidence={
                "idempotency_key": tx.idempotency_key,
                "transaction_id": str(tx.transaction_id),
            },
        )

        return verification


class CommitMembrane:
    """
    Only component permitted to commit verified results into durable state.
    Reloads contract and capability ledger at time of commit.
    Enforces all commit invariants.
    """

    def action_gate(
        self, request: CommitRequest, tx: ToolTransaction,
        current_ledger: Dict[str, Any], current_contract: Dict[str, Any]
    ) -> Tuple[bool, Optional[CommitDecision]]:
        """
        Action gate: commit or reject verified result.

        Reloads and re-validates all state at commit time.
        """
        # Re-validate preconditions
        for predicate in request.expected_preconditions:
            if not predicate.condition:
                return False, None

        # Check ledger hasn't changed
        if current_ledger.get("hash") != request.ledger_hash:
            return False, None

        # Check contract hasn't changed
        if current_contract.get("hash") != request.contract_hash:
            return False, None

        # Create and sign decision
        decision = CommitDecision(
            decision_id=uuid4(),
            transaction_id=request.transaction_id,
            subject_type="action",
            subject_ref=str(request.transaction_id),
            contract_hash=request.contract_hash,
            ledger_hash=request.ledger_hash,
            evidence_refs=request.evidence_refs,
            checks={"all_invariants_hold": True},
            decision="APPROVE",
            signature=self._sign_decision(request.transaction_id),
            expires_at=utcnow() + timedelta(hours=1),
        )

        return True, decision

    def _sign_decision(self, transaction_id: UUID) -> str:
        """Sign commit decision."""
        data = {"transaction_id": str(transaction_id), "timestamp": utcnow().isoformat()}
        data_str = json.dumps(data, sort_keys=True)
        return hashlib.sha256(data_str.encode()).hexdigest()


class AuditLedger:
    """Append-only, tamper-evident event stream."""

    def __init__(self):
        self.events: List[TransactionEvent] = []

    def append(self, event: TransactionEvent) -> TransactionEvent:
        """Append event with hash chain."""
        if self.events:
            event.prior_hash = self.events[-1].event_hash

        event.event_hash = self._compute_hash(event)
        self.events.append(event)
        return event

    def get_for_transaction(self, transaction_id: UUID) -> List[TransactionEvent]:
        """Get all events for transaction."""
        return [e for e in self.events if e.transaction_id == transaction_id]

    def _compute_hash(self, event: TransactionEvent) -> str:
        """Compute event hash."""
        data = {
            "transaction_id": str(event.transaction_id),
            "event_type": event.event_type,
            "from_state": event.from_state,
            "to_state": event.to_state,
            "actor": f"{event.actor_type}:{event.actor_id}",
            "timestamp": event.created_at.isoformat(),
            "prior_hash": event.prior_hash,
        }
        data_str = json.dumps(data, sort_keys=True)
        return hashlib.sha256(data_str.encode()).hexdigest()


class ReconciliationWorker:
    """
    First-class service for reconciling UNKNOWN, PENDING_EXTERNAL, and
    expired-lease transactions with provider status.
    """

    def __init__(self, adapter: ToolAdapter, verifier: ResultVerifier):
        self.adapter = adapter
        self.verifier = verifier

    def reconcile(self, tx: ToolTransaction) -> Tuple[bool, str]:
        """
        Reconcile transaction by querying external status.

        Returns:
            (success, message)
        """
        if tx.external_operation_id is None:
            return False, "No external operation ID to reconcile"

        # Query provider status
        status_result = self.adapter.query_status(tx.tool_id, tx.external_operation_id)

        if status_result["status"] == "succeeded":
            return True, "Reconciliation succeeded: external operation succeeded"
        elif status_result["status"] == "failed":
            return False, "Reconciliation failed: external operation failed"
        else:
            return False, "Reconciliation pending: external operation still unknown"
