"""
Durable audit chain and reconciliation (handoff step 9: D10).
"""

from datetime import timedelta

import pytest
from sqlalchemy import text

from cfa_shoc.domain import TransactionState, utcnow
from cfa_shoc.services import ReconciliationWorker

from tests._env import Env

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


def _sql(env, statement, **params):
    with env.storage.engine.begin() as connection:
        connection.execute(text(statement), params)


# ---------------------------------------------------------------- durable hash chain


def test_committed_transaction_has_an_intact_stored_chain(env):
    transaction, _ = env.run(ECHO_LOW)
    assert env.storage.verify_chain(transaction.transaction_id) == (True, None)

    session = env.storage.get_session()
    try:
        events = env.storage.get_events_for_transaction(session, str(transaction.transaction_id))
        assert [e.sequence for e in events] == list(range(7))
        assert events[0].prior_hash is None
        for previous, current in zip(events, events[1:]):
            assert current.prior_hash == previous.event_hash
    finally:
        session.close()


def test_in_memory_mirror_matches_storage(env):
    transaction, _ = env.run(ECHO_LOW)
    mirror = env.audit.get_for_transaction(transaction.transaction_id)
    session = env.storage.get_session()
    try:
        stored = env.storage.get_events_for_transaction(session, str(transaction.transaction_id))
        assert [e.event_hash for e in mirror] == [e.event_hash for e in stored]
    finally:
        session.close()
    assert env.audit.verify(transaction.transaction_id) == (True, None)


def test_editing_a_stored_payload_is_detected(env):
    transaction, _ = env.run(ECHO_LOW)
    _sql(env, "UPDATE transaction_events SET payload = :p WHERE transaction_id = :t AND sequence = 3",
         p='{"reason": "edited"}', t=str(transaction.transaction_id))
    assert env.storage.verify_chain(transaction.transaction_id) == (False, 3)


def test_editing_a_state_is_detected(env):
    transaction, _ = env.run({**ECHO_LOW, "risk_class": "high"})  # REJECTED by policy
    _sql(env, "UPDATE transaction_events SET to_state = 'AUTHORIZED' WHERE transaction_id = :t AND sequence = 2",
         t=str(transaction.transaction_id))
    assert env.storage.verify_chain(transaction.transaction_id) == (False, 2)


def test_recomputing_an_edited_hash_breaks_the_next_link(env):
    transaction, _ = env.run(ECHO_LOW)
    _sql(env, "UPDATE transaction_events SET event_hash = :h WHERE transaction_id = :t AND sequence = 1",
         h="0" * 64, t=str(transaction.transaction_id))
    ok, index = env.storage.verify_chain(transaction.transaction_id)
    assert not ok and index == 1


def test_deleting_an_event_is_detected(env):
    transaction, _ = env.run(ECHO_LOW)
    _sql(env, "DELETE FROM transaction_events WHERE transaction_id = :t AND sequence = 4",
         t=str(transaction.transaction_id))
    assert env.storage.verify_chain(transaction.transaction_id) == (False, 4)


def test_caller_supplied_hashes_are_ignored(env):
    transaction, _ = env.run(ECHO_LOW)
    session = env.storage.get_session()
    try:
        event = env.storage.append_event(session, {
            "transaction_id": str(transaction.transaction_id), "event_type": "note",
            "actor_type": "user", "actor_id": "u1", "payload": {},
            "prior_hash": "forged", "event_hash": "forged", "sequence": 99,
        })
        assert event.sequence == 7 and event.event_hash != "forged"
    finally:
        session.close()
    assert env.storage.verify_chain(transaction.transaction_id) == (True, None)


def test_chains_are_per_transaction(env):
    first, _ = env.run({**ECHO_LOW, "idempotency_key": "a"})
    second, _ = env.run({**ECHO_LOW, "idempotency_key": "b"})
    _sql(env, "UPDATE transaction_events SET payload = '{}' WHERE transaction_id = :t AND sequence = 2",
         t=str(first.transaction_id))
    assert env.storage.verify_chain(first.transaction_id) == (False, 2)
    assert env.storage.verify_chain(second.transaction_id) == (True, None)


# ---------------------------------------------------------------- reconciliation


class FakeProvider:
    """Answers status queries with whatever `status` is set to."""

    def __init__(self, status="unknown", result=None):
        self.status = status
        self.result = result
        self.queries = 0

    def __call__(self, tool_id, operation_id):
        self.queries += 1
        return {"status": self.status, "result": self.result}


def _worker(env):
    return ReconciliationWorker(env.adapter, env.orchestrator.verifier, orchestrator=env.orchestrator)


def _pending(env, provider, **proposal):
    env.adapter.register(
        "tool_echo",
        lambda r: {"status": "accepted", "external_operation_id": "op-1"},
        status_handler=provider,
    )
    transaction, _ = env.run({**ECHO_LOW, **proposal})
    assert transaction.state == TransactionState.PENDING_EXTERNAL
    return transaction


def _unknown(env, provider, compensator=None, **proposal):
    def timeout(request):
        raise TimeoutError("no answer")

    env.adapter.register("tool_echo", timeout, status_handler=provider, compensator=compensator)
    transaction, _ = env.run({**ECHO_LOW, **proposal})
    assert transaction.state == TransactionState.UNKNOWN
    return transaction


def _state(env, transaction):
    return env.stored(transaction)[0].state


