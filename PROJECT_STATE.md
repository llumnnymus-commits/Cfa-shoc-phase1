# CFA-SHOC Phase 1 Project State

**Status:** Core implementation complete. Test suite in place. Ready for integration testing and API layer.

**Date Updated:** 2026-09-30

## Project Purpose

Implement the Capability-Factual, Stateful Hybrid Orchestration and Cognition (CFA-SHOC) Phase 1 system - a deterministic control shell around a probabilistic proposal engine for governed agent execution.

The system enforces:
- **Deterministic state machine** with 13 legally defined transaction states
- **Commit guarantees** preventing unregistered tool dispatch, stale ledger operations, and duplicate side effects
- **Audit trails** with hash-chained append-only event ledgers
- **Idempotency guarantees** via short-lived dispatch leases
- **Policy framework** with three approval lanes (automatic, user-authorized, explicit confirmation)
- **Result verification** through signature validation and schema conformance checking

## Architecture

### Core Components Implemented

#### 1. Domain Models (`src/domain.py` - ~400 lines)
- **TransactionState** enum: 13 legal states per spec section 3.2
  - PROPOSED → PREFLIGHTED → AUTHORIZED → DISPATCHED → RETURNED/PENDING_EXTERNAL → VERIFIED → COMMITTED
  - Terminal states: COMMITTED, COMPENSATED, COMPENSATION_FAILED, FAILED, REJECTED
  - No direct path from PROPOSED → COMMITTED (prohibited by state machine)
  
- **Core dataclasses:**
  - `ActionProposal`: Canonical action from model/planner (never receives credentials)
  - `ToolTransaction`: Durable transaction record (transaction source of truth)
  - `TransactionEvent`: Append-only audit event with hash chaining
  - `PolicyDecision`: Signed policy decision with expiry
  - `DispatchLease`: Short-lived idempotency lock
  - `ResultVerification`: Verification result with signature/schema checks
  - `CommitRequest`: Request to persist verified result
  - `ObservationEnvelope`: Wrapper for tool output with security properties

- **Risk/Side-Effect/Idempotency/Rollback classifications** for action characterization

#### 2. State Machine (`src/state_machine.py` - ~200 lines)
- **TransactionStateMachine** class enforcing legal transitions
  - 22 legal transitions defined in LEGAL_TRANSITIONS dict
  - Prohibited path enforcement: PROPOSED ↛ COMMITTED
  - Terminal state detection
  - Possible transitions query for UI/orchestration

- **TransactionInvariants** class validating spec section 3.3 invariants:
  - COMMITTED implies VERIFIED
  - VERIFIED implies result_envelope exists
  - AUTHORIZED implies valid policy signature
  - DISPATCHED implies idempotency lock held
  - UNKNOWN implies uncertain side-effect status
  - Only COMMITTED displays 'success'

#### 3. Storage Layer (`src/storage.py` - ~300 lines)
- **SQLAlchemy ORM models** for PostgreSQL-first architecture (SQLite in tests)
  - `ToolTransactionORM`: Main transaction table with state machine columns
  - `TransactionEventORM`: Append-only audit table with hash chaining indices
  - `CommitDecisionORM`: Policy decisions cache with expiry
  - `DispatchLeaseORM`: Short-lived dispatch leases
  - `PolicyDecisionORM`: Policy decision cache

- **StorageManager** class providing:
  - Database session lifecycle management
  - Transaction persistence (save/retrieve with version tracking)
  - Append-only event logging
  - Lease acquisition/release (idempotency lock)
  - Reconciliation transaction queries
  - Proper indexes for query performance

#### 4. Services (`src/services.py` - ~700 lines)
10 core Phase 1 services coordinated by orchestrator:

1. **CapabilitySnapshotService**
   - Maintains tool/connector/model/skill/quota/policy state
   - Publishes immutable snapshots with monotonically increasing sequence
   - Snapshot hashing for integrity

