# CFA-SHOC Phase 1: Deterministic Control Shell for Agent Execution

**Capability-Factual, Stateful Hybrid Orchestration and Cognition**

A deterministic control shell around a probabilistic proposal engine for governed agent execution. Enforces strict guarantees on transaction consistency, auditability, and state management.

## What This System Does

CFA-SHOC Phase 1 wraps agentic tool invocation with a robust transaction framework that guarantees:

### Core Guarantees

1. **No Unregistered Tool Dispatch** - Tools must be registered in current capability snapshot before invocation
2. **No Stale Ledger Operations** - Every proposal created against a specific ledger snapshot, enabling rollback detection
3. **No Duplicate Side Effects** - Dispatch leases prevent concurrent invocations of same transaction
4. **Deterministic State Machine** - 13 legally defined states with explicit transitions; impossible to reach COMMITTED without going through VERIFIED
5. **Tamper-Evident Audit Trail** - Hash-chained append-only event ledger detects any manipulation
6. **Idempotent Retries** - Same proposal with same idempotency key produces single side effect
7. **Policy-Driven Authorization** - 3-tier approval lanes (automatic/user/explicit) balancing control and usability
8. **Result Verification** - Signature validation and schema conformance checking before commit

## Architecture

### Transaction Lifecycle (RUN_TOOL_NODE Protocol)

```
1. Ledger Refresh
   ↓
2. Proposal Normalization (ActionProposal)
   ↓
3. Preflight Check (8 deterministic checks)
   ↓
4. Policy Evaluation (3-lane approval)
   ↓
5. Authorization (verify policy signature & expiry)
   ↓
6. Lease Acquisition (idempotency lock)
   ↓
7. Dispatch (tool invocation)
   ↓
8. Result Verification (signature & schema)
   ↓
9. Commit (persist to durable state)
```

### 13-State Transaction Machine

```
PROPOSED
├─ preflight_passed → PREFLIGHTED
└─ preflight_failed → REJECTED

PREFLIGHTED
├─ policy_approved → AUTHORIZED
└─ policy_denied → REJECTED

AUTHORIZED
└─ dispatch_started → DISPATCHED

DISPATCHED
├─ response_received → RETURNED
├─ async_accepted → PENDING_EXTERNAL
├─ deadline_exceeded → UNKNOWN
└─ connector_error → FAILED

RETURNED
├─ verification_passed → VERIFIED
└─ verification_failed → FAILED

PENDING_EXTERNAL
├─ status_succeeded → VERIFIED
├─ status_failed → FAILED
└─ status_unknown → UNKNOWN

VERIFIED
├─ action_commit_approved → COMMITTED
└─ action_commit_denied → REJECTED

UNKNOWN
├─ reconcile_succeeded → VERIFIED
├─ reconcile_failed → FAILED
└─ compensation_required → COMPENSATION_REQUIRED

COMMITTED
├─ compensation_succeeded → COMPENSATED
└─ compensation_failed → COMPENSATION_FAILED

Terminal States: COMMITTED, COMPENSATED, COMPENSATION_FAILED, FAILED, REJECTED
```

### Service Architecture

```
┌─────────────────────────────────────────────────────┐
│           RUN_TOOL_NODE Orchestrator                │
├─────────────────────────────────────────────────────┤
├─ CapabilitySnapshotService (ledger management)
├─ ActionNormalizer (proposal validation)
├─ PreflightService (8 deterministic checks)
├─ PolicyDecisionPoint (3-lane approval)
├─ TransactionCoordinator (state machine)
├─ ToolAdapter (credential isolation)
├─ ResultVerifier (result validation)
├─ CommitMembrane (commit enforcement)
├─ AuditLedger (tamper-evident logging)
└─ ReconciliationWorker (async/timeout handling)
```

## Quick Start

### Installation

```bash
# Clone repository
cd cfa-shoc-phase1

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -e ".[dev]"
```

### Running Tests

```bash
# Run all tests
pytest

# Run specific category
pytest -m unit           # Unit tests
pytest -m integration    # Integration tests  
pytest -m chaos         # Chaos tests

# Run with coverage
pytest --cov=src tests/

# Run specific test file
pytest tests/test_orchestration.py -v
```

### Using the Orchestrator

