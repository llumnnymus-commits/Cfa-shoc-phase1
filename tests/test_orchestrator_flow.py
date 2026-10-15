"""
End-to-end RUN_TOOL_NODE flows (handoff step 5: D5, D8, D9).

Each test asserts the exact end state, that the durable record agrees with the in-memory
transaction, and the audit trail.
"""

from uuid import uuid4

import pytest

from cfa_shoc.domain import TransactionState
from cfa_shoc.orchestrator import OrchestrationContext, RUN_TOOL_NODE
from cfa_shoc.services import (
    ActionNormalizer, AuditLedger, CapabilitySnapshotService, CommitMembrane,
    PolicyDecisionPoint, PreflightService, ResultVerifier, ToolAdapter,
)
from cfa_shoc.storage import CommitDecisionORM, LeaseConflictError, StorageManager


class Env:
    """A fully wired orchestrator with tool_echo registered and a call counter."""

    def __init__(self):
        self.storage = StorageManager("sqlite:///:memory:")
        self.storage.init_db()
        self.capability = CapabilitySnapshotService()
        self.capability.update_tool_state("tool_echo", {"id": "tool_echo", "scopes": []})
        self.adapter = ToolAdapter()
        self.calls = []
        self.adapter.register("tool_echo", self._echo)
        self.audit = AuditLedger()
        self.orchestrator = RUN_TOOL_NODE(
            storage=self.storage,
            capability_service=self.capability,
            normalizer=ActionNormalizer(self.capability),
            preflight=PreflightService(self.capability),
            policy_point=PolicyDecisionPoint(),
            tool_adapter=self.adapter,
            verifier=ResultVerifier(),
            commit_membrane=CommitMembrane(),
            audit_ledger=self.audit,
        )

    def _echo(self, request):
        self.calls.append(request)
        return {"status": "succeeded", "output": {"echo": request.arguments.get("message")}}

    def context(self, **kwargs):
        return OrchestrationContext(
            proposal_id=uuid4(), episode_id=uuid4(), task_node_id=uuid4(),
            principal_id="user_1", tenant_id="tenant_1", requested_by="model", **kwargs,
        )

    def run(self, proposal, context=None):
        return self.orchestrator.execute(proposal, context or self.context())

    def stored(self, transaction):
        session = self.storage.get_session()
        try:
            row = self.storage.get_transaction(session, str(transaction.transaction_id))
            events = self.storage.get_events_for_transaction(session, str(transaction.transaction_id))
            return row, [e.event_type for e in events]
        finally:
            session.close()


ECHO_LOW = {
    "tool_id": "tool_echo",
    "operation": "echo",
    "arguments": {"message": "hello"},
    "risk_class": "low",
    "side_effect_class": "none",
}


@pytest.fixture
def env():
    return Env()


def test_low_risk_echo_commits_with_full_event_trail(env):
    transaction, result = env.run(ECHO_LOW)

    assert result.allowed, result.reason
    assert transaction.state == TransactionState.COMMITTED
    assert transaction.result_envelope["observation"]["content"] == {"echo": "hello"}
    assert len(env.calls) == 1

    row, events = env.stored(transaction)
    assert row.state == "COMMITTED"
    assert events == [
        "orchestration_started",
        "preflight_passed",
        "policy_approved",
        "dispatch_started",
        "response_received",
        "verification_passed",
        "action_commit_approved",
    ]

    session = env.storage.get_session()
    try:
        decisions = session.query(CommitDecisionORM).filter(
            CommitDecisionORM.transaction_id == str(transaction.transaction_id)
        ).all()
        assert len(decisions) == 1 and decisions[0].decision == "APPROVE"
    finally:
        session.close()


def test_medium_risk_call_is_rejected_by_policy_and_never_dispatched(env):
    transaction, result = env.run({**ECHO_LOW, "risk_class": "medium"})

    assert not result.allowed
    assert "Policy denied" in result.reason
    assert transaction.state == TransactionState.REJECTED
    assert env.calls == []
    row, events = env.stored(transaction)
    assert row.state == "REJECTED"
    assert events == ["orchestration_started", "preflight_passed", "policy_denied"]


def test_unregistered_tool_is_refused_before_dispatch(env):
    transaction, result = env.run({"tool_id": "tool_missing", "operation": "op", "risk_class": "low"})

    assert not result.allowed
    assert "not registered" in result.reason
    assert transaction.state == TransactionState.REJECTED
    assert env.calls == []
    row, events = env.stored(transaction)
    assert row.state == "REJECTED"
    assert events == ["orchestration_started", "preflight_failed"]


