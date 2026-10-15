"""
Integration tests for RUN_TOOL_NODE orchestration protocol.

Covers:
- Full transaction lifecycle from proposal to commit
- Service coordination and state transitions
- Error handling and rollback scenarios
- Idempotency guarantees
"""

import pytest
from datetime import datetime
from uuid import uuid4
from src.domain import (
    ActionProposal, PreflightResult, RiskClass, SideEffectClass, RollbackStrategy,
    TransactionState, DispatchRequest, RawResult, ResultVerification,
)
from src.storage import StorageManager
from src.state_machine import TransactionStateMachine
from src.services import (
    CapabilitySnapshotService, ActionNormalizer, PreflightService, PolicyDecisionPoint,
    TransactionCoordinator, ToolAdapter, ResultVerifier, CommitMembrane, AuditLedger,
)
from src.orchestrator import RUN_TOOL_NODE, OrchestrationContext


@pytest.fixture
def storage():
    """In-memory SQLite storage for testing."""
    manager = StorageManager("sqlite:///:memory:")
    manager.init_db()
    return manager


TOOLS_USED = [
    "tool_echo", "tool_calc", "tool_read", "tool_safe", "tool_idempotent", "tool_dangerous",
    "tool_audit", "tool_context", "tool_dedup", "tool_seq", "tool_terminal",
]


@pytest.fixture
def services(storage):
    """Create all services for orchestration, sharing one capability service."""
    capability = CapabilitySnapshotService()
    capability.update_tool_state("tool_echo", {"id": "tool_echo"})

    adapter = ToolAdapter()
    adapter.calls = []

    def echo(request):
        adapter.calls.append(request)
        return {"status": "succeeded", "output": {"tool": request.tool_id, "args": request.arguments}}

    def failing(request):
        adapter.calls.append(request)
        raise ConnectionError("connector down")

    for tool_id in TOOLS_USED:
        adapter.register(tool_id, echo)
    adapter.register("tool_failing", failing)

    return {
        "capability": capability,
        "normalizer": ActionNormalizer(capability),
        "preflight": PreflightService(capability),
        "policy": PolicyDecisionPoint(),
        "coordinator": TransactionCoordinator(),
        "adapter": adapter,
        "verifier": ResultVerifier(),
        "commit": CommitMembrane(),
        "audit": AuditLedger(),
    }


def stored_events(orchestrator, transaction):
    """Event types stored for a transaction, in order."""
    session = orchestrator.storage.get_session()
    try:
        events = orchestrator.storage.get_events_for_transaction(session, str(transaction.transaction_id))
        return [e.event_type for e in events]
    finally:
        session.close()


@pytest.fixture
def orchestrator(storage, services):
    """Create orchestrator instance."""
    return RUN_TOOL_NODE(
        storage=storage,
        capability_service=services["capability"],
        normalizer=services["normalizer"],
        preflight=services["preflight"],
        policy_point=services["policy"],
        tool_adapter=services["adapter"],
        verifier=services["verifier"],
        commit_membrane=services["commit"],
        audit_ledger=services["audit"],
    )


