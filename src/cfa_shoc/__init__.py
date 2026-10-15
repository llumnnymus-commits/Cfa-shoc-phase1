"""
CFA-SHOC: a transaction layer for AI agent tool calls.

Every tool call becomes a transaction that passes preflight checks, a policy decision,
a dispatch lease, result verification and a commit gate, with an audit trail.
"""

from .domain import (
    ActionProposal, CommitDecision, CommitRequest, DispatchLease, DispatchRequest,
    IdempotencyClass, ObservationEnvelope, PolicyDecision, Predicate, PreflightResult,
    RawResult, ResultVerification, RiskClass, RollbackStrategy, SideEffectClass,
    ToolTransaction, TransactionEvent, TransactionState, utcnow,
)
from .state_machine import IllegalTransitionError, TransactionInvariants, TransactionStateMachine
from .storage import LeaseConflictError, StorageManager
from .services import (
    ActionNormalizer, AuditLedger, CapabilitySnapshotService, CommitMembrane,
    PolicyDecisionPoint, PreflightService, ReconciliationWorker, ResultVerifier, ToolAdapter,
    TransactionCoordinator, to_raw_result,
)
from .orchestrator import OrchestrationContext, RUN_TOOL_NODE
from . import baton

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # domain
    "ActionProposal", "CommitDecision", "CommitRequest", "DispatchLease", "DispatchRequest",
    "IdempotencyClass", "ObservationEnvelope", "PolicyDecision", "Predicate", "PreflightResult",
    "RawResult", "ResultVerification", "RiskClass", "RollbackStrategy", "SideEffectClass",
    "ToolTransaction", "TransactionEvent", "TransactionState", "utcnow",
    # state machine
    "IllegalTransitionError", "TransactionInvariants", "TransactionStateMachine",
    # storage
    "LeaseConflictError", "StorageManager",
    # services
    "ActionNormalizer", "AuditLedger", "CapabilitySnapshotService", "CommitMembrane",
    "PolicyDecisionPoint", "PreflightService", "ReconciliationWorker", "ResultVerifier",
    "ToolAdapter", "TransactionCoordinator", "to_raw_result",
    # orchestration
    "OrchestrationContext", "RUN_TOOL_NODE",
    # DRxit baton handoffs
    "baton",
]
