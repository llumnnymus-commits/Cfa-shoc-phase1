"""
CFA-SHOC Phase 1 Service Implementations

Core services for capability management, preflight checks, policy decisions, etc.
"""

import copy
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from .domain import (
    utcnow,
    ActionProposal, CommitDecision, CommitRequest, DispatchRequest, DispatchLease,
    IdempotencyClass, ObservationEnvelope, PolicyDecision, PreflightResult,
    RawResult, ResultVerification, RiskClass, RollbackStrategy, SideEffectClass, ToolTransaction,
    TransactionEvent, TransactionState, risk_at_most,
)
from .audit import compute_event_hash, event_content
from .schema import validate as validate_schema
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
        # Quota usage counters: kept outside the hashed ledger content, so consuming quota
        # does not change the ledger hash between authorization and commit.
        self.quota_used: Dict[Tuple[str, str], int] = {}

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

    def quota_limit(self, tenant_id: str, tool_id: str) -> Optional[int]:
        """
        Call limit for a tenant and tool from the ledger's `quotas`
        ({tenant_id: {tool_id: limit}}, with "*" as a wildcard tenant), or None.
        """
        quotas = self.current_snapshot["quotas"]
        for tenant_key in (tenant_id, "*"):
            limit = quotas.get(tenant_key, {}).get(tool_id)
            if limit is not None:
                return int(limit)
        return None

    def quota_remaining(self, tenant_id: str, tool_id: str) -> Optional[int]:
        """Calls left for a tenant and tool, or None when no limit is set."""
        limit = self.quota_limit(tenant_id, tool_id)
        if limit is None:
            return None
        return max(0, limit - self.quota_used.get((tenant_id, tool_id), 0))

    def consume_quota(self, tenant_id: str, tool_id: str) -> None:
        """Count one dispatch against the tenant's quota for the tool."""
        key = (tenant_id, tool_id)
        self.quota_used[key] = self.quota_used.get(key, 0) + 1

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
    Validates ledger, tool, connector, scopes, schema, quota, risk policy, conflicts.

    The conflict check needs transaction storage; RUN_TOOL_NODE supplies its own storage
    when none was given here. Without storage the conflict check is reported as skipped.
    """

    # States in which a transaction is still in flight (not final and not committed)
    IN_FLIGHT_STATES = [
        state.value for state in TransactionState
        if state not in TransactionStateMachine.TERMINAL_STATES
    ]

    def __init__(self, capability_service: CapabilitySnapshotService, storage: Any = None):
        self.capability_service = capability_service
        self.storage = storage

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
        tool = self.capability_service.get_tool(proposal.tool_id)

        # 1. Ledger currency and validity
        if not self._check_ledger_current(ledger):
            failures.append("Ledger is stale or invalid")
        checks.append("ledger_valid")

        # 2. Tool exists and callable
        if tool is None:
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

        # 5. Input validates against the tool's declared input schema
        schema_errors = self._schema_errors(proposal.arguments, tool)
        if schema_errors:
            failures.append("Arguments do not validate against schema: " + "; ".join(schema_errors))
        checks.append("schema_valid")

        # 6. Quota sufficient
        if not self._check_quota(proposal.tool_id, proposal.tenant_id):
            failures.append("Quota exhausted")
        checks.append("quota_available")

        # 7. Risk policy allows (contract max_risk caps the risk class)
        if not self._check_risk_policy(proposal.risk_class, episode_contract):
            failures.append(
                f"Risk class {proposal.risk_class.value} exceeds contract max_risk "
                f"{episode_contract.get('max_risk')}"
            )
        checks.append("risk_allowed")

        # 8. No conflicting active transaction
        conflict = self._conflict(proposal, tool)
        if conflict:
            failures.append(conflict)
        checks.append("no_conflict" if self.storage is not None else "no_conflict_skipped")

        return PreflightResult(
            allowed=len(failures) == 0,
            reason="; ".join(failures) if failures else None,
            checks_performed=checks,
            failures=failures,
        )

    def _check_ledger_current(self, ledger: Dict[str, Any]) -> bool:
        """Verify ledger is current: it has a hash and matches the live capability content."""
        if ledger.get("hash") is None:
            return False
        version = ledger.get("content_version")
        return version is None or version == self.capability_service.content_version

    def _check_scopes(self, contract: Dict[str, Any], required_scopes: List[str]) -> bool:
        """Verify required scopes are granted."""
        granted_scopes = contract.get("scopes", [])
        return all(scope in granted_scopes for scope in required_scopes)

    def _schema_errors(self, arguments: Dict[str, Any], tool: Optional[Dict[str, Any]]) -> List[str]:
        """Validate arguments against the tool's `input_schema` (none declared: valid)."""
        if not tool:
            return []
        return validate_schema(arguments, tool.get("input_schema"))

    def _check_schema(self, arguments: Dict[str, Any], tool_id: str) -> bool:
        """Validate arguments against tool schema."""
        return not self._schema_errors(arguments, self.capability_service.get_tool(tool_id))

    def _check_quota(self, tool_id: str, tenant_id: str) -> bool:
        """Check quota is available (no limit set: available)."""
        remaining = self.capability_service.quota_remaining(tenant_id, tool_id)
        return remaining is None or remaining > 0

    def _check_risk_policy(self, risk_class: RiskClass, contract: Optional[Dict[str, Any]] = None) -> bool:
        """Check the risk class against the contract's max_risk (no cap: allowed)."""
        cap = (contract or {}).get("max_risk")
        if cap is None:
            return True
        try:
            return risk_at_most(risk_class, cap)
        except ValueError:
            return False  # an unreadable cap fails closed

    def _conflict(self, proposal: ActionProposal, tool: Optional[Dict[str, Any]]) -> Optional[str]:
        """Describe a conflicting in-flight transaction, or None."""
        if self.storage is None:
            return None
        session = self.storage.get_session()
        try:
            same_key = [
                row for row in self.storage.find_by_idempotency_key(
                    session, proposal.tenant_id, proposal.idempotency_key, self.IN_FLIGHT_STATES
                )
                if row.proposal_id != str(proposal.proposal_id)
            ]
            if same_key:
                return (
                    f"Conflicting transaction already active: {same_key[0].transaction_id} "
                    f"has the same idempotency key ({same_key[0].state})"
                )
            if tool and tool.get("exclusive"):
                busy = [
                    row for row in self.storage.find_in_flight(
                        session, proposal.tenant_id, proposal.tool_id, self.IN_FLIGHT_STATES
                    )
                    if row.proposal_id != str(proposal.proposal_id)
                ]
                if busy:
                    return (
                        f"Conflicting transaction already active: exclusive tool "
                        f"{proposal.tool_id} is in use by {busy[0].transaction_id}"
                    )
            return None
        finally:
            session.close()

    def _check_no_conflict(self, tool_id: str, tenant_id: str) -> bool:
        """Verify no transaction is in flight for an exclusive tool and tenant."""
        if self.storage is None:
            return True
        session = self.storage.get_session()
        try:
            return not self.storage.find_in_flight(session, tenant_id, tool_id, self.IN_FLIGHT_STATES)
        finally:
            session.close()