2. **ActionNormalizer**
   - Converts model output to canonical ActionProposal
   - Rejects free-form tool calls
   - Validates tool registration

3. **PreflightService**
   - 8 deterministic checks per spec section 3.1:
     1. Ledger currency (snapshot sequence valid)
     2. Tool availability
     3. Connector health
     4. Scope validation
     5. Schema validation
     6. Quota availability
     7. Risk policy compliance
     8. No conflict detection

4. **PolicyDecisionPoint**
   - Evaluates proposals against policy framework
   - Three approval lanes:
     - **Lane A**: Automatic for low-risk, reversible, deterministic operations
     - **Lane B**: User-authorized (pre-configured in contract)
     - **Lane C**: Explicit confirmation required (high-risk operations)
   - Returns signed decisions with expiry timestamps

5. **TransactionCoordinator**
   - Owns transaction state machine
   - Manages idempotency lock (DispatchLease)
   - Orchestrates state transitions

6. **ToolAdapter**
   - Boundary for tool invocation
   - Credential isolation from proposal state
   - Protocol handling (HTTP, gRPC, etc.)

7. **ResultVerifier**
   - Validates result signature
   - Schema conformance checking
   - Consistency validation
   - Returns ResultVerification with evidence

8. **CommitMembrane**
   - Only component permitted to commit verified results
   - Reloads current_ledger at commit time
   - Reloads current_contract at commit time
   - Enforces all preconditions before approving

9. **AuditLedger**
   - Append-only event stream
   - Hash chaining (event_hash includes prior_hash)
   - Tamper evidence
   - Chronological ordering guarantee

10. **ReconciliationWorker**
    - Reconciles UNKNOWN transactions (timeout scenarios)
    - Reconciles PENDING_EXTERNAL transactions (async completion)
    - Provider status queries
    - Compensation logic trigger

#### 5. Orchestrator (`src/orchestrator.py` - ~600 lines)
**RUN_TOOL_NODE orchestration protocol** - Complete transaction lifecycle:

1. **Ledger Refresh**: Get current capability snapshot (tool registration, connector health, policies)
2. **Proposal Normalization**: Convert model output to ActionProposal (validates tool, rejects free-form)
3. **Preflight Check**: 8 deterministic checks against current ledger
4. **Policy Evaluation**: PolicyDecisionPoint yields signed decision
5. **Authorization**: Verify policy signature and check expiry
6. **Lease Acquisition**: Acquire short-lived idempotency lock (prevents duplicate dispatch)
7. **Dispatch**: Tool invocation via ToolAdapter/connector boundary
8. **Result Verification**: Check signature, schema, and consistency
9. **Commit**: CommitMembrane persists verified result to durable state

**OrchestrationContext** class preserves:
- proposal_id, episode_id, task_node_id (tracing)
- principal_id, tenant_id (auth context)
- requested_by (model/user/policy/recovery)

Proper error handling and state transition logging at each step.

## Test Coverage

### Unit Tests (`tests/test_state_machine.py` - 30+ tests)
- State transition legality (22 transitions + prohibited paths)
- Terminal state enforcement
- Invariant validation (6 invariants from spec section 3.3)
- Transition error handling

### Integration Tests (`tests/test_orchestration.py` - 20+ tests)
- Full transaction lifecycle: proposal → normalize → preflight → policy → authorize → lease → dispatch → verify → commit
- Proposal normalization correctness
- Preflight validation passing
- Policy decision framework
- Idempotency key preservation
- Preflight failure blocking progression
- Policy denial rejection
- Dispatch error handling
- Audit trail generation
- Context preservation through execution
- Concurrent lease handling
- State transition sequencing
- Terminal state achievement

### Chaos & Property Tests (`tests/test_chaos_and_properties.py` - 20+ tests)
- **Chaos scenarios**: Storage failures, concurrent access, expiry, hash chaining
- **Property invariants**:
  - No unregistered tool dispatch
  - No double dispatch (idempotency)
  - No jump to COMMITTED without VERIFIED
  - Result verification before commit
  - Monotonic ledger sequences
  - Policy decision expiry
