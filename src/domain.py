"""
CFA-SHOC Phase 1 Domain Models

Core data structures for the Capability-Factual, Stateful Hybrid Orchestration
and Cognition system with deterministic control shell around probabilistic proposal engine.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4


class TransactionState(str, Enum):
    """Legal transaction states."""
    PROPOSED = "PROPOSED"
    PREFLIGHTED = "PREFLIGHTED"
    AUTHORIZED = "AUTHORIZED"
    DISPATCHED = "DISPATCHED"
    RETURNED = "RETURNED"
    PENDING_EXTERNAL = "PENDING_EXTERNAL"
    VERIFIED = "VERIFIED"
    COMMITTED = "COMMITTED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    COMPENSATION_REQUIRED = "COMPENSATION_REQUIRED"
    COMPENSATED = "COMPENSATED"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"


class RiskClass(str, Enum):
    """Risk classification for actions."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SideEffectClass(str, Enum):
    """Side effect classification."""
    NONE = "none"
    REVERSIBLE = "reversible"
    COMPENSATABLE = "compensatable"
    IRREVERSIBLE = "irreversible"


class IdempotencyClass(str, Enum):
    """Idempotency classification for retry behavior."""
    PURE_READ = "pure_read"
    DETERMINISTIC_COMPUTE = "deterministic_compute"
    IDEMPOTENT_WRITE = "idempotent_write"
    COMPENSATABLE_WRITE = "compensatable_write"
    NON_IDEMPOTENT_ACTION = "non_idempotent_action"


class RollbackStrategy(str, Enum):
    """How to handle rollback scenarios."""
    NONE = "none"
    FULL_ROLLBACK = "full_rollback"
    COMPENSATION = "compensation"
    ALERT_ONLY = "alert_only"


@dataclass
class Predicate:
    """A condition that must hold for action execution."""
    name: str
    condition: bool
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionProposal:
    """
    Canonical action proposal from model/planner.
    Never given credentials, raw connector clients, or durable-memory write handle.
    """
    proposal_id: UUID = field(default_factory=uuid4)
    episode_id: UUID = field(default_factory=uuid4)
    task_node_id: UUID = field(default_factory=uuid4)
    tool_id: str = ""
    operation: str = ""
    connector_id: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    input_schema_hash: str = ""
    requested_by: str = ""  # model, user, policy, recovery
    contract_hash: str = ""
    ledger_hash: str = ""
    risk_class: RiskClass = RiskClass.MEDIUM
    side_effect_class: SideEffectClass = SideEffectClass.NONE
    idempotency_key: str = ""
    proposed_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class PreflightResult:
    """Result of preflight checks."""
    allowed: bool
    reason: Optional[str] = None
    checks_performed: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)


@dataclass
class PolicyDecision:
    """Policy decision from PolicyDecisionPoint."""
    approved: bool
    reason: str
    subject_type: str  # contract, principal, tenant, policy, etc.
    subject_ref: str
    contract_hash: str
    ledger_hash: str
    decision_id: UUID = field(default_factory=uuid4)
    evidence_refs: List[str] = field(default_factory=list)
    checks: Dict[str, bool] = field(default_factory=dict)
    signature: str = ""
    expires_at: datetime = field(default_factory=datetime.utcnow)
    approver_type: str = "system"
    approver_id: str = ""


@dataclass
class DispatchRequest:
    """Request to dispatch a tool invocation."""
    transaction_id: UUID
    tool_id: str
    operation: str
    connector_id: Optional[str]
    arguments: Dict[str, Any]
    idempotency_key: str
    deadline_at: datetime = field(default_factory=datetime.utcnow)
    auth_context_ref: str = ""
    trace_context: Dict[str, str] = field(default_factory=dict)


@dataclass
class RawResult:
    """Raw result from external tool/connector."""
    transport_status: str  # HTTP status, protocol status
    status_code: Optional[int] = None
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[bytes] = None
    external_operation_id: Optional[str] = None
    received_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ResultVerification:
    """Verification of a tool result."""
    passed: bool
    reason: Optional[str] = None
    signature_valid: bool = False
    schema_valid: bool = False
    tool_connector_match: bool = False
    status_terminal: bool = False
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CommitRequest:
    """Request to commit a verified result."""
    transaction_id: UUID
    verified_result_ref: str
    contract_hash: str
    ledger_hash: str
    expected_preconditions: List[Predicate] = field(default_factory=list)
    risk_class: RiskClass = RiskClass.MEDIUM
    side_effect_class: SideEffectClass = SideEffectClass.NONE
    rollback_strategy: RollbackStrategy = RollbackStrategy.NONE
    evidence_refs: List[str] = field(default_factory=list)
    requested_commit_scope: str = "action"


@dataclass
class ToolTransaction:
    """Durable transaction record for tool execution."""
    transaction_id: UUID = field(default_factory=uuid4)
    proposal_id: UUID = field(default_factory=uuid4)
    episode_id: UUID = field(default_factory=uuid4)
    task_node_id: UUID = field(default_factory=uuid4)
    tool_id: str = ""
    operation: str = ""
    connector_id: Optional[str] = None
    principal_id: str = ""
    tenant_id: str = ""
    ledger_hash: str = ""
    contract_hash: str = ""
    input_payload: Dict[str, Any] = field(default_factory=dict)
    input_schema_hash: str = ""
    idempotency_key: str = ""
    risk_class: str = ""
    side_effect_class: str = ""
    rollback_strategy: str = ""
    state: TransactionState = TransactionState.PROPOSED
    external_operation_id: Optional[str] = None
    result_envelope: Optional[Dict[str, Any]] = None
    policy_decision: Optional[Dict[str, Any]] = None
    verification: Optional[Dict[str, Any]] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    deadline_at: Optional[datetime] = None
    version: int = 0


@dataclass
class TransactionEvent:
    """Append-only audit event."""
    event_id: UUID = field(default_factory=uuid4)
    transaction_id: UUID = field(default_factory=uuid4)
    event_type: str = ""
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    actor_type: str = ""  # system, connector, user, recovery
    actor_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    prior_hash: Optional[str] = None
    event_hash: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class CommitDecision:
    """Decision to commit or reject a transaction."""
    decision_id: UUID = field(default_factory=uuid4)
    transaction_id: UUID = field(default_factory=uuid4)
    subject_type: str = ""
    subject_ref: str = ""
    contract_hash: str = ""
    ledger_hash: str = ""
    evidence_refs: List[str] = field(default_factory=list)
    checks: Dict[str, bool] = field(default_factory=dict)
    decision: str = ""  # APPROVE, DENY
    signature: str = ""
    expires_at: datetime = field(default_factory=datetime.utcnow)
    approver_type: str = "system"
    approver_id: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class DispatchLease:
    """Short-lived lease preventing concurrent dispatch."""
    transaction_id: UUID
    owner_id: str
    lease_token: str = field(default_factory=lambda: str(uuid4()))
    acquired_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ExternalStatusQuery:
    """Query for external operation status."""
    transaction_id: UUID
    external_operation_id: str
    idempotency_key: str
    tool_id: str
    connector_id: Optional[str]


@dataclass
class ObservationEnvelope:
    """Wrapper for tool output with security properties."""
    source_class: str = "tool_output"  # source of observation
    authority_level: str = "non_authoritative_content"
    content: Any = None
    transaction_id: UUID = field(default_factory=uuid4)
    connector_id: str = ""
    integrity_signature: str = ""
