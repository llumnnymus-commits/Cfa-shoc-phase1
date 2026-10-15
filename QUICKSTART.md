# CFA-SHOC Phase 1 Quick Start Guide

## TL;DR

CFA-SHOC is a deterministic control shell that wraps agent tool invocation with a robust transaction framework. It guarantees no unregistered tool dispatch, no stale operations, no duplicate side effects, and maintains tamper-evident audit trails.

### In 30 Seconds

```python
# 1. Setup
orchestrator = RUN_TOOL_NODE(storage, capability_service, ...)

# 2. Execute
transaction, result = orchestrator.execute(model_proposal, context)

# 3. Check result
if result.allowed and transaction.state == TransactionState.COMMITTED:
    print("Tool executed successfully and result persisted")
```

## File Map

### Core Implementation (Read in Order)

1. **`src/domain.py`** (5 min read)
   - What: Core data structures
   - Key classes: ActionProposal, ToolTransaction, PolicyDecision, ResultVerification
   - Key enums: TransactionState (13 states), RiskClass, SideEffectClass

2. **`src/state_machine.py`** (5 min read)
   - What: 13-state transaction machine with legal transitions
   - Key class: TransactionStateMachine
   - Key concepts: 22 legal transitions, prohibited paths, terminal states, 6 invariants

3. **`src/storage.py`** (5 min read)
   - What: Database abstraction layer
   - Key class: StorageManager
   - Key models: ToolTransactionORM, TransactionEventORM, DispatchLeaseORM

4. **`src/services.py`** (10 min read)
   - What: 10 core Phase 1 services
   - Key services:
     - CapabilitySnapshotService (ledger management)
     - PreflightService (8 deterministic checks)
     - PolicyDecisionPoint (3-lane approval)
     - CommitMembrane (commit enforcement)

5. **`src/orchestrator.py`** (10 min read)
   - What: RUN_TOOL_NODE orchestration protocol (main entry point)
   - Key class: RUN_TOOL_NODE
   - Key method: execute() (9 sequential steps)

### Testing (Run Tests)

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_orchestration.py -v

# Run with coverage
pytest --cov=src tests/
```

### Documentation

- **`README.md`**: User guide with examples
- **`PROJECT_STATE.md`**: Detailed architecture and status
- **`MEMORY.md`**: Session context for next developer

## Architecture Overview

### Transaction Flow

```
Model Proposal
    ↓
RUN_TOOL_NODE.execute()
    ├─ Ledger Refresh (get current tool registry)
    ├─ Normalize Proposal (validate tool exists)
    ├─ Preflight Check (8 deterministic checks)
    ├─ Policy Evaluation (3-lane approval)
    ├─ Authorize (verify policy signature)
    ├─ Acquire Lease (idempotency lock)
    ├─ Dispatch (invoke tool)
    ├─ Verify Result (check signature & schema)
    └─ Commit (persist to durable state)
    ↓
Result + Transaction State
```

### State Diagram (Simplified)

```
PROPOSED → PREFLIGHTED → AUTHORIZED → DISPATCHED
                                        ↓
                              RETURNED/PENDING_EXTERNAL
                                        ↓
                                    VERIFIED
                                        ↓
                                   COMMITTED (terminal)
```

## Key Guarantees

| Guarantee | How Enforced |
|-----------|--------------|
| No unregistered tool dispatch | ActionNormalizer validates tool in snapshot |
| No stale ledger operations | Every proposal includes ledger snapshot hash |
| No duplicate side effects | Dispatch leases prevent concurrent execution |
| Deterministic state machine | TransactionStateMachine enforces legal transitions |
| Tamper-evident audit | Hash-chained append-only event ledger |
| Idempotent retries | Idempotency key + dispatch lease |
| Policy-driven authorization | PolicyDecisionPoint with 3 approval lanes |
| Result verification | ResultVerifier checks signature & schema |

## Services at a Glance

| Service | Responsibility | Key Method |
|---------|-----------------|------------|
| CapabilitySnapshotService | Ledger management | refresh_snapshot() |
| ActionNormalizer | Proposal validation | normalize() |
| PreflightService | 8 deterministic checks | check() |
| PolicyDecisionPoint | 3-lane approval | evaluate() |
| TransactionCoordinator | State machine | transition() |
| ToolAdapter | Tool invocation boundary | invoke() |
| ResultVerifier | Result validation | verify() |
| CommitMembrane | Commit enforcement | commit() |
| AuditLedger | Tamper-evident logging | append_event() |
| ReconciliationWorker | Async/timeout handling | reconcile() |

## Testing Strategy

### Unit Tests (30+)
Focus: Individual component correctness
- State machine transitions
- Invariant validation
- Service logic

Run: `pytest -m unit`

### Integration Tests (20+)
Focus: Service coordination
- Full transaction lifecycle
- Error scenarios
- Concurrent execution

Run: `pytest -m integration`

### Chaos Tests (20+)
Focus: Failure modes and edge cases
- Storage failures
- Concurrent conflicts
- Property invariants
- Boundary conditions

Run: `pytest -m chaos`

## Common Tasks

### Submit a Transaction

```python
from src.orchestrator import RUN_TOOL_NODE, OrchestrationContext
from uuid import uuid4

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
}

