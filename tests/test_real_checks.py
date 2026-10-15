"""
Real checks that replaced the stubs (handoff step 8: D11, D12, D13).

Every check has a failing case and a passing case.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from cfa_shoc.domain import (
    ActionProposal, CommitRequest, PolicyDecision, RiskClass, SideEffectClass, ToolTransaction,
    TransactionState, utcnow,
)
from cfa_shoc.schema import validate
from cfa_shoc.services import CommitMembrane, PolicyDecisionPoint, ResultVerifier

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


# ---------------------------------------------------------------- schema validator


class TestSchemaValidator:
    SCHEMA = {
        "type": "object",
        "required": ["message"],
        "properties": {
            "message": {"type": "string", "maxLength": 5},
            "mode": {"enum": ["plain", "loud"]},
            "count": {"type": "integer", "minimum": 1, "maximum": 3},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        },
        "additionalProperties": False,
    }

    def test_valid_instance(self):
        assert validate({"message": "hi", "mode": "loud", "count": 2, "tags": ["a"]}, self.SCHEMA) == []

    @pytest.mark.parametrize("instance, fragment", [
        ({}, "missing required property 'message'"),
        ({"message": 7}, "expected string"),
        ({"message": "toolong"}, "maxLength"),
        ({"message": "hi", "mode": "quiet"}, "is not one of"),
        ({"message": "hi", "count": 0}, "below minimum"),
        ({"message": "hi", "count": 9}, "above maximum"),
        ({"message": "hi", "count": True}, "expected integer"),
        ({"message": "hi", "tags": ["a", 1]}, "expected string"),
        ({"message": "hi", "tags": ["a", "b", "c"]}, "maxItems"),
        ({"message": "hi", "extra": 1}, "unexpected property 'extra'"),
        ("not an object", "expected object"),
    ])
    def test_invalid_instances(self, instance, fragment):
        errors = validate(instance, self.SCHEMA)
        assert errors and any(fragment in e for e in errors), errors

    def test_no_schema_accepts_anything(self):
        assert validate({"anything": [1, 2]}, None) == []


# ---------------------------------------------------------------- preflight: schema


def test_schema_check_rejects_bad_arguments(env):
    env.capability.update_tool_state("tool_echo", {
        "id": "tool_echo",
        "input_schema": {"type": "object", "required": ["message"],
                         "properties": {"message": {"type": "string"}}},
    })
    transaction, result = env.run({**ECHO_LOW, "arguments": {"message": 42}})
    assert transaction.state == TransactionState.REJECTED
    assert "schema" in result.reason
    assert env.calls == []


def test_schema_check_accepts_good_arguments(env):
    env.capability.update_tool_state("tool_echo", {
        "id": "tool_echo",
        "input_schema": {"type": "object", "required": ["message"],
                         "properties": {"message": {"type": "string"}}},
    })
    transaction, result = env.run(ECHO_LOW)
    assert transaction.state == TransactionState.COMMITTED, result.reason


# ---------------------------------------------------------------- preflight: quota


def test_quota_exhausted_is_rejected_before_dispatch(env):
    env.capability.current_snapshot["quotas"] = {"tenant_1": {"tool_echo": 1}}
    first, _ = env.run({**ECHO_LOW, "idempotency_key": "q1"})
    second, result = env.run({**ECHO_LOW, "idempotency_key": "q2"})

    assert first.state == TransactionState.COMMITTED
    assert second.state == TransactionState.REJECTED
    assert "Quota exhausted" in result.reason
    assert len(env.calls) == 1
    assert env.capability.quota_remaining("tenant_1", "tool_echo") == 0


def test_quota_available_allows_and_is_consumed(env):
    env.capability.current_snapshot["quotas"] = {"*": {"tool_echo": 2}}
    transaction, _ = env.run(ECHO_LOW)
    assert transaction.state == TransactionState.COMMITTED
    assert env.capability.quota_remaining("tenant_1", "tool_echo") == 1


# ---------------------------------------------------------------- preflight: risk cap


def test_risk_above_contract_max_risk_is_rejected(env):
    context = env.context(contract={"max_risk": "low",
                                    "pre_authorized": [{"tool_id": "tool_echo", "max_risk": "high"}]})
    transaction, result = env.run({**ECHO_LOW, "risk_class": "high"}, context)
    assert transaction.state == TransactionState.REJECTED
    assert "exceeds contract max_risk" in result.reason


def test_risk_within_contract_max_risk_passes(env):
    context = env.context(contract={"max_risk": "medium"})
    transaction, _ = env.run(ECHO_LOW, context)
    assert transaction.state == TransactionState.COMMITTED


# ---------------------------------------------------------------- preflight: conflicts


def _hold_in_flight(env, key):
    """Leave a transaction in PENDING_EXTERNAL (in flight) with the given key."""
    env.adapter.register("tool_echo", lambda r: {"status": "accepted", "external_operation_id": "op"})
    held, _ = env.run({**ECHO_LOW, "idempotency_key": key})
    assert held.state == TransactionState.PENDING_EXTERNAL
    env.adapter.register("tool_echo", env._echo)
    return held


def test_same_idempotency_key_in_flight_conflicts(env):
    held = _hold_in_flight(env, "dup")
    transaction, result = env.run({**ECHO_LOW, "idempotency_key": "dup"})
    assert transaction.state == TransactionState.REJECTED
    assert str(held.transaction_id) in result.reason
    assert env.calls == []


def test_different_idempotency_key_does_not_conflict(env):
    _hold_in_flight(env, "dup")
    transaction, _ = env.run({**ECHO_LOW, "idempotency_key": "other"})
    assert transaction.state == TransactionState.COMMITTED


def test_exclusive_tool_in_use_conflicts(env):
    env.capability.update_tool_state("tool_echo", {"id": "tool_echo", "exclusive": True})
    _hold_in_flight(env, "first")
    transaction, result = env.run({**ECHO_LOW, "idempotency_key": "second"})
    assert transaction.state == TransactionState.REJECTED
    assert "exclusive tool" in result.reason


def test_exclusive_tool_free_passes(env):
    env.capability.update_tool_state("tool_echo", {"id": "tool_echo", "exclusive": True})
    transaction, _ = env.run(ECHO_LOW)
    assert transaction.state == TransactionState.COMMITTED


# ---------------------------------------------------------------- policy lane B


def test_lane_b_pre_authorized_medium_risk_commits(env):
    context = env.context(contract={
        "pre_authorized": [{"tool_id": "tool_echo", "operation": "echo", "max_risk": "medium"}]
    })
    transaction, result = env.run({**ECHO_LOW, "risk_class": "medium"}, context)
    assert transaction.state == TransactionState.COMMITTED, result.reason
    assert transaction.policy_decision["lane"] == "B"


@pytest.mark.parametrize("entry", [
    {"tool_id": "tool_echo", "operation": "other_op", "max_risk": "high"},  # wrong operation
    {"tool_id": "tool_echo", "operation": "*", "max_risk": "low"},          # risk above entry cap
    {"tool_id": "tool_other", "operation": "*", "max_risk": "high"},        # different tool
])
def test_lane_b_entry_that_does_not_cover_is_denied(env, entry):
    context = env.context(contract={"pre_authorized": [entry]})
    transaction, _ = env.run({**ECHO_LOW, "risk_class": "medium"}, context)
    assert transaction.state == TransactionState.REJECTED
    assert env.calls == []


# ---------------------------------------------------------------- policy lane C


def _lane_c(env):
    context = env.context(contract={"confirm_up_to": "high"})
    transaction, result = env.run({**ECHO_LOW, "risk_class": "medium"}, context)
    return transaction, result


def test_lane_c_waits_in_preflighted_without_dispatch(env):
    transaction, result = _lane_c(env)
    assert transaction.state == TransactionState.PREFLIGHTED
    assert result.requires_confirmation and not result.allowed
    assert env.calls == []
    assert env.stored(transaction)[1][-1] == "confirmation_requested"


def test_lane_c_confirm_commits_with_named_approver(env):
    transaction, _ = _lane_c(env)
    confirmed, result = env.orchestrator.confirm(transaction.transaction_id, "alice")

    assert confirmed.state == TransactionState.COMMITTED, result.reason
    assert confirmed.policy_decision["lane"] == "C"
    assert confirmed.policy_decision["approver_type"] == "human"
    assert confirmed.policy_decision["approver_id"] == "alice"
    assert len(env.calls) == 1
    assert env.stored(confirmed)[1][:4] == [
        "orchestration_started", "preflight_passed", "confirmation_requested", "policy_approved",
    ]


def test_lane_c_decline_rejects_without_dispatch(env):
    transaction, _ = _lane_c(env)
    declined, result = env.orchestrator.decline(transaction.transaction_id, "alice")
    assert declined.state == TransactionState.REJECTED
    assert env.calls == []


def test_lane_c_expired_confirmation_rejects(env, monkeypatch):
    monkeypatch.setattr(PolicyDecisionPoint, "CONFIRMATION_TTL", timedelta(seconds=-1))
    transaction, _ = _lane_c(env)
    expired, result = env.orchestrator.confirm(transaction.transaction_id, "alice")
    assert expired.state == TransactionState.REJECTED
    assert "expired" in result.reason
    assert env.calls == []


def test_confirm_refuses_transaction_not_waiting(env):
    transaction, _ = env.run(ECHO_LOW)  # committed through lane A
    with pytest.raises(ValueError):
        env.orchestrator.confirm(transaction.transaction_id, "alice")
    with pytest.raises(ValueError):
        env.orchestrator.confirm(uuid4(), "alice")


def test_without_confirm_up_to_medium_is_still_denied(env):
    transaction, result = env.run({**ECHO_LOW, "risk_class": "medium"})
    assert transaction.state == TransactionState.REJECTED
    assert not result.requires_confirmation


# ---------------------------------------------------------------- signatures


def _proposal(**overrides):
    fields = dict(tool_id="tool_echo", operation="echo", risk_class=RiskClass.LOW,
                  side_effect_class=SideEffectClass.NONE)
    fields.update(overrides)
    return ActionProposal(**fields)


class TestPolicySignatures:
    def test_signature_verifies(self):
        point = PolicyDecisionPoint(signing_key=b"k" * 32)
        decision = point.evaluate(_proposal(), {"hash": "h"}, {})
        assert decision.approved and decision.lane == "A"
        assert point.verify_signature(decision)

    def test_tampered_decision_fails(self):
        point = PolicyDecisionPoint(signing_key=b"k" * 32)
        decision = point.evaluate(_proposal(risk_class=RiskClass.HIGH), {"hash": "h"}, {})
        assert not decision.approved
        decision.approved = True  # forged approval
        assert not point.verify_signature(decision)

    def test_other_key_fails(self):
        decision = PolicyDecisionPoint(signing_key=b"a" * 32).evaluate(_proposal(), {"hash": "h"}, {})
        assert not PolicyDecisionPoint(signing_key=b"b" * 32).verify_signature(decision)

    def test_unsigned_decision_fails(self):
        point = PolicyDecisionPoint()
        decision = PolicyDecision(approved=True, reason="forged", subject_type="action",
                                  subject_ref="x", contract_hash="", ledger_hash="")
        assert not point.verify_signature(decision)

    def test_key_never_in_repr(self):
        point = PolicyDecisionPoint(signing_key=b"secret-key-material-0123456789ab")
        assert "secret" not in repr(point) and "secret" not in str(vars(point))


def test_forged_policy_decision_is_revoked_before_dispatch(env, monkeypatch):
    original = env.orchestrator.policy_point.evaluate

    def forge(proposal, ledger, contract):
        decision = original(proposal, ledger, contract)
        decision.reason = "edited after signing"
        return decision

    monkeypatch.setattr(env.orchestrator.policy_point, "evaluate", forge)
    transaction, result = env.run(ECHO_LOW)
    assert transaction.state == TransactionState.REJECTED
    assert env.stored(transaction)[1][-1] == "authorization_revoked"
    assert env.calls == []


def test_expired_policy_decision_is_revoked_before_dispatch(env, monkeypatch):
    monkeypatch.setattr(PolicyDecisionPoint, "DECISION_TTL", timedelta(seconds=-1))
    transaction, result = env.run(ECHO_LOW)
    assert transaction.state == TransactionState.REJECTED
    assert "expired or invalid signature" in result.reason
    assert env.calls == []


class TestCommitSignatures:
    def _verified_tx(self):
        return ToolTransaction(
            state=TransactionState.VERIFIED, verification={"passed": True},
            result_envelope={"observation": {}}, ledger_hash="L", contract_hash="C",
        )

    def test_commit_decision_signature_verifies_and_detects_tampering(self):
        membrane = CommitMembrane(signing_key=b"m" * 32)
        tx = self._verified_tx()
        ok, decision = membrane.action_gate(
            CommitRequest(transaction_id=tx.transaction_id, verified_result_ref="r",
                          contract_hash="C", ledger_hash="L"),
            tx, {"hash": "L"}, {"hash": "C"},
        )
        assert ok and membrane.verify_signature(decision)
        decision.decision = "DENY"
        assert not membrane.verify_signature(decision)

    @pytest.mark.parametrize("mutate", [
        lambda tx: setattr(tx, "state", TransactionState.RETURNED),
        lambda tx: setattr(tx, "verification", {"passed": False}),
        lambda tx: setattr(tx, "result_envelope", None),
    ])
    def test_gate_refuses_unverified_transaction(self, mutate):
        membrane = CommitMembrane()
        tx = self._verified_tx()
        mutate(tx)
        ok, decision = membrane.action_gate(
            CommitRequest(transaction_id=tx.transaction_id, verified_result_ref="r",
                          contract_hash="C", ledger_hash="L"),
            tx, {"hash": "L"}, {"hash": "C"},
        )
        assert not ok and decision is None


# ---------------------------------------------------------------- result verifier


class TestResultVerifier:
    def _tx(self, **overrides):
        fields = dict(tool_id="tool_echo", connector_id=None, idempotency_key="k1")
        fields.update(overrides)
        return ToolTransaction(**fields)

    def _raw(self, **overrides):
        raw = {"transport_status": "ok", "tool_id": "tool_echo", "connector_id": None,
               "output": {"echo": "hi"}, "idempotency_key": None}
        raw.update(overrides)
        return raw

    def test_good_result_passes(self):
        verification = ResultVerifier().verify(self._raw(), self._tx())
        assert verification.passed, verification.reason
        assert verification.status_terminal and verification.schema_valid
        assert verification.tool_connector_match

    @pytest.mark.parametrize("overrides, fragment", [
        ({"transport_status": "async_accepted"}, "not terminal"),
        ({"transport_status": "error"}, "not terminal"),
        ({"tool_id": "tool_other"}, "expected tool_echo"),
        ({"connector_id": "http"}, "expected tool_echo"),
        ({"idempotency_key": "someone-else"}, "idempotency key does not match"),
    ])
    def test_bad_results_fail(self, overrides, fragment):
        verification = ResultVerifier().verify(self._raw(**overrides), self._tx())
        assert not verification.passed
        assert fragment in verification.reason

    def test_output_schema_enforced(self, env):
        env.capability.update_tool_state("tool_echo", {
            "id": "tool_echo",
            "output_schema": {"type": "object", "required": ["echo"],
                              "properties": {"echo": {"type": "string"}}},
        })
        verifier = ResultVerifier(env.capability)
        assert verifier.verify(self._raw(), self._tx()).passed
        bad = verifier.verify(self._raw(output={"echo": 5}), self._tx())
        assert not bad.passed and not bad.schema_valid

    def test_tool_that_must_echo_key(self, env):
        env.capability.update_tool_state("tool_echo", {"id": "tool_echo", "echoes_idempotency_key": True})
        verifier = ResultVerifier(env.capability)
        assert not verifier.verify(self._raw(), self._tx()).passed
        assert verifier.verify(self._raw(idempotency_key="k1"), self._tx()).passed

    def test_bad_output_in_orchestration_ends_failed(self, env):
        env.capability.update_tool_state("tool_echo", {
            "id": "tool_echo", "output_schema": {"type": "object", "required": ["never"]},
        })
        transaction, result = env.run(ECHO_LOW)
        assert transaction.state == TransactionState.FAILED
        assert env.stored(transaction)[1][-1] == "verification_failed"