```python
from src.orchestrator import RUN_TOOL_NODE, OrchestrationContext
from src.storage import StorageManager
from src.services import (
    CapabilitySnapshotService, ActionNormalizer, PreflightService,
    PolicyDecisionPoint, ToolAdapter, ResultVerifier, CommitMembrane,
    AuditLedger,
)

# Setup storage
storage = StorageManager("sqlite:///transactions.db")
storage.init_db()

# Create services
capability_service = CapabilitySnapshotService()
normalizer = ActionNormalizer()
preflight = PreflightService()
policy = PolicyDecisionPoint()
adapter = ToolAdapter()
verifier = ResultVerifier()
commit = CommitMembrane()
audit = AuditLedger()

# Create orchestrator
orchestrator = RUN_TOOL_NODE(
    storage=storage,
    capability_service=capability_service,
    normalizer=normalizer,
    preflight=preflight,
    policy_point=policy,
    tool_adapter=adapter,
    verifier=verifier,
    commit_membrane=commit,
    audit_ledger=audit,
)

# Execute transaction
context = OrchestrationContext(
    proposal_id=uuid4(),
    episode_id=uuid4(),
    task_node_id=uuid4(),
    principal_id="user_123",
    tenant_id="tenant_456",
    requested_by="user",
)

model_proposal = {
    "tool_id": "tool_echo",
    "operation": "echo",
    "arguments": {"message": "hello"},
    "risk_class": "low",
    "side_effect_class": "none",
}

transaction, result = orchestrator.execute(model_proposal, context)
print(f"Transaction state: {transaction.state}")
print(f"Result allowed: {result.allowed}")
```

## Project Layout

```
cfa-shoc-phase1/
├── src/
│   ├── domain.py          # Core dataclasses (ActionProposal, ToolTransaction, etc.)
│   ├── state_machine.py   # 13-state transaction machine with invariants
│   ├── storage.py         # SQLAlchemy ORM and database layer
│   ├── services.py        # 10 core Phase 1 services
│   └── orchestrator.py    # RUN_TOOL_NODE orchestration protocol
├── tests/
│   ├── test_state_machine.py       # 30+ state machine tests
│   ├── test_orchestration.py       # 20+ integration tests
│   └── test_chaos_and_properties.py # 20+ chaos/property tests
├── pyproject.toml         # Project metadata and dependencies
├── pytest.ini             # Pytest configuration
├── PROJECT_STATE.md       # Detailed project status
└── README.md             # This file
```

## Key Concepts

### ActionProposal

Canonical action from probabilistic engine, normalized to:
- Never contains credentials or direct connector clients
- Typed tool_id, operation, arguments
- Risk/side-effect classification
- Idempotency key for deduplication

### Transaction State Machine

Enforces legal state progression with no shortcuts:
- PROPOSED → PREFLIGHTED → AUTHORIZED → DISPATCHED → VERIFIED → COMMITTED
- All states must be visited in order (except error paths)
- Impossible to reach COMMITTED without going through VERIFIED
- Terminal states prevent further transitions

### Preflight Checks (8)

1. **Ledger Currency**: Snapshot sequence matches current state
2. **Tool Available**: Tool registered in current snapshot
3. **Connector Healthy**: Connector connected and operational
4. **Scopes Valid**: Principal has required scopes
5. **Schema Valid**: Arguments match tool schema
6. **Quota Available**: Principal under quota
7. **Risk Policy**: Proposal risk level allowed
8. **No Conflict**: No active lease for this transaction

### Policy Decision Lanes

- **Lane A (Automatic)**: Low-risk, reversible, deterministic → auto-approved
- **Lane B (User-Authorized)**: Pre-configured in contract → approved if matches
- **Lane C (Explicit Confirmation)**: High-risk → requires explicit approval

### Dispatch Lease

Short-lived mutex preventing concurrent invocation of same transaction:
- Acquired at AUTHORIZED state
- Held until result verified
- Released on success or error
- Expires after 5 minutes (prevents deadlock)

### Observation Envelope

Result wrapper providing security properties:
- source_class: "tool_output"
- authority_level: "non_authoritative_content"
- content: Raw tool result
- integrity_signature: Hash for tamper detection

## Testing

Phase 1 includes 70+ tests covering:

### Unit Tests (30+)
- State machine transitions (22 transitions, prohibited paths)
- Terminal state enforcement
- Invariant validation (6 invariants)
- Error handling

### Integration Tests (20+)
- Full transaction lifecycle (proposal → commit)
- Service coordination
- Error scenarios and rollback
- Concurrent access handling

