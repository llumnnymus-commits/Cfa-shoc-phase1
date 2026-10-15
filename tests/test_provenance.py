"""
Provenance certificate acceptance tests (docs/PROVENANCE_SPEC.md, handoff step 11).

Test names carry the spec's acceptance-test number.
"""

import copy
import hashlib
import json

import pytest
from sqlalchemy import text

pytest.importorskip("cryptography")

from cfa_shoc import cli
from cfa_shoc.provenance import (
    ProvenanceError, generate_signing_key, issue_certificate, issuer_did, leaf_hash,
    merkle_root, sign_certificate, verify_certificate,
)

from tests._env import Env

ECHO_LOW = {
    "tool_id": "tool_echo",
    "operation": "echo",
    "arguments": {"message": "hello"},
    "risk_class": "low",
    "side_effect_class": "none",
}

# Fixed vectors: leaf i is the JSON string "leaf-i". Leaf digest =
# SHA256(f"{i}:" + JCS(value)); node = SHA256(left + right); an odd last node is carried up.
MERKLE_VECTORS = {
    1: ("dabee6194499fbfeb0a1ee085b1ebbea28d2044065d5ebdad2e2e0429a097352", 0),
    3: ("1b7b29369f72ad28053b20e3eb9d58a916414b5b2e3c8ba333bd919fb59b5893", 2),
    5: ("2b1c164e7b5ed33722eb4e0c8d0288fc1031f2d6f00a1ca41fe96fdc8b0c3b42", 3),
    7: ("4a41bb81c792339bc30f0fe6aa68bd401703b162368f3adc195acaf7fb8cea0e", 3),
}


@pytest.fixture
def env():
    return Env()


@pytest.fixture
def key():
    return generate_signing_key()


@pytest.fixture
def committed(env):
    transaction, _ = env.run(ECHO_LOW)
    assert transaction.state.value == "COMMITTED"
    return transaction


def test_1_issue_then_verify_is_valid(env, key, committed):
    cert = issue_certificate(committed.transaction_id, env.storage, key)
    assert cert["type"] == ["VerifiableCredential", "AgentActionProvenance"]
    subject = cert["credentialSubject"]
    assert subject["trace_id"] == str(committed.transaction_id)
    assert subject["task"]["tool_id"] == "tool_echo"
    assert len(subject["events"]) == 7
    assert subject["cryptographic_commitment"]["leaf_count"] == 4 + 7 + 2
    assert verify_certificate(cert, {issuer_did(key)}) == (True, "VALID")
    assert verify_certificate(cert, {issuer_did(key)}, storage=env.storage) == (True, "VALID")


def test_2_flipped_output_hash_fails_signature_then_merkle(env, key, committed):
    cert = issue_certificate(committed.transaction_id, env.storage, key)
    tampered = copy.deepcopy(cert)
    output_hash = tampered["credentialSubject"]["output"]["hash"]
    tampered["credentialSubject"]["output"]["hash"] = ("0" if output_hash[0] != "0" else "1") + output_hash[1:]
    assert verify_certificate(tampered, {issuer_did(key)}) == (False, "Invalid signature")

    resigned = sign_certificate(tampered, key)
    assert verify_certificate(resigned, {issuer_did(key)}) == (False, "Merkle root mismatch")


def test_3_removed_event_fails(env, key, committed):
    cert = issue_certificate(committed.transaction_id, env.storage, key)
    tampered = copy.deepcopy(cert)
    del tampered["credentialSubject"]["events"][3]
    assert verify_certificate(tampered, {issuer_did(key)}) == (False, "Invalid signature")
    resigned = sign_certificate(tampered, key)
    assert verify_certificate(resigned, {issuer_did(key)}) == (False, "Merkle root mismatch")


def test_4_wrong_issuer_is_untrusted(env, key, committed):
    cert = issue_certificate(committed.transaction_id, env.storage, key)
    other = generate_signing_key()
    assert verify_certificate(cert, {issuer_did(other)}) == (False, "Untrusted issuer")


def test_4b_issuer_swapped_to_trusted_did_fails_signature(env, key, committed):
    cert = issue_certificate(committed.transaction_id, env.storage, key)
    trusted = generate_signing_key()
    forged = copy.deepcopy(cert)
    forged["issuer"] = issuer_did(trusted)
    assert verify_certificate(forged, {issuer_did(trusted)}) == (False, "Invalid signature")


