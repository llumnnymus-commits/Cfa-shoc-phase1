"""
DRxit baton acceptance tests (docs/DRXIT_BATON_SPEC.md, handoff step 10).

Test names carry the spec's acceptance-test number.
"""

import copy
import hashlib

import pytest
from sqlalchemy import text

from cfa_shoc import baton as drxit
from cfa_shoc.baton import BatonChainError, BatonRejected, BatonStateError

from tests._env import Env

KEY = b"baton-signing-key-0123456789abcd"
REGISTRY = {"debugger": ["staging-only"], "verifier": ["read-only"], "coordinator": ["close"]}


@pytest.fixture
def env():
    return Env()


@pytest.fixture
def artifact(tmp_path):
    path = tmp_path / "test-report.json"
    path.write_bytes(b'{"passed": 12, "failed": 0}')
    return tmp_path, {"path": "test-report.json", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def committed_tx(env, side_effect="none", key=None):
    transaction, _ = env.run({
        "tool_id": "tool_echo", "operation": "echo", "arguments": {"message": "x"},
        "risk_class": "low", "side_effect_class": side_effect,
        "idempotency_key": key or f"k-{side_effect}-{len(env.calls)}",
    })
    assert transaction.state.value == "COMMITTED"
    return str(transaction.transaction_id)


def make_baton(run_id="drxit-run-1", leg=1, parent=None, transactions=(), artifacts=(), **overrides):
    baton = {
        "baton_version": "1",
        "run_id": run_id,
        "leg": leg,
        "parent_leg": None if parent is None else leg - 1,
        "parent_baton_hash": parent,
        "global_objective": "Deliver a verified production-safe implementation",
        "local_objective": f"Leg {leg} work",
        "world_state": {
            "confirmed_facts": [{"fact": f"leg {leg} tests pass", "evidence_refs": ["artifact:test-report.json"]}],
            "uncertainties": ["staging behaviour"],
            "open_contradictions": [],
            "constraints": ["no production writes"],
        },
        "artifacts": list(artifacts),
        "transactions": list(transactions),
        "evaluation": {
            "reward": 0.8, "verifier_results": [{"name": "unit", "passed": True}],
            "cost": 0.0, "latency_ms": 1200, "failure_mode": None,
        },
        "handoff_decision": "HANDOFF_TO_VERIFIER",
        "next_leg_contract": {
            "required_capability": "debugger",
            "objective": "Test adapter against the staging database",
            "entry_conditions": [],
            "entry_state": ["test-report.json"],
            "success_condition": [],
            "completion_criteria": ["integration suite passes"],
            "authority": "staging-only",
            "rollback_state": None,
        },
    }
    for key, value in overrides.items():
        baton[key] = value
    return baton


def test_1_valid_baton_seals_validates_acknowledges_retires_in_order(env, artifact):
    root, art = artifact
    sealed = drxit.seal(make_baton(transactions=[committed_tx(env)], artifacts=[art]), KEY, env.storage)
    assert sealed.state == "SEALED" and sealed.baton["signature"]

    assert drxit.validate(sealed.baton, KEY, env.storage, REGISTRY, artifact_root=str(root)) == []
    drxit.acknowledge(sealed.baton_id, "leg-2-debugger", env.storage)
    drxit.retire(sealed.baton_id, env.storage)

    [stored] = drxit.chain("drxit-run-1", env.storage)
    assert stored.state == "RETIRED" and stored.successor_id == "leg-2-debugger"


def test_1b_acknowledge_requires_passing_validation(env):
    sealed = drxit.seal(make_baton(), KEY, env.storage)
    with pytest.raises(BatonStateError):
        drxit.acknowledge(sealed.baton_id, "leg-2", env.storage)  # never validated


def test_2_retire_before_acknowledge_raises(env):
    sealed = drxit.seal(make_baton(), KEY, env.storage)
    assert drxit.validate(sealed.baton, KEY, env.storage, REGISTRY) == []
    with pytest.raises(BatonStateError):
        drxit.retire(sealed.baton_id, env.storage)


def test_3_confirmed_fact_without_evidence_is_rejected(env):
    baton = make_baton()
    baton["world_state"]["confirmed_facts"] = [{"fact": "it works", "evidence_refs": []}]
    with pytest.raises(BatonRejected) as excinfo:
        drxit.seal(baton, KEY, env.storage)
    assert any("no evidence_refs" in e for e in excinfo.value.errors)


def test_4a_missing_rollback_state_key_is_rejected(env):
    baton = make_baton()
    del baton["next_leg_contract"]["rollback_state"]
    with pytest.raises(BatonRejected) as excinfo:
        drxit.seal(baton, KEY, env.storage)
    assert any("rollback_state is missing" in e for e in excinfo.value.errors)


def test_4b_null_rollback_state_with_side_effects_is_rejected(env):
    baton = make_baton(transactions=[committed_tx(env, side_effect="reversible")])
    with pytest.raises(BatonRejected) as excinfo:
        drxit.seal(baton, KEY, env.storage)
    assert any("rollback_state is null" in e for e in excinfo.value.errors)


def test_4c_null_rollback_state_allowed_without_side_effects_or_when_closing(env):
    drxit.seal(make_baton(transactions=[committed_tx(env)]), KEY, env.storage)
    closing = make_baton(
        run_id="drxit-run-close", transactions=[committed_tx(env, side_effect="reversible", key="c")],
        handoff_decision="COMMIT_AND_CLOSE",
        world_state={"confirmed_facts": [], "uncertainties": [], "open_contradictions": [], "constraints": []},
    )
    closing["next_leg_contract"].update(required_capability="coordinator", authority="close")
    assert drxit.seal(closing, KEY, env.storage).state == "SEALED"


def test_4d_rollback_state_present_with_side_effects_is_accepted(env):
    baton = make_baton(transactions=[committed_tx(env, side_effect="reversible")])
    baton["next_leg_contract"]["rollback_state"] = {"restore": "snapshot-41"}
    assert drxit.seal(baton, KEY, env.storage).state == "SEALED"


def test_5_changing_any_field_after_sealing_fails_signature(env):
    sealed = drxit.seal(make_baton(), KEY, env.storage)
    for mutate in (
        lambda b: b.__setitem__("local_objective", "something else"),
        lambda b: b["evaluation"].__setitem__("reward", 1.0),
        lambda b: b["next_leg_contract"].__setitem__("authority", "production"),
    ):
        tampered = copy.deepcopy(sealed.baton)
        mutate(tampered)
        errors = drxit.validate(tampered, KEY, env.storage, REGISTRY)
        assert "signature does not verify" in errors


def test_5b_wrong_key_fails_signature_and_rejects(env):
    sealed = drxit.seal(make_baton(), KEY, env.storage)
    errors = drxit.validate(sealed.baton, b"another-key-0123456789abcdefghij", env.storage, REGISTRY)
    assert "signature does not verify" in errors
    assert drxit.chain("drxit-run-1", env.storage)[0].state == "REJECTED"  # fail closed
    with pytest.raises(BatonStateError):
        drxit.acknowledge(sealed.baton_id, "leg-2", env.storage)


def test_6_artifact_sha256_mismatch_is_rejected(env, artifact):
    root, art = artifact
    sealed = drxit.seal(make_baton(artifacts=[{**art, "sha256": "0" * 64}]), KEY, env.storage)
    errors = drxit.validate(sealed.baton, KEY, env.storage, REGISTRY, artifact_root=str(root))
    assert any("sha256 mismatch" in e for e in errors)


def test_6b_missing_artifact_is_rejected(env, tmp_path):
    sealed = drxit.seal(make_baton(artifacts=[{"path": "nope.json", "sha256": "a" * 64}]), KEY, env.storage)
    errors = drxit.validate(sealed.baton, KEY, env.storage, REGISTRY, artifact_root=str(tmp_path))
    assert any("cannot be read" in e for e in errors)


def test_7a_unknown_transaction_is_rejected(env):
    with pytest.raises(BatonRejected) as excinfo:
        drxit.seal(make_baton(transactions=["00000000-0000-0000-0000-000000000000"]), KEY, env.storage)
    assert any("does not exist" in e for e in excinfo.value.errors)


def test_7b_transaction_not_final_is_rejected(env):
    env.adapter.register("tool_echo", lambda r: {"status": "accepted", "external_operation_id": "op"})
    pending, _ = env.run({"tool_id": "tool_echo", "operation": "echo", "risk_class": "low"})
    with pytest.raises(BatonRejected) as excinfo:
        drxit.seal(make_baton(transactions=[str(pending.transaction_id)]), KEY, env.storage)
    assert any("not in a final state (PENDING_EXTERNAL)" in e for e in excinfo.value.errors)


def _three_legs(env):
    first = drxit.seal(make_baton(leg=1), KEY, env.storage)
    second = drxit.seal(make_baton(leg=2, parent=first.baton_id), KEY, env.storage)
    third = drxit.seal(make_baton(leg=3, parent=second.baton_id), KEY, env.storage)
    return first, second, third


def test_8a_three_leg_chain_verifies(env):
    first, second, third = _three_legs(env)
    batons = drxit.chain("drxit-run-1", env.storage)
    assert [b.baton_id for b in batons] == [first.baton_id, second.baton_id, third.baton_id]
    assert batons[1].parent_baton_hash == first.baton_id


def test_8b_breaking_the_middle_parent_hash_is_detected_at_index_1(env):
    _three_legs(env)
    with env.storage.engine.begin() as connection:
        row = connection.execute(text("SELECT baton_hash, content FROM batons WHERE leg = 2")).one()
        import json
        content = json.loads(row.content) if isinstance(row.content, str) else row.content
        content["parent_baton_hash"] = "f" * 64
        connection.execute(text("UPDATE batons SET content = :c WHERE baton_hash = :h"),
                           {"c": json.dumps(content), "h": row.baton_hash})
    with pytest.raises(BatonChainError) as excinfo:
        drxit.chain("drxit-run-1", env.storage)
    assert excinfo.value.index == 1


def test_8c_unsealed_parent_is_rejected(env):
    with pytest.raises(BatonRejected):
        drxit.seal(make_baton(leg=2, parent="a" * 64), KEY, env.storage)


def test_9a_unknown_handoff_decision_is_rejected(env):
    with pytest.raises(BatonRejected) as excinfo:
        drxit.seal(make_baton(handoff_decision="HANDOFF_TO_THE_VOID"), KEY, env.storage)
    assert any("unknown handoff_decision" in e for e in excinfo.value.errors)


def test_9b_unknown_required_capability_is_rejected(env):
    baton = make_baton()
    baton["next_leg_contract"]["required_capability"] = "wizard"
    sealed = drxit.seal(baton, KEY, env.storage)
    errors = drxit.validate(sealed.baton, KEY, env.storage, REGISTRY)
    assert any("not in the registry" in e for e in errors)


def test_9c_authority_not_granted_is_rejected(env):
    baton = make_baton()
    baton["next_leg_contract"]["authority"] = "production"
    sealed = drxit.seal(baton, KEY, env.storage)
    errors = drxit.validate(sealed.baton, KEY, env.storage, REGISTRY)
    assert any("not granted" in e for e in errors)


def test_extra_raw_reasoning_field_is_rejected(env):
    with pytest.raises(BatonRejected) as excinfo:
        drxit.seal(make_baton(hidden_reasoning="..."), KEY, env.storage)
    assert any("unsupported fields" in e for e in excinfo.value.errors)


def test_contradictory_baton_is_rejected(env):
    baton = make_baton()
    baton["world_state"]["uncertainties"] = ["leg 1 tests pass"]
    with pytest.raises(BatonRejected) as excinfo:
        drxit.seal(baton, KEY, env.storage)
    assert any("contradiction" in e for e in excinfo.value.errors)


def test_failed_verifier_needs_recovery_or_human(env):
    evaluation = {"reward": 0.0, "verifier_results": [{"name": "unit", "passed": False}],
                  "cost": 0.0, "latency_ms": 5, "failure_mode": None}
    with pytest.raises(BatonRejected):
        drxit.seal(make_baton(evaluation=evaluation), KEY, env.storage)
    recovery = make_baton(evaluation={**evaluation, "failure_mode": "tests failing"},
                          handoff_decision="HANDOFF_TO_RECOVERY")
    assert drxit.seal(recovery, KEY, env.storage).state == "SEALED"


def test_same_leg_cannot_be_sealed_twice(env):
    drxit.seal(make_baton(), KEY, env.storage)
    with pytest.raises(BatonRejected):
        drxit.seal(make_baton(local_objective="other"), KEY, env.storage)