- **Boundary conditions**: Empty arguments, large payloads, far-future deadlines
- **Edge cases**: None optional fields, version counter increments

### Test Fixtures (`tests/conftest.py`)
- In-memory SQLite database (unit/quick tests)
- File-based SQLite database (integration tests)
- Service fixtures (pre-configured services)
- Orchestrator fixture (ready-to-use orchestrator)

### Test Configuration (`pytest.ini`)
- Test discovery configuration
- Marker definitions (unit, integration, chaos, property)
- Coverage settings
- Timeout configuration (30 seconds per test)

## File Structure

```
cfa-shoc-phase1/
├── src/
│   ├── __init__.py
│   ├── domain.py              # Core dataclasses and enums (~400 lines)
│   ├── state_machine.py       # Transaction state machine (~200 lines)
│   ├── storage.py             # SQLAlchemy ORM and StorageManager (~300 lines)
│   ├── services.py            # 10 core Phase 1 services (~700 lines)
│   └── orchestrator.py        # RUN_TOOL_NODE orchestration protocol (~600 lines)
├── tests/
│   ├── __init__.py
│   ├── conftest.py            # Pytest fixtures and configuration
│   ├── test_state_machine.py  # State machine unit tests (30+ tests)
│   ├── test_orchestration.py  # Integration tests (20+ tests)
│   └── test_chaos_and_properties.py  # Chaos and property tests (20+ tests)
├── pyproject.toml             # Project metadata and dependencies
├── pytest.ini                 # Pytest configuration
├── PROJECT_STATE.md           # This file
├── MEMORY.md                  # Session context and decisions
└── README.md                  # User-facing project guide
```

## Completed Work

### Phase 1 Implementation Checklist

✅ **Core Architecture**
- ✅ 13-state transaction state machine
- ✅ All legal state transitions (22 transitions defined)
- ✅ Prohibited path enforcement (PROPOSED ↛ COMMITTED)
- ✅ Terminal state identification
- ✅ Invariant enforcement (6 invariants)

✅ **Storage Layer**
- ✅ PostgreSQL-first schema (works with SQLite in tests)
- ✅ Tool transaction table with state tracking
- ✅ Append-only audit ledger with hash chaining
- ✅ Commit decision cache with expiry
- ✅ Dispatch lease table for idempotency
- ✅ Proper indexing for query performance
- ✅ ORM abstraction via SQLAlchemy

✅ **Service Layer**
- ✅ CapabilitySnapshotService (ledger management)
- ✅ ActionNormalizer (proposal canonicalization)
- ✅ PreflightService (8 deterministic checks)
- ✅ PolicyDecisionPoint (3-lane approval framework)
- ✅ TransactionCoordinator (state machine owner)
- ✅ ToolAdapter (credential isolation boundary)
- ✅ ResultVerifier (result validation)
- ✅ CommitMembrane (commit enforcement)
- ✅ AuditLedger (tamper-evident logging)
- ✅ ReconciliationWorker (async/timeout handling)

✅ **Orchestration**
- ✅ RUN_TOOL_NODE protocol (9 orchestration steps)
- ✅ OrchestrationContext (tracing and auth context)
- ✅ Complete lifecycle: proposal → normalize → preflight → policy → authorize → lease → dispatch → verify → commit
- ✅ Error handling and rollback
- ✅ Audit event logging at each step
- ✅ State transition validation

✅ **Testing**
- ✅ State machine unit tests (30+ tests)
- ✅ Orchestration integration tests (20+ tests)
- ✅ Chaos and property tests (20+ tests)
- ✅ Test fixtures and configuration
- ✅ Edge case coverage

## Remaining Work

### High Priority