class TestOrchestrationSuccess:
    """Tests for successful orchestration scenarios."""

    def test_full_transaction_lifecycle(self, orchestrator, services):
        """Complete transaction from proposal to commit."""
        # Setup
        services["capability"].update_tool_state("tool_echo", {"id": "tool_echo", "version": "1.0"})
        services["capability"].current_snapshot["tools"]["tool_echo"] = {
            "id": "tool_echo",
            "scopes": ["read"],
        }

        context = OrchestrationContext(
            proposal_id=uuid4(),
            episode_id=uuid4(),
            task_node_id=uuid4(),
            principal_id="user_123",
            tenant_id="tenant_456",
            requested_by="user",
            contract={"scopes": ["read"]},  # tool_echo requires the "read" scope
        )

        model_proposal = {
            "tool_id": "tool_echo",
            "operation": "echo",
            "arguments": {"message": "hello"},
            "risk_class": "low",
            "side_effect_class": "none",
        }

        # Execute orchestration
        transaction, result = orchestrator.execute(model_proposal, context)

        # Verify transaction state
        assert result.allowed, result.reason
        assert transaction.state == TransactionState.COMMITTED
        assert stored_events(orchestrator, transaction) == [
            "orchestration_started", "preflight_passed", "policy_approved", "dispatch_started",
            "response_received", "verification_passed", "action_commit_approved",
        ]
        assert transaction.tool_id == "tool_echo"
        assert transaction.operation == "echo"
        assert transaction.principal_id == "user_123"
        assert transaction.tenant_id == "tenant_456"

    def test_proposal_normalization(self, orchestrator, services):
        """ActionProposal correctly normalized from model output."""
        services["capability"].update_tool_state("tool_calc", {"id": "tool_calc"})
        services["capability"].current_snapshot["tools"]["tool_calc"] = {"id": "tool_calc"}

        context = OrchestrationContext(
            proposal_id=uuid4(),
            episode_id=uuid4(),
            task_node_id=uuid4(),
            principal_id="user_123",
            tenant_id="tenant_456",
            requested_by="model",
        )

        model_proposal = {
            "tool_id": "tool_calc",
            "operation": "add",
            "arguments": {"a": 5, "b": 3},
            "risk_class": "low",
            "side_effect_class": "none",
        }

        transaction, result = orchestrator.execute(model_proposal, context)
        assert transaction.tool_id == "tool_calc"
        assert transaction.operation == "add"
        assert transaction.input_payload["a"] == 5
        assert transaction.state == TransactionState.COMMITTED

    def test_preflight_checks_pass(self, orchestrator, services):
        """Preflight validation passes for valid proposal."""
        services["capability"].current_snapshot["tools"]["tool_read"] = {
            "id": "tool_read",
            "version": "1.0",
        }
        services["capability"].current_snapshot["connectors"]["http"] = {
            "id": "http",
            "health": "healthy",
        }

        context = OrchestrationContext(
            proposal_id=uuid4(),
            episode_id=uuid4(),
            task_node_id=uuid4(),
            principal_id="user_123",
            tenant_id="tenant_456",
            requested_by="user",
        )

        model_proposal = {
            "tool_id": "tool_read",
            "operation": "get",
            "arguments": {"key": "value"},
            "connector_id": "http",
            "risk_class": "low",
            "side_effect_class": "none",
        }

        transaction, result = orchestrator.execute(model_proposal, context)
        # Healthy connector, registered tool, low risk: passes preflight and commits
        assert result.allowed, result.reason
        assert "preflight_passed" in stored_events(orchestrator, transaction)
        assert transaction.state == TransactionState.COMMITTED

    def test_policy_decision_framework(self, orchestrator, services):
        """Policy decision point evaluates proposals."""
        services["capability"].current_snapshot["tools"]["tool_safe"] = {"id": "tool_safe"}

        context = OrchestrationContext(
            proposal_id=uuid4(),
            episode_id=uuid4(),
            task_node_id=uuid4(),
            principal_id="user_123",
            tenant_id="tenant_456",
            requested_by="user",
        )

        model_proposal = {
            "tool_id": "tool_safe",
            "operation": "safe_op",
            "arguments": {},
            "risk_class": "low",
            "side_effect_class": "reversible",
        }

        transaction, result = orchestrator.execute(model_proposal, context)
        # Policy point evaluated the proposal: low risk + reversible is approved (lane A)
        assert transaction.tool_id == "tool_safe"
        assert transaction.policy_decision["approved"] is True
        assert transaction.state == TransactionState.COMMITTED

    def test_idempotency_key_preservation(self, orchestrator, services):
        """Idempotency key is preserved through transaction."""
        services["capability"].current_snapshot["tools"]["tool_idempotent"] = {
            "id": "tool_idempotent"
        }

        idempotency_key = "idem_abc123_test"
        context = OrchestrationContext(
            proposal_id=uuid4(),
            episode_id=uuid4(),
            task_node_id=uuid4(),
            principal_id="user_123",
            tenant_id="tenant_456",
            requested_by="user",
        )

        model_proposal = {
            "tool_id": "tool_idempotent",
            "operation": "op",
            "arguments": {},
            "idempotency_key": idempotency_key,
        }

        transaction, result = orchestrator.execute(model_proposal, context)
        assert transaction.idempotency_key == idempotency_key
        # No risk_class given: default MEDIUM is denied by policy
        assert transaction.state == TransactionState.REJECTED
        session = orchestrator.storage.get_session()
        try:
            row = orchestrator.storage.get_transaction(session, str(transaction.transaction_id))
            assert row.idempotency_key == idempotency_key
        finally:
            session.close()