### Chaos & Property Tests (20+)
- Storage failures and fault injection
- Concurrent lease conflicts
- Property invariants (no unregistered dispatch, no double dispatch, etc.)
- Boundary conditions and edge cases

## Specification

This implementation follows:
- **CFA-SHOC v0.2**: Complete architecture and design (36 pages)
- **Phase 1 Implementation Addendum**: Concrete execution pseudocode and examples (22 pages)

Key sections implemented:
- Section 3.2: Transaction state machine with legal transitions
- Section 3.3: Transaction invariants and enforcement
- Section 13: Concrete Phase 1 execution pseudocode (RUN_TOOL_NODE)

## Development

### Code Quality

```bash
# Format code
black src/ tests/

# Lint code
ruff check src/ tests/

# Type checking
mypy src/

# Run tests with coverage
pytest --cov=src --cov-report=html
```

### Development Workflow

1. **Create feature branch**: `git checkout -b feature/new-feature`
2. **Implement feature** with tests
3. **Run all checks**: `black . && ruff check . && mypy src/ && pytest`
4. **Commit with clear message**: `git commit -m "Add new feature"`
5. **Push and create PR**

### Adding New Services

1. Define interface in service
2. Create ORM model if persistent storage needed
3. Implement service class with clear methods
4. Add unit tests for service methods
5. Add integration tests for orchestrator interaction
6. Update orchestrator if needed

## Next Steps

### Phase 1 Completion
- [ ] API endpoints for transaction submission
- [ ] Database migrations for PostgreSQL
- [ ] OpenTelemetry instrumentation
- [ ] Timeout recovery and async polling
- [ ] Manual reconciliation operator tools

### Phase 2
- [ ] Multi-step transaction composition
- [ ] Complex compensation strategies
- [ ] Advanced policy rule engine
- [ ] Performance optimization
- [ ] Distributed deployment support

## Architecture Decisions

### Why 13 States?

The state machine captures all meaningful points in the transaction lifecycle:
- **Proposal Phase**: PROPOSED
- **Validation Phase**: PREFLIGHTED
- **Authorization Phase**: AUTHORIZED
- **Execution Phase**: DISPATCHED, RETURNED, PENDING_EXTERNAL
- **Verification Phase**: VERIFIED
- **Commit Phase**: COMMITTED
- **Error/Recovery Phase**: UNKNOWN, FAILED, REJECTED, COMPENSATION_REQUIRED, COMPENSATED, COMPENSATION_FAILED

### Why Dispatch Leases?

Prevent duplicate side effects when:
- Network timeouts cause retries
- Duplicate proposals with same idempotency key
- Concurrent execution of same operation

### Why Hash-Chained Audit?

Provide tamper evidence:
- Each event includes hash of previous event
- Detecting modification anywhere breaks chain
- Immutable audit trail for regulatory compliance

### Why 3-Lane Policy?

Balance automation and control:
- **Lane A**: Usable for safe operations
- **Lane B**: Scales manual approval (pre-configured)
- **Lane C**: Explicit consent for risky operations

## Performance Characteristics

**Transaction Overhead**:
- Ledger refresh: ~1-5ms (network bound)
- Preflight checks: ~5-10ms (deterministic)
- Policy evaluation: ~10-50ms (depends on policy complexity)
- Lease acquisition: ~1-5ms (database)
- Dispatch: 0-X (tool dependent)
- Verification: ~5-20ms (signature validation)
- Commit: ~5-20ms (database)

**Total Framework Overhead**: ~35-150ms (before tool execution)

## Security Considerations

- **Credential Isolation**: Credentials never appear in proposal state or logs
- **Signature Validation**: Results verified before commit
- **Schema Enforcement**: Prevents malformed results
- **Audit Trail**: Hash-chained events detect tampering
- **Idempotency**: Prevents duplicate side effects
- **Policy Framework**: Restricts high-risk operations
- **Scope Validation**: Principal authorization checks

## Troubleshooting

### Transaction in UNKNOWN state

Indicates dispatch was sent but response not received within deadline:
- Check connector logs
- Query provider for operation status
- Use reconciliation worker to recover

### Policy decision denied

Check:
- Risk classification of proposal
- Principal's policy contract
- Required scopes for operation
- Approval lane requirements

### Lease acquisition timeout

Indicates concurrent execution:
- Check if previous invocation still in progress
- Verify lease expiration (default 5 min)
- May need retry backoff in client

## License

MIT

## Questions & Support

See PROJECT_STATE.md for detailed architecture documentation.
Contact: [maintainer email]