class PolicyDecisionPoint:
    """
    Evaluates ActionProposal against policy.
    Returns signed, short-lived policy decisions.
    Model cannot manufacture or edit decisions: they are HMAC-SHA256 signed with a key
    injected at construction (a random per-instance key when none is given).

    Lanes:
      A  automatic: LOW risk with NONE or REVERSIBLE side effects.
      B  pre-authorized: the contract's `pre_authorized` list has an entry
         {"tool_id", "operation" ("*" = any), "max_risk"} covering the proposal.
      C  explicit confirmation: anything else at or below contract `confirm_up_to`
         returns requires_confirmation; RUN_TOOL_NODE.confirm() completes it.
      Everything else is denied.
    """

    DECISION_TTL = timedelta(minutes=5)
    CONFIRMATION_TTL = timedelta(hours=1)

    def __init__(self, signing_key: Optional[bytes] = None):
        self.decisions_cache = {}
        self.__key = _SecretKey(signing_key if signing_key is not None else os.urandom(32))

    def __repr__(self) -> str:  # never show the key
        return "PolicyDecisionPoint(signing_key=<hidden>)"

    def evaluate(
        self, proposal: ActionProposal, ledger: Dict[str, Any], contract: Dict[str, Any]
    ) -> PolicyDecision:
        """
        Evaluate proposal against policy.

        Returns:
            Signed policy decision (approved, denied, or requires_confirmation)
        """
        lane, approved, requires_confirmation, reason, checks = self._apply_policy_rules(
            proposal, contract
        )
        ttl = self.CONFIRMATION_TTL if requires_confirmation else self.DECISION_TTL
        decision = PolicyDecision(
            approved=approved,
            reason=reason,
            subject_type="action",
            subject_ref=str(proposal.proposal_id),
            contract_hash=CapabilitySnapshotService.contract_hash(contract),
            ledger_hash=ledger.get("hash", ""),
            checks=checks,
            expires_at=utcnow() + ttl,
            approver_type="system",
            approver_id="policy_engine",
            lane=lane,
            requires_confirmation=requires_confirmation,
        )
        decision.signature = self.sign(decision)
        self.decisions_cache[str(decision.decision_id)] = decision
        return decision

    def confirm(self, pending: PolicyDecision, approver_id: str) -> PolicyDecision:
        """
        Lane C: turn a verified, unexpired requires_confirmation decision into a signed
        approval by a named approver.

        Raises:
            ValueError: the pending decision is not a valid, open confirmation request
        """
        if not pending.requires_confirmation:
            raise ValueError("Decision does not require confirmation")
        if not self.verify_signature(pending):
            raise ValueError("Pending decision signature is invalid")
        if pending.expires_at < utcnow():
            raise ValueError("Confirmation request has expired")
        if not approver_id:
            raise ValueError("approver_id is required")
        decision = PolicyDecision(
            approved=True,
            reason=f"Explicitly confirmed by {approver_id}",
            subject_type=pending.subject_type,
            subject_ref=pending.subject_ref,
            contract_hash=pending.contract_hash,
            ledger_hash=pending.ledger_hash,
            evidence_refs=[f"policy:{pending.decision_id}"],
            checks={**pending.checks, "explicit_confirmation": True},
            expires_at=utcnow() + self.DECISION_TTL,
            approver_type="human",
            approver_id=approver_id,
            lane="C",
            requires_confirmation=False,
        )
        decision.signature = self.sign(decision)
        self.decisions_cache[str(decision.decision_id)] = decision
        return decision

    def _apply_policy_rules(
        self, proposal: ActionProposal, contract: Dict[str, Any]
    ) -> Tuple[str, bool, bool, str, Dict[str, bool]]:
        """Return (lane, approved, requires_confirmation, reason, checks)."""
        lane_a = (
            proposal.risk_class == RiskClass.LOW
            and proposal.side_effect_class in (SideEffectClass.NONE, SideEffectClass.REVERSIBLE)
        )
        lane_b = any(self._covers(entry, proposal) for entry in contract.get("pre_authorized", []))
        confirm_cap = contract.get("confirm_up_to")
        lane_c = False
        if confirm_cap is not None:
            try:
                lane_c = risk_at_most(proposal.risk_class, confirm_cap)
            except ValueError:
                lane_c = False
        checks = {"lane_a": lane_a, "lane_b": lane_b, "lane_c_eligible": lane_c}

        if lane_a:
            return "A", True, False, "Approved automatically (lane A: low risk, no lasting side effect)", checks
        if lane_b:
            return "B", True, False, "Approved by contract pre-authorization (lane B)", checks
        if lane_c:
            return "C", False, True, "Explicit confirmation required (lane C)", checks
        return "", False, False, (
            f"Policy check failed: risk {proposal.risk_class.value} with side effect "
            f"{proposal.side_effect_class.value} is not automatic, pre-authorized or confirmable"
        ), checks

    @staticmethod
    def _covers(entry: Dict[str, Any], proposal: ActionProposal) -> bool:
        if not isinstance(entry, dict) or entry.get("tool_id") != proposal.tool_id:
            return False
        if entry.get("operation", "*") not in ("*", proposal.operation):
            return False
        try:
            return risk_at_most(proposal.risk_class, entry.get("max_risk", RiskClass.LOW.value))
        except ValueError:
            return False

    @staticmethod
    def _signed_fields(decision: PolicyDecision) -> Dict[str, Any]:
        return {
            "decision_id": str(decision.decision_id),
            "approved": decision.approved,
            "reason": decision.reason,
            "subject_type": decision.subject_type,
            "subject_ref": decision.subject_ref,
            "contract_hash": decision.contract_hash,
            "ledger_hash": decision.ledger_hash,
            "evidence_refs": list(decision.evidence_refs),
            "checks": dict(decision.checks),
            "expires_at": decision.expires_at.isoformat(),
            "approver_type": decision.approver_type,
            "approver_id": decision.approver_id,
            "lane": decision.lane,
            "requires_confirmation": decision.requires_confirmation,
        }

    def sign(self, decision: PolicyDecision) -> str:
        """HMAC-SHA256 over the decision's fields (everything except the signature)."""
        return _hmac(self.__key.value, self._signed_fields(decision))

    def verify_signature(self, decision: PolicyDecision) -> bool:
        """True when the signature matches this point's key and the decision's fields."""
        return bool(decision.signature) and hmac.compare_digest(decision.signature, self.sign(decision))


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
        self.compensators: Dict[str, Any] = {}

    def register(
        self, tool_id: str, handler: Any, status_handler: Any = None, compensator: Any = None
    ) -> None:
        """
        Register the handler that performs a tool's operations, plus optional
        ``status_handler(tool_id, external_operation_id) -> {"status", "result"}`` and
        ``compensator(tool_id, transaction_id, external_operation_id) -> {"status"}``.
        """
        self.adapters[tool_id] = handler
        if status_handler is not None:
            self.status_handlers[tool_id] = status_handler
        if compensator is not None:
            self.compensators[tool_id] = compensator

    def compensate(
        self, tool_id: str, transaction_id: Any, external_operation_id: Optional[str]
    ) -> Optional[str]:
        """
        Undo a tool's side effect. Returns "succeeded" or "failed", or None when no
        compensator is registered (a human has to act).
        """
        compensator = self.compensators.get(tool_id)
        if compensator is None:
            return None
        try:
            answer = compensator(tool_id, transaction_id, external_operation_id)
        except Exception:
            return "failed"
        return "succeeded" if (answer or {}).get("status") == "succeeded" else "failed"

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
        unknown = {"status": "unknown", "operation_id": external_operation_id,
                   "result": None, "tool_id": tool_id}
        status_handler = self.status_handlers.get(tool_id)
        if status_handler is None:
            return unknown
        try:
            answer = status_handler(tool_id, external_operation_id)
        except Exception:
            return unknown
        status = (answer or {}).get("status", "unknown")
        if status not in ("succeeded", "failed", "unknown"):
            status = "unknown"
        return {
            "status": status,
            "operation_id": external_operation_id,
            "result": answer.get("result"),
            "tool_id": tool_id,
            "idempotency_key": answer.get("idempotency_key"),
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
    Checks a raw tool result before it can be committed:
      - status_terminal: the adapter reported a finished, successful call ("ok")
      - schema_valid: output matches the tool's declared `output_schema`, if any
      - tool_connector_match: the result is for this transaction's tool and connector
      - idempotency: an echoed idempotency key must match; tools declaring
        `echoes_idempotency_key: True` must echo it
    Results are not signed by tools in Phase 1, so `signature_valid` is reported as False
    and is not required to pass. The result stays non-authoritative content.

    RUN_TOOL_NODE supplies its capability service when none was given here.
    """

    def __init__(self, capability_service: Optional[CapabilitySnapshotService] = None):
        self.capability_service = capability_service

    def verify(self, raw_result: Any, tx: ToolTransaction) -> ResultVerification:
        """Verify raw result (RawResult or adapter dict) from tool."""
        raw = to_raw_result(raw_result)
        if raw is None:
            return ResultVerification(passed=False, reason="No result")
        tool = self.capability_service.get_tool(tx.tool_id) if self.capability_service else None
        tool = tool or {}
        problems: List[str] = []

        status_terminal = raw.transport_status == "ok"
        if not status_terminal:
            problems.append(f"status not terminal-success ({raw.transport_status})")

        schema_errors = validate_schema(raw.output, tool.get("output_schema"))
        if schema_errors:
            problems.append("output schema: " + "; ".join(schema_errors))

        tool_connector_match = raw.tool_id == tx.tool_id and raw.connector_id == tx.connector_id
        if not tool_connector_match:
            problems.append(
                f"result is for {raw.tool_id}/{raw.connector_id}, expected {tx.tool_id}/{tx.connector_id}"
            )

        idempotency_ok = True
        if raw.idempotency_key is not None and raw.idempotency_key != tx.idempotency_key:
            idempotency_ok = False
            problems.append("echoed idempotency key does not match")
        elif tool.get("echoes_idempotency_key") and raw.idempotency_key is None:
            idempotency_ok = False
            problems.append("tool must echo the idempotency key but did not")

        return ResultVerification(
            passed=not problems,
            reason="Result verification passed" if not problems else "; ".join(problems),
            signature_valid=False,
            schema_valid=not schema_errors,
            tool_connector_match=tool_connector_match,
            status_terminal=status_terminal,
            evidence={
                "idempotency_key": tx.idempotency_key,
                "idempotency_ok": idempotency_ok,
                "transaction_id": str(tx.transaction_id),
                "external_operation_id": raw.external_operation_id,
            },
        )


class CommitMembrane:
    """
    Only component permitted to commit verified results into durable state.
    Reloads contract and capability ledger at time of commit.
    Enforces all commit invariants. Commit decisions are HMAC-SHA256 signed with a key
    injected at construction (a random per-instance key when none is given).
    """

    def __init__(self, signing_key: Optional[bytes] = None):
        self.__key = _SecretKey(signing_key if signing_key is not None else os.urandom(32))

    def __repr__(self) -> str:  # never show the key
        return "CommitMembrane(signing_key=<hidden>)"

    def action_gate(
        self, request: CommitRequest, tx: ToolTransaction,
        current_ledger: Dict[str, Any], current_contract: Dict[str, Any]
    ) -> Tuple[bool, Optional[CommitDecision]]:
        """
        Action gate: commit or reject verified result.

        Reloads and re-validates all state at commit time. Returns (False, None) when any
        check fails.
        """
        checks = {
            "same_transaction": request.transaction_id == tx.transaction_id,
            "state_verified": tx.state == TransactionState.VERIFIED,
            "verification_passed": bool(tx.verification and tx.verification.get("passed")),
            "result_envelope_present": tx.result_envelope is not None,
            "preconditions_hold": all(p.condition for p in request.expected_preconditions),
            "ledger_unchanged": current_ledger.get("hash") == request.ledger_hash,
            "contract_unchanged": current_contract.get("hash") == request.contract_hash,
        }
        if not all(checks.values()):
            return False, None

        decision = CommitDecision(
            transaction_id=request.transaction_id,
            subject_type="action",
            subject_ref=str(request.transaction_id),
            contract_hash=request.contract_hash,
            ledger_hash=request.ledger_hash,
            evidence_refs=list(request.evidence_refs),
            checks=checks,
            decision="APPROVE",
            expires_at=utcnow() + timedelta(hours=1),
            approver_type="system",
            approver_id="commit_membrane",
        )
        decision.signature = self.sign(decision)
        return True, decision

    @staticmethod
    def _signed_fields(decision: CommitDecision) -> Dict[str, Any]:
        return {
            "decision_id": str(decision.decision_id),
            "transaction_id": str(decision.transaction_id),
            "subject_type": decision.subject_type,
            "subject_ref": decision.subject_ref,
            "contract_hash": decision.contract_hash,
            "ledger_hash": decision.ledger_hash,
            "evidence_refs": list(decision.evidence_refs),
            "checks": dict(decision.checks),
            "decision": decision.decision,
            "expires_at": decision.expires_at.isoformat(),
            "approver_type": decision.approver_type,
            "approver_id": decision.approver_id,
        }

    def sign(self, decision: CommitDecision) -> str:
        """HMAC-SHA256 over the decision's fields (everything except the signature)."""
        return _hmac(self.__key.value, self._signed_fields(decision))

    def verify_signature(self, decision: CommitDecision) -> bool:
        """True when the signature matches this membrane's key and the decision's fields."""
        return bool(decision.signature) and hmac.compare_digest(decision.signature, self.sign(decision))


class _SecretKey:
    """Holds signing key bytes; repr/str never show them, so logging a service is safe."""

    __slots__ = ("value",)

    def __init__(self, value: bytes):
        if not isinstance(value, (bytes, bytearray)) or len(value) < 16:
            raise ValueError("signing_key must be at least 16 bytes")
        self.value = bytes(value)

    def __repr__(self) -> str:
        return "<hidden>"

    __str__ = __repr__


def _hmac(key: bytes, fields: Dict[str, Any]) -> str:
    message = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


class AuditLedger:
    """
    Append-only, tamper-evident event stream (in-memory mirror).

    Chains events per transaction with the same hash definition as durable storage
    (cfa_shoc.audit), so the mirror and the database agree event for event. Storage is the
    authority: use StorageManager.verify_chain for the durable record.
    """

    def __init__(self):
        self.events: List[TransactionEvent] = []
        self._last: Dict[str, TransactionEvent] = {}

    def append(self, event: TransactionEvent) -> TransactionEvent:
        """
        Append event with hash chain. If the event already carries a hash (from storage),
        it must equal the one computed here.
        """
        key = str(event.transaction_id)
        last = self._last.get(key)
        if last is None and event.event_hash:
            # First event this mirror sees for the transaction (e.g. a new process):
            # adopt storage's position in the chain, still checking the hash below.
            sequence, prior_hash = event.sequence, event.prior_hash
        else:
            sequence = 0 if last is None else last.sequence + 1
            prior_hash = None if last is None else last.event_hash
        computed = self._compute_hash(event, sequence, prior_hash)
        if event.event_hash and (event.event_hash != computed or event.sequence != sequence):
            raise AssertionError(
                f"Audit mirror diverged from storage for transaction {key} at sequence {sequence}"
            )
        event.sequence, event.prior_hash, event.event_hash = sequence, prior_hash, computed
        self.events.append(event)
        self._last[key] = event
        return event

    def get_for_transaction(self, transaction_id: UUID) -> List[TransactionEvent]:
        """Get all events for transaction."""
        return [e for e in self.events if str(e.transaction_id) == str(transaction_id)]

    def verify(self, transaction_id: UUID) -> Tuple[bool, Optional[int]]:
        """Recompute the in-memory chain: (True, None) or (False, first bad index)."""
        prior_hash = None
        for index, event in enumerate(self.get_for_transaction(transaction_id)):
            if event.sequence != index or event.prior_hash != prior_hash:
                return False, index
            if self._compute_hash(event, index, prior_hash) != event.event_hash:
                return False, index
            prior_hash = event.event_hash
        return True, None

    def _compute_hash(
        self, event: TransactionEvent, sequence: Optional[int] = None, prior_hash: Optional[str] = None
    ) -> str:
        """Compute event hash."""
        return compute_event_hash(event_content(
            event_id=event.event_id, transaction_id=event.transaction_id,
            sequence=event.sequence if sequence is None else sequence,
            event_type=event.event_type, from_state=event.from_state, to_state=event.to_state,
            actor_type=event.actor_type, actor_id=event.actor_id, payload=event.payload,
            created_at=event.created_at,
            prior_hash=event.prior_hash if sequence is None else prior_hash,
        ))


class ReconciliationWorker:
    """
    First-class service for reconciling UNKNOWN, PENDING_EXTERNAL, COMPENSATION_REQUIRED
    and expired-lease (stuck DISPATCHED) transactions with provider status.

    With an orchestrator, `run_once()` drives every reconcilable transaction one step
    through RUN_TOOL_NODE.reconcile (which fires the state-machine events, verifies, and
    commits through the action gate). Run it on a schedule.
    """

    def __init__(self, adapter: ToolAdapter, verifier: ResultVerifier, orchestrator: Any = None):
        self.adapter = adapter
        self.verifier = verifier
        self.orchestrator = orchestrator

    def run_once(self, now: Optional[datetime] = None, limit: int = 100) -> List[Tuple[str, str]]:
        """
        Reconcile up to `limit` transactions. Returns (transaction_id, action) pairs, where
        action names the event fired or why nothing was done.
        """
        if self.orchestrator is None:
            raise ValueError("run_once needs an orchestrator")
        storage = self.orchestrator.storage
        session = storage.get_session()
        try:
            ids = [row.transaction_id for row in storage.find_reconcilable_transactions(session, limit)]
        finally:
            session.close()
        results = []
        for transaction_id in ids:
            _, action = self.orchestrator.reconcile(transaction_id, now=now)
            results.append((transaction_id, action))
        return results

    def reconcile(self, tx: ToolTransaction) -> Tuple[bool, str]:
        """
        Query external status for one transaction without changing it.

        Returns:
            (success, message)
        """
        if tx.external_operation_id is None:
            return False, "No external operation ID to reconcile"

        status_result = self.adapter.query_status(tx.tool_id, tx.external_operation_id)

        if status_result["status"] == "succeeded":
            return True, "Reconciliation succeeded: external operation succeeded"
        elif status_result["status"] == "failed":
            return False, "Reconciliation failed: external operation failed"
        else:
            return False, "Reconciliation pending: external operation still unknown"