class TestOrchestrationFailures:
    """Tests for failure and error scenarios."""

    def test_preflight_failure_blocks_progression(self, orchestrator, services):
        """Preflight check failure prevents authorization."""
        # Tool not available in snapshot
        context = OrchestrationContext(
            proposal_id=uuid4(),
            episode_id=uuid4(),
            task_node_id=uuid4(),
            principal_id="user_123",
            tenant_id="tenant_456",
            requested_by="user",
        )

        model_proposal = {
            "tool_id": "tool_missing",
            "operation": "op",
            "arguments": {},
        }

        transaction, result = orchestrator.execute(model_proposal, context)
        # Refused before authorization: never dispatched
        assert not result.allowed
        assert transaction.state == TransactionState.REJECTED
        assert "policy_approved" not in stored_events(orchestrator, transaction)
        assert services["adapter"].calls == []

    def test_policy_denial_rejects_transaction(self, orchestrator, services):
        """Policy denial transitions to REJECTED state."""
        # Setup tool
        services["capability"].current_snapshot["tools"]["tool_dangerous"] = {
            "id": "tool_dangerous",
            "risk": "critical",
        }

        context = OrchestrationContext(
            proposal_id=uuid4(),
            episode_id=uuid4(),
            task_node_id=uuid4(),
            principal_id="user_123",
            tenant_id="tenant_456",
            requested_by="user",
        )

        model_proposal = {
            "tool_id": "tool_dangerous",
            "operation": "critical_op",
            "arguments": {},
            "risk_class": "critical",
        }

        transaction, result = orchestrator.execute(model_proposal, context)
        # Policy point denies critical operations by default
        assert transaction.state == TransactionState.REJECTED
        assert stored_events(orchestrator, transaction)[-1] == "policy_denied"
        assert services["adapter"].calls == []

    def test_dispatch_failure_on_connector_error(self, orchestrator, services):
        """Tool dispatch failure is handled gracefully."""
        services["capability"].current_snapshot["tools"]["tool_failing"] = {
            "id": "tool_failing"
        }
        services["capability"].current_snapshot["connectors"]["failing_connector"] = {
            "id": "failing_connector",
            "health": "unhealthy",
        }

        context = OrchestrationContext(
            proposal_id=uuid4(),
            episode_id=uuid4(),
            task_node_id=uuid4(),
            principal_id="user_123",
            tenant_id="tenant_456",
            requested_by="user",
        )

        model_proposal = {
            "tool_id": "tool_failing",
            "operation": "fail",
            "arguments": {},
            "connector_id": "failing_connector",
        }

        transaction, result = orchestrator.execute(model_proposal, context)
        # The unhealthy connector is caught at preflight, before any dispatch
        assert transaction.tool_id == "tool_failing"
        assert not result.allowed
        assert transaction.state == TransactionState.REJECTED
        assert "not healthy" in result.reason
        assert services["adapter"].calls == []

        # With a healthy connector the dispatch itself fails: FAILED
        services["capability"].current_snapshot["connectors"]["failing_connector"]["health"] = "healthy"
        context2 = OrchestrationContext(
            proposal_id=uuid4(), episode_id=uuid4(), task_node_id=uuid4(),
            principal_id="user_123", tenant_id="tenant_456", requested_by="user",
        )
        transaction2, result2 = orchestrator.execute(
            {**model_proposal, "risk_class": "low", "idempotency_key": "fail-2"}, context2
        )
        assert not result2.allowed
        assert transaction2.state == TransactionState.FAILED
        assert stored_events(orchestrator, transaction2)[-1] == "connector_error"

    def test_no_direct_proposed_to_committed(self, orchestrator):
        """Constraint: Cannot jump directly from PROPOSED to COMMITTED."""
        # This is enforced by state machine - the orchestration follows legal transitions
        assert not TransactionStateMachine.is_legal_transition(
            TransactionState.PROPOSED, TransactionState.COMMITTED, "invalid_event"
        )


class TestAuditAndObservability:
    """Tests for audit trail and event logging."""

    def test_transaction_events_logged(self, orchestrator, services):
        """Each state transition generates audit event."""
        services["capability"].current_snapshot["tools"]["tool_audit"] = {
            "id": "tool_audit"
        }

        context = OrchestrationContext(
            proposal_id=uuid4(),
            episode_id=uuid4(),
            task_node_id=uuid4(),
            principal_id="user_123",
            tenant_id="tenant_456",
            requested_by="user",
        )

        model_proposal = {
            "tool_id": "tool_audit",
            "operation": "op",
            "arguments": {},
        }

        transaction, result = orchestrator.execute(model_proposal, context)

        # Verify transaction has been created with at least initial event
        session = orchestrator.storage.get_session()
        try:
            tx_record = orchestrator.storage.get_transaction(session, str(transaction.transaction_id))
            assert tx_record is not None
            # Default MEDIUM risk is denied; the stored state matches
            assert tx_record.state == TransactionState.REJECTED.value
            assert tx_record.state == transaction.state.value
        finally:
            session.close()
        # One event per transition, in order
        assert stored_events(orchestrator, transaction) == [
            "orchestration_started", "preflight_passed", "policy_denied",
        ]

    def test_transaction_context_preserved(self, orchestrator, services):
        """Orchestration context preserved through execution."""
        principal = "principal_xyz"
        tenant = "tenant_xyz"
        episode = uuid4()
        task_node = uuid4()

        services["capability"].current_snapshot["tools"]["tool_context"] = {
            "id": "tool_context"
        }

        context = OrchestrationContext(
            proposal_id=uuid4(),
            episode_id=episode,
            task_node_id=task_node,
            principal_id=principal,
            tenant_id=tenant,
            requested_by="user",
        )

        model_proposal = {
            "tool_id": "tool_context",
            "operation": "op",
            "arguments": {},
        }

        transaction, result = orchestrator.execute(model_proposal, context)

        # Verify context is preserved
        assert transaction.principal_id == principal
        assert transaction.tenant_id == tenant
        assert transaction.episode_id == episode
        assert transaction.task_node_id == task_node