def test_context_ids_reach_the_transaction(env):
    context = env.context()
    transaction, _ = env.run(ECHO_LOW, context)
    assert transaction.proposal_id == context.proposal_id
    assert transaction.episode_id == context.episode_id
    assert transaction.task_node_id == context.task_node_id
    assert transaction.principal_id == "user_1"
    assert transaction.tenant_id == "tenant_1"


def test_ledger_hash_is_content_addressed(env):
    first = env.capability.refresh_snapshot("user_1", "tenant_1")
    second = env.capability.refresh_snapshot("user_1", "tenant_1")
    assert first["hash"] == second["hash"]  # time passed, content did not change

    env.capability.update_tool_state("tool_new", {"id": "tool_new"})
    third = env.capability.refresh_snapshot("user_1", "tenant_1")
    assert third["hash"] != second["hash"]
    assert third["content_version"] == second["content_version"] + 1


def test_snapshot_is_not_altered_by_later_registry_edits(env):
    snapshot = env.capability.refresh_snapshot("user_1", "tenant_1")
    env.capability.update_tool_state("tool_late", {"id": "tool_late"})
    assert "tool_late" not in snapshot["tools"]


def test_ledger_change_during_dispatch_blocks_commit(env):
    def echo_then_change_ledger(request):
        env.capability.update_tool_state("tool_surprise", {"id": "tool_surprise"})
        return {"status": "succeeded", "output": {"ok": True}}

    env.adapter.register("tool_echo", echo_then_change_ledger)
    transaction, result = env.run(ECHO_LOW)

    assert not result.allowed
    assert transaction.state == TransactionState.REJECTED
    row, events = env.stored(transaction)
    assert row.state == "REJECTED"
    assert events[-1] == "action_commit_denied"


def test_contract_change_during_dispatch_blocks_commit(env):
    context = env.context()
    env.capability.register_contract(context.episode_id, {"scopes": ["read"]})

    def echo_then_revoke(request):
        env.capability.register_contract(context.episode_id, {"scopes": []})
        return {"status": "succeeded", "output": {"ok": True}}

    env.adapter.register("tool_echo", echo_then_revoke)
    transaction, result = env.run(ECHO_LOW, context)

    assert transaction.state == TransactionState.REJECTED
    assert env.stored(transaction)[1][-1] == "action_commit_denied"


def test_connector_error_ends_failed(env):
    def broken(request):
        raise ConnectionError("upstream down")

    env.adapter.register("tool_echo", broken)
    transaction, result = env.run(ECHO_LOW)

    assert not result.allowed
    assert transaction.state == TransactionState.FAILED
    row, events = env.stored(transaction)
    assert row.state == "FAILED"
    assert events[-1] == "connector_error"


def test_tool_without_handler_fails_closed(env):
    env.capability.update_tool_state("tool_nohandler", {"id": "tool_nohandler"})
    transaction, result = env.run({**ECHO_LOW, "tool_id": "tool_nohandler"})
    assert transaction.state == TransactionState.FAILED
    assert not result.allowed


def test_timeout_ends_unknown(env):
    def slow(request):
        raise TimeoutError("no answer within deadline")

    env.adapter.register("tool_echo", slow)
    transaction, result = env.run(ECHO_LOW)

    assert transaction.state == TransactionState.UNKNOWN
    assert env.stored(transaction)[0].state == "UNKNOWN"


def test_async_acceptance_ends_pending_external(env):
    env.adapter.register(
        "tool_echo", lambda request: {"status": "accepted", "external_operation_id": "op-42"}
    )
    transaction, result = env.run(ECHO_LOW)

    assert result.allowed
    assert transaction.state == TransactionState.PENDING_EXTERNAL
    assert transaction.external_operation_id == "op-42"
    row, _ = env.stored(transaction)
    assert row.state == "PENDING_EXTERNAL"
    assert row.external_operation_id == "op-42"


def test_lease_conflict_revokes_authorization(env, monkeypatch):
    def conflict(session, lease_data):
        raise LeaseConflictError("held by someone else")

    monkeypatch.setattr(env.storage, "acquire_lease", conflict)
    transaction, result = env.run(ECHO_LOW)

    assert not result.allowed
    assert transaction.state == TransactionState.REJECTED
    assert env.calls == []
    assert env.stored(transaction)[1][-1] == "authorization_revoked"


def test_durable_state_matches_memory_after_every_scenario(env):
    for proposal in (ECHO_LOW, {**ECHO_LOW, "risk_class": "high"}, {"tool_id": "x", "operation": "y"}):
        transaction, _ = env.run(proposal)
        assert env.stored(transaction)[0].state == transaction.state.value