transaction, result = orchestrator.execute(model_proposal, context)
```

### Check Transaction Status

```python
session = storage.get_session()
tx = storage.get_transaction(session, str(transaction.transaction_id))
print(f"State: {tx.state}")
print(f"Created: {tx.created_at}")
session.close()
```

### View Audit Trail

```python
session = storage.get_session()
events = storage.get_events_for_transaction(session, str(transaction.transaction_id))
for event in events:
    print(f"{event.event_type}: {event.from_state} → {event.to_state}")
session.close()
```

### Add a Preflight Check

1. Add check method to PreflightService
2. Call check in PreflightService.check()
3. Return result in PreflightResult
4. Add unit test to test_state_machine.py or test_orchestration.py

### Add a Policy Rule

1. Add evaluation logic to PolicyDecisionPoint.evaluate()
2. Return PolicyDecision with approved=True/False
3. Add test case to test_orchestration.py

## Debugging Tips

### Check State Transition Errors

```python
from src.state_machine import IllegalTransitionError

try:
    next_state, reason = TransactionStateMachine.transition(
        TransactionState.PROPOSED, "invalid_event"
    )
except IllegalTransitionError as e:
    print(f"Invalid transition: {e}")
```

### Verify Service Configuration

```python
# Check what services are available
print(f"Services: {orchestrator.capability_service}, {orchestrator.preflight}, ...")

# Check capability snapshot
snapshot = orchestrator.capability_service.refresh_snapshot("user", "tenant")
print(f"Tools: {snapshot['tools'].keys()}")
print(f"Policies: {snapshot['policies'].keys()}")
```

### Inspect Transaction State

```python
session = storage.get_session()
tx = storage.get_transaction(session, str(tx_id))

print(f"State: {tx.state}")
print(f"Risk: {tx.risk_class}")
print(f"Policy Decision: {tx.policy_decision}")
print(f"Verification: {tx.verification}")
print(f"Result: {tx.result_envelope}")

# Get event history
events = storage.get_events_for_transaction(session, str(tx_id))
for e in events:
    print(f"  {e.created_at}: {e.event_type}")

session.close()
```

## Performance Targets

- Ledger refresh: <5ms
- Preflight checks: <10ms
- Policy evaluation: <50ms
- Dispatch: variable (tool-dependent)
- Verification: <20ms
- Commit: <20ms

**Total overhead**: <150ms per transaction (before tool execution)

## What's NOT Implemented Yet

- [ ] API endpoints (POST /transactions, etc.)
- [ ] Authentication & authorization
- [ ] External connector implementations
- [ ] Observability/telemetry
- [ ] Recovery worker for timeouts
- [ ] Async operation polling

See MEMORY.md for implementation roadmap.

## Next Steps

1. **Read the code** in order: domain → state_machine → storage → services → orchestrator
2. **Run the tests**: `pytest -v` to see how everything works
3. **Try executing a transaction**: See example above
4. **Review PROJECT_STATE.md** for architectural decisions
5. **Start implementing API endpoints** (see MEMORY.md for next session tasks)

## Quick Reference

| Question | Answer |
|----------|--------|
| Where do I submit a transaction? | RUN_TOOL_NODE.execute() |
| What states can a transaction be in? | See TransactionState enum (13 states) |
| How do I prevent duplicate side effects? | Dispatch leases (automatic) |
| How is the audit trail protected? | Hash-chained append-only events |
| Where are policies evaluated? | PolicyDecisionPoint (3-lane system) |
| How are results verified? | ResultVerifier (signature + schema) |
| Where is state validated? | TransactionStateMachine + TransactionInvariants |
| How do I run tests? | `pytest` |
| Where is the database schema? | StorageManager + ORM models |
| What's the main entry point? | RUN_TOOL_NODE.execute() |

## Resources

- **Full README**: See README.md
- **Architecture Details**: See PROJECT_STATE.md
- **Session Context**: See MEMORY.md
- **Specification**: CFA-SHOC v0.2 + Phase 1 Implementation Addendum (referenced in docs)
- **Tests**: 70+ tests in tests/ directory showing all features

## Common Errors & Fixes

| Error | Cause | Fix |
|-------|-------|-----|
| IllegalTransitionError | Invalid state transition | Check current state before calling transition() |
| Tool not available | Tool not in snapshot | Add tool to capability_service.current_snapshot["tools"] |
| Policy denied | Risk too high | Lower risk_class or use Lane B/C |
| Lease acquisition failed | Concurrent execution | Verify previous transaction completed |
| Result verification failed | Signature mismatch | Check ResultVerifier implementation |

---

**Ready to dive in?** Start with `src/domain.py`, then `pytest -v` to see it in action!