class TestConcurrency:
    """Tests for concurrent execution and idempotency."""

    def test_duplicate_proposal_uses_same_transaction(self, orchestrator, services):
        """Duplicate proposals with same idempotency key are deduplicated."""
        services["capability"].current_snapshot["tools"]["tool_dedup"] = {
            "id": "tool_dedup"
        }

        idempotency_key = "dedup_key_12345"
        context1 = OrchestrationContext(
            proposal_id=uuid4(),
            episode_id=uuid4(),
            task_node_id=uuid4(),
            principal_id="user_123",
            tenant_id="tenant_456",
            requested_by="user",
        )

        model_proposal = {
            "tool_id": "tool_dedup",
            "operation": "op",
            "arguments": {},
            "idempotency_key": idempotency_key,
            "risk_class": "low",
        }

        transaction1, result1 = orchestrator.execute(model_proposal, context1)
        assert transaction1.idempotency_key == idempotency_key
        assert transaction1.state == TransactionState.COMMITTED

        # Second execution with same idempotency key should be handled by lease logic
        context2 = OrchestrationContext(
            proposal_id=uuid4(),
            episode_id=uuid4(),
            task_node_id=uuid4(),
            principal_id="user_123",
            tenant_id="tenant_456",
            requested_by="user",
        )

        transaction2, result2 = orchestrator.execute(model_proposal, context2)
        # Second is an idempotent replay: same transaction, no second dispatch
        assert transaction2.idempotency_key == idempotency_key
        assert transaction2.transaction_id == transaction1.transaction_id
        assert transaction2.state == TransactionState.COMMITTED
        assert result2.allowed and "Idempotent replay" in result2.reason
        assert len(services["adapter"].calls) == 1


class TestStateTransitionSequence:
    """Tests for proper state transition ordering."""

    def test_transition_sequence_proposed_to_preflighted(self, orchestrator, services):
        """Verify PROPOSED -> PREFLIGHTED -> AUTHORIZED progression."""
        services["capability"].current_snapshot["tools"]["tool_seq"] = {"id": "tool_seq"}

        context = OrchestrationContext(
            proposal_id=uuid4(),
            episode_id=uuid4(),
            task_node_id=uuid4(),
            principal_id="user_123",
            tenant_id="tenant_456",
            requested_by="user",
        )

        model_proposal = {
            "tool_id": "tool_seq",
            "operation": "op",
            "arguments": {},
            "risk_class": "low",
            "side_effect_class": "reversible",
        }

        transaction, result = orchestrator.execute(model_proposal, context)

        # Starts in PROPOSED, then PREFLIGHTED, then AUTHORIZED, then onward to COMMITTED
        session = orchestrator.storage.get_session()
        try:
            events = orchestrator.storage.get_events_for_transaction(session, str(transaction.transaction_id))
            path = [(e.from_state, e.to_state) for e in events]
        finally:
            session.close()
        assert path[:3] == [
            (None, "PROPOSED"),
            ("PROPOSED", "PREFLIGHTED"),
            ("PREFLIGHTED", "AUTHORIZED"),
        ]
        assert transaction.state == TransactionState.COMMITTED

    def test_terminal_state_reached_on_success(self, orchestrator, services):
        """Successful execution reaches terminal state (COMMITTED or equivalent)."""
        services["capability"].current_snapshot["tools"]["tool_terminal"] = {
            "id": "tool_terminal"
        }

        context = OrchestrationContext(
            proposal_id=uuid4(),
            episode_id=uuid4(),
            task_node_id=uuid4(),
            principal_id="user_123",
            tenant_id="tenant_456",
            requested_by="user",
        )

        model_proposal = {
            "tool_id": "tool_terminal",
            "operation": "op",
            "arguments": {},
            "risk_class": "low",  # successful execution requires an approvable proposal
        }

        transaction, result = orchestrator.execute(model_proposal, context)

        # Successful execution ends COMMITTED, which is terminal
        assert transaction.state == TransactionState.COMMITTED
        assert TransactionStateMachine.is_terminal(transaction.state)