1. **API Endpoints** (Phase 1 completion)
   - POST /transactions (submit tool request)
   - GET /transactions/{id} (status query)
   - GET /transactions/{id}/events (audit trail)
   - POST /transactions/{id}/authorize (Lane C confirmation)
   - POST /transactions/reconcile (manual reconciliation)

2. **Recovery & Compensation** (Phase 1 completion)
   - Timeout recovery (DISPATCHED → UNKNOWN → reconciliation)
   - Async completion polling (PENDING_EXTERNAL → VERIFIED)
   - Compensation logic for UNKNOWN → COMPENSATION_REQUIRED
   - Manual compensation operator UI

3. **Observability** (Phase 1 completion)
   - OpenTelemetry instrumentation
   - Traces keyed by transaction_id and episode_id
   - Metrics: state transition latencies, policy decision timing
   - Structured logging with JSON output

4. **Migration Scripts**
   - Create database schema for fresh deployment
   - Migration from SQLite (testing) to PostgreSQL (production)

### Medium Priority

5. **Authentication & Authorization**
   - Principal verification (tenant isolation)
   - Scope validation for principal
   - Rate limiting per principal/tenant

6. **Configuration Management**
   - Environment-based configuration (dev/staging/prod)
   - Policy definition loading
   - Tool registry loading from external source

7. **Connector Implementations**
   - HTTP/gRPC connector adapters
   - Credential management per connector
   - Timeout and retry logic

### Low Priority (Phase 2+)

8. **Advanced Features**
   - Compensation strategies (full rollback, partial compensation)
   - Complex policy rule engine
   - Multi-step transactions
   - Transaction composition

9. **Performance Optimization**
   - Connection pooling
   - Query optimization
   - Caching strategies
   - Batch operations

10. **Documentation**
    - API specification (OpenAPI)
    - Architecture decision records (ADRs)
    - Operator runbooks
    - Troubleshooting guide

## Key Design Decisions

1. **Durable State**: Tool transactions are source of truth, persisted before any side effects
2. **No Unregistered Dispatch**: ActionNormalizer and PreflightService validate tool existence
3. **Ledger Snapshot**: Every proposal creates ledger snapshot to detect stale operations
4. **Idempotency Locks**: Short-lived dispatch leases prevent concurrent invocations
5. **Append-Only Audit**: Hash-chained events provide tamper evidence
6. **Policy Lanes**: 3-tier approval (automatic/user/explicit) balances usability and control
7. **Service Isolation**: Credential boundary enforced via ToolAdapter
8. **Invariant Enforcement**: Spec 3.3 invariants validated at commit time

## Running Tests

```bash
# Install dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run with coverage
pytest --cov=src tests/

# Run specific test category
pytest -m unit           # Unit tests only
pytest -m integration    # Integration tests only
pytest -m chaos         # Chaos tests only

# Run specific test file
pytest tests/test_state_machine.py -v

# Run with timeout (30 seconds per test)
pytest --timeout=30
```

## Next Immediate Steps

1. Implement API endpoints for transaction submission and status queries
2. Create database migration scripts for PostgreSQL deployment
3. Add OpenTelemetry instrumentation for observability
4. Implement timeout recovery and async completion polling
5. Build operator dashboard for manual reconciliation

## Dependencies

- **pydantic>=2.0**: Data validation
- **sqlalchemy>=2.0**: ORM for database abstraction
- **python-dateutil>=2.8**: Date/time utilities
- **pytest>=7.0**: Testing framework
- **pytest-cov>=4.0**: Coverage reporting
- **black>=23.0**: Code formatting
- **ruff>=0.1**: Linting
- **mypy>=1.0**: Static type checking

## Notes for Next Session

- Core Phase 1 implementation is complete and well-tested
- Focus on API layer and integration with external systems
- Orchestrator is the main entry point for transaction execution
- StorageManager abstracts database layer (ready for PostgreSQL migration)
- All service dependencies are clearly defined for dependency injection