def test_pending_external_succeeded_commits(env):
    provider = FakeProvider("succeeded", {"echo": "hello"})
    transaction = _pending(env, provider)
    results = _worker(env).run_once()

    assert results == [(str(transaction.transaction_id), "action_commit_approved")]
    assert _state(env, transaction) == "COMMITTED"
    assert env.stored(transaction)[1][-3:] == ["async_accepted", "status_succeeded", "action_commit_approved"]
    assert env.storage.verify_chain(transaction.transaction_id) == (True, None)


def test_pending_external_failed_ends_failed(env):
    transaction = _pending(env, FakeProvider("failed"))
    assert _worker(env).run_once() == [(str(transaction.transaction_id), "status_failed")]
    assert _state(env, transaction) == "FAILED"


def test_pending_external_unknown_waits_then_goes_unknown(env):
    transaction = _pending(env, FakeProvider("unknown"))
    worker = _worker(env)
    assert worker.run_once()[0][1].startswith("wait:")
    assert _state(env, transaction) == "PENDING_EXTERNAL"

    later = utcnow() + timedelta(hours=25)
    assert worker.run_once(now=later)[0][1] == "status_unknown"
    assert _state(env, transaction) == "UNKNOWN"


def test_unknown_succeeded_commits(env):
    transaction = _unknown(env, FakeProvider("succeeded", {"echo": "hello"}))
    # The timeout left no external id; the provider can still be asked by id when known
    _sql(env, "UPDATE tool_transactions SET external_operation_id = 'op-9' WHERE transaction_id = :t",
         t=str(transaction.transaction_id))
    assert _worker(env).run_once()[0][1] == "action_commit_approved"
    assert _state(env, transaction) == "COMMITTED"


def test_unknown_failed_ends_failed(env):
    transaction = _unknown(env, FakeProvider("failed"))
    _sql(env, "UPDATE tool_transactions SET external_operation_id = 'op-9' WHERE transaction_id = :t",
         t=str(transaction.transaction_id))
    assert _worker(env).run_once()[0][1] == "reconcile_failed"
    assert _state(env, transaction) == "FAILED"


def test_unknown_without_side_effect_fails_after_deadline(env):
    transaction = _unknown(env, FakeProvider("unknown"))
    worker = _worker(env)
    assert worker.run_once()[0][1].startswith("wait:")
    assert worker.run_once(now=utcnow() + timedelta(hours=2))[0][1] == "reconcile_failed"
    assert _state(env, transaction) == "FAILED"


def test_unknown_with_side_effect_requires_then_gets_compensation(env):
    compensations = []

    def compensator(tool_id, transaction_id, operation_id):
        compensations.append(transaction_id)
        return {"status": "succeeded"}

    transaction = _unknown(env, FakeProvider("unknown"), compensator=compensator,
                           side_effect_class="reversible", idempotency_key="side")
    worker = _worker(env)
    later = utcnow() + timedelta(hours=2)
    assert worker.run_once(now=later)[0][1] == "compensation_required"
    assert _state(env, transaction) == "COMPENSATION_REQUIRED"
    assert worker.run_once(now=later)[0][1] == "compensation_succeeded"
    assert _state(env, transaction) == "COMPENSATED"
    assert len(compensations) == 1
    assert env.storage.verify_chain(transaction.transaction_id) == (True, None)


def test_failed_compensation_ends_compensation_failed(env):
    transaction = _unknown(env, FakeProvider("unknown"),
                           compensator=lambda *a: {"status": "failed"},
                           side_effect_class="reversible")
    worker = _worker(env)
    later = utcnow() + timedelta(hours=2)
    worker.run_once(now=later)
    assert worker.run_once(now=later)[0][1] == "compensation_failed"
    assert _state(env, transaction) == "COMPENSATION_FAILED"


def test_compensation_without_compensator_waits_for_a_human(env):
    transaction = _unknown(env, FakeProvider("unknown"), side_effect_class="reversible")
    worker = _worker(env)
    later = utcnow() + timedelta(hours=2)
    worker.run_once(now=later)
    assert worker.run_once(now=later)[0][1].startswith("wait:no compensator")
    assert _state(env, transaction) == "COMPENSATION_REQUIRED"


def test_stuck_dispatch_with_expired_lease_goes_unknown(env):
    transaction, _ = env.run(ECHO_LOW)
    # Simulate a dispatcher that died after dispatch_started: force state, no live lease
    _sql(env, "UPDATE tool_transactions SET state = 'DISPATCHED' WHERE transaction_id = :t",
         t=str(transaction.transaction_id))
    results = dict(_worker(env).run_once())
    assert results[str(transaction.transaction_id)] == "deadline_exceeded"
    assert _state(env, transaction) == "UNKNOWN"


def test_succeeded_result_failing_verification_ends_failed(env):
    env.capability.update_tool_state("tool_echo", {
        "id": "tool_echo", "output_schema": {"type": "object", "required": ["echo"]},
    })
    transaction = _pending(env, FakeProvider("succeeded", {"wrong": 1}))
    assert _worker(env).run_once()[0][1] == "status_failed"
    assert _state(env, transaction) == "FAILED"


def test_worker_without_orchestrator_refuses(env):
    with pytest.raises(ValueError):
        ReconciliationWorker(env.adapter, env.orchestrator.verifier).run_once()