@pytest.mark.parametrize("count", [1, 3, 5, 7])
def test_5_odd_leaf_counts_give_documented_roots(count):
    leaves = [leaf_hash(i, f"leaf-{i}") for i in range(count)]
    assert merkle_root(leaves) == MERKLE_VECTORS[count]


def test_5b_vectors_match_hand_built_trees():
    h = lambda data: hashlib.sha256(data).digest()  # noqa: E731
    l = [hashlib.sha256(f'{i}:"leaf-{i}"'.encode()).digest() for i in range(7)]  # noqa: E741
    assert MERKLE_VECTORS[1][0] == l[0].hex()
    assert MERKLE_VECTORS[3][0] == h(h(l[0] + l[1]) + l[2]).hex()
    assert MERKLE_VECTORS[5][0] == h(h(h(l[0] + l[1]) + h(l[2] + l[3])) + l[4]).hex()
    assert MERKLE_VECTORS[7][0] == h(h(h(l[0] + l[1]) + h(l[2] + l[3])) + h(h(l[4] + l[5]) + l[6])).hex()


def test_5c_even_count_and_empty():
    leaves = [leaf_hash(i, f"leaf-{i}") for i in range(2)]
    assert merkle_root(leaves) == (hashlib.sha256(leaves[0] + leaves[1]).hexdigest(), 1)
    with pytest.raises(ValueError):
        merkle_root([])


def test_6_transaction_not_committed_raises(env, key):
    rejected, _ = env.run({**ECHO_LOW, "risk_class": "high"})
    assert rejected.state.value == "REJECTED"
    with pytest.raises(ProvenanceError):
        issue_certificate(rejected.transaction_id, env.storage, key)
    with pytest.raises(ProvenanceError):
        issue_certificate("00000000-0000-0000-0000-000000000000", env.storage, key)


def test_7_tampered_stored_event_fails_audit_check(env, key, committed):
    cert = issue_certificate(committed.transaction_id, env.storage, key)
    with env.storage.engine.begin() as connection:
        connection.execute(
            text("UPDATE transaction_events SET payload = '{}' WHERE transaction_id = :t AND sequence = 2"),
            {"t": str(committed.transaction_id)},
        )
    assert verify_certificate(cert, {issuer_did(key)}) == (True, "VALID")  # offline: still valid
    assert verify_certificate(cert, {issuer_did(key)}, storage=env.storage) == (False, "Audit chain mismatch")


def test_baton_ref_and_extensions_are_committed(env, key, committed):
    cert = issue_certificate(
        committed.transaction_id, env.storage, key, baton_ref="b" * 64,
        extensions={"energy_trace": {"energy_delta": 2, "threshold": 5}},
    )
    assert cert["credentialSubject"]["cryptographic_commitment"]["leaf_count"] == 13 + 2
    assert verify_certificate(cert, {issuer_did(key)}) == (True, "VALID")

    drifting = issue_certificate(
        committed.transaction_id, env.storage, key,
        extensions={"energy_trace": {"energy_delta": 9, "threshold": 5}},
    )
    ok, reason = verify_certificate(drifting, {issuer_did(key)})
    assert not ok and reason.startswith("Energy drift")


def test_raw_seed_key_and_did_round_trip(env, committed):
    seed = bytes(range(32))
    cert = issue_certificate(committed.transaction_id, env.storage, seed)
    assert cert["issuer"].startswith("did:key:z6Mk")  # Ed25519 did:key prefix
    assert verify_certificate(cert, {cert["issuer"]}) == (True, "VALID")


def test_cli_verify(env, key, committed, tmp_path, capsys):
    cert = issue_certificate(committed.transaction_id, env.storage, key)
    path = tmp_path / "cert.json"
    path.write_text(json.dumps(cert))

    assert cli.main(["verify", str(path), "--trust", issuer_did(key)]) == 0
    assert capsys.readouterr().out.strip() == "VALID"

    assert cli.main(["verify", str(path), "--trust", issuer_did(generate_signing_key())]) == 1
    assert capsys.readouterr().out.strip() == "Untrusted issuer"

    assert cli.main(["verify", str(tmp_path / "missing.json")]) == 1
