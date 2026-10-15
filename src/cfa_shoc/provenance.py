"""
Provenance certificates for committed CFA-SHOC transactions.

Spec: docs/PROVENANCE_SPEC.md. A COMMITTED transaction is exported as a W3C Verifiable
Credential-shaped document whose `credentialSubject` names the tool call, hashes its input
and output, lists its audit-event hashes and decision hashes, and commits to all of them with
a Merkle root. The issuer signs JCS(credentialSubject) with Ed25519; anyone holding the
issuer's `did:key` can verify offline.

Scope note: this implements the spec's *source form* (proof type `Ed25519Signature2020`, signature
over JCS(credentialSubject)). Certificates are VC-shaped and verifiable with this library;
they are not claimed to verify with generic VC tooling.

Ed25519 needs the optional `cryptography` package: `pip install "cfa-shoc[provenance]"`.
The Merkle functions need nothing extra.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .audit import canonical_json
from .domain import utcnow
from .storage import CommitDecisionORM, StorageManager

ALGORITHM = "SHA256"
PROOF_TYPE = "Ed25519Signature2020"
VC_CONTEXT = "https://www.w3.org/ns/credentials/v2"
EXTENSION_LEAVES = ("expert_calls", "symbolic_steps", "adjudication", "energy_trace")
_ED25519_MULTICODEC = b"\xed\x01"
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class ProvenanceError(ValueError):
    """A certificate cannot be issued for this transaction."""


# ------------------------------------------------------------------ JCS and Merkle


def jcs(value: Any) -> str:
    """
    Phase 1 JSON canonicalization: sorted keys, no whitespace, UTF-8 kept. Matches RFC 8785
    for certificate values, which are restricted to strings, ints, bools, null, lists and
    objects (no floats).
    """
    _reject_floats(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: Any) -> str:
    """SHA-256 hex of a value's canonical JSON (canonical_json allows any JSON value)."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def leaf_hash(index: int, value: Any) -> bytes:
    """Leaf digest = SHA256( str(index) + ":" + JCS(value) ), UTF-8."""
    return hashlib.sha256(f"{index}:{jcs(value)}".encode("utf-8")).digest()


def merkle_root(leaves: List[bytes]) -> Tuple[str, int]:
    """
    Root of a Merkle tree over leaf digests. Internal node = SHA256(left + right). On a level
    with an odd number of nodes, the last node is carried up unchanged (not duplicated).

    Returns:
        (hex root, depth), where depth is the number of levels above the leaves
    """
    if not leaves:
        raise ValueError("a Merkle tree needs at least one leaf")
    level = list(leaves)
    depth = 0
    while len(level) > 1:
        next_level = [
            hashlib.sha256(level[i] + level[i + 1]).digest() for i in range(0, len(level) - 1, 2)
        ]
        if len(level) % 2 == 1:
            next_level.append(level[-1])
        level = next_level
        depth += 1
    return level[0].hex(), depth


def subject_leaf_values(subject: Dict[str, Any]) -> List[Any]:
    """The values committed to, in the spec's fixed order."""
    values: List[Any] = [
        subject["trace_id"],
        subject["task"],
        subject["input"]["hash"],
        subject["output"]["hash"],
    ]
    values.extend(event["event_hash"] for event in subject["events"])
    values.append(subject["policy_decision"]["hash"])
    values.append(subject["commit_decision"]["hash"])
    if subject.get("baton_ref") is not None:
        values.append(subject["baton_ref"])
    for name in EXTENSION_LEAVES:
        if name in subject:
            values.append(subject[name])
    return values


def commitment_for(subject: Dict[str, Any]) -> Dict[str, Any]:
    values = subject_leaf_values(subject)
    root, depth = merkle_root([leaf_hash(i, v) for i, v in enumerate(values)])
    return {"merkle_root": root, "tree_depth": depth, "leaf_count": len(values), "algorithm": ALGORITHM}


# ------------------------------------------------------------------ keys and did:key


def generate_signing_key():
    """A new Ed25519 private key (needs the `cryptography` extra)."""
    return _ed25519().Ed25519PrivateKey.generate()


def issuer_did(key) -> str:
    """did:key for an Ed25519 private or public key."""
    public = key.public_key() if hasattr(key, "public_key") else key
    raw = public.public_bytes(_serialization().Encoding.Raw, _serialization().PublicFormat.Raw)
    return "did:key:z" + _b58encode(_ED25519_MULTICODEC + raw)


def public_key_from_did(did: str):
    """The Ed25519 public key inside a did:key."""
    if not did.startswith("did:key:z"):
        raise ValueError("not a base58btc did:key")
    data = _b58decode(did[len("did:key:z"):])
    if data[:2] != _ED25519_MULTICODEC or len(data) != 34:
        raise ValueError("not an Ed25519 did:key")
    return _ed25519().Ed25519PublicKey.from_public_bytes(data[2:])


# ------------------------------------------------------------------ issue and verify


def issue_certificate(
    transaction_id: Any,
    storage: StorageManager,
    signing_key: Any,
    baton_ref: Optional[str] = None,
    extensions: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build and sign a provenance certificate for a COMMITTED transaction.

    `signing_key` is an Ed25519PrivateKey or its 32-byte raw seed. It is used once and never
    stored or logged.

    Raises:
        ProvenanceError: unknown transaction, not COMMITTED, or no commit decision stored
    """
    key = _private_key(signing_key)
    session = storage.get_session()
    try:
        row = storage.get_transaction(session, str(transaction_id))
        if row is None:
            raise ProvenanceError(f"Unknown transaction {transaction_id}")
        if row.state != "COMMITTED":
            raise ProvenanceError(f"Transaction {transaction_id} is {row.state}, not COMMITTED")
        commit = session.query(CommitDecisionORM).filter(
            CommitDecisionORM.transaction_id == row.transaction_id
        ).order_by(CommitDecisionORM.created_at).first()
        if commit is None:
            raise ProvenanceError(f"Transaction {transaction_id} has no stored commit decision")
        events = storage.get_events_for_transaction(session, row.transaction_id)

        policy = row.policy_decision or {}
        observation = (row.result_envelope or {}).get("observation", {})
        subject: Dict[str, Any] = {
            "trace_id": row.transaction_id,
            "task": {
                "tool_id": row.tool_id,
                "operation": row.operation,
                "principal_id": row.principal_id,
                "tenant_id": row.tenant_id,
            },
            "input": {"hash": sha256_hex(row.input_payload)},
            "output": {"hash": sha256_hex(observation.get("content"))},
            "events": [
                {"index": e.sequence, "event_type": e.event_type, "event_hash": e.event_hash}
                for e in events
            ],
            "policy_decision": {
                "decision_id": policy.get("decision_id", ""),
                "lane": policy.get("lane", ""),
                "hash": sha256_hex(policy),
            },
            "commit_decision": {
                "decision_id": commit.decision_id,
                "hash": sha256_hex(_commit_fields(commit)),
            },
        }
        if baton_ref is not None:
            subject["baton_ref"] = baton_ref
        for name, value in (extensions or {}).items():
            if name not in EXTENSION_LEAVES:
                raise ProvenanceError(f"Unknown extension {name!r}")
            subject[name] = value
        subject["cryptographic_commitment"] = commitment_for(subject)
    finally:
        session.close()

    did = issuer_did(key)
    now = utcnow().replace(microsecond=0).isoformat()
    certificate = {
        "@context": [VC_CONTEXT],
        "type": ["VerifiableCredential", "AgentActionProvenance"],
        "issuer": did,
        "validFrom": now,
        "credentialSubject": subject,
    }
    return sign_certificate(certificate, key, created=now)


def sign_certificate(certificate: Dict[str, Any], signing_key: Any, created: Optional[str] = None) -> Dict[str, Any]:
    """(Re-)sign a certificate's credentialSubject. Returns a new certificate dict."""
    key = _private_key(signing_key)
    did = issuer_did(key)
    signature = key.sign(jcs(certificate["credentialSubject"]).encode("utf-8"))
    signed = dict(certificate)
    signed["issuer"] = did
    signed["proof"] = {
        "type": PROOF_TYPE,
        "created": created or utcnow().replace(microsecond=0).isoformat(),
        "verificationMethod": f"{did}#{did[len('did:key:'):]}",
        "proofPurpose": "assertionMethod",
        "proofValue": "z" + _b58encode(signature),
    }
    return signed


def verify_certificate(
    cert: Dict[str, Any], trusted_issuers: Set[str], storage: Optional[StorageManager] = None
) -> Tuple[bool, str]:
    """
    Verify a certificate offline (and, with storage, against the stored audit chain).

    Returns:
        (True, "VALID") or (False, reason): "Untrusted issuer", "Invalid signature",
        "Merkle root mismatch", "Audit chain mismatch", "Energy drift: ..."
    """
    try:
        issuer = cert["issuer"]
        subject = cert["credentialSubject"]
        proof = cert["proof"]
    except (KeyError, TypeError):
        return False, "Malformed certificate"

    # 1. Trusted issuer
    if issuer not in trusted_issuers:
        return False, "Untrusted issuer"

    # 2. Ed25519 signature over JCS(credentialSubject)
    try:
        if proof.get("type") != PROOF_TYPE or not str(proof.get("verificationMethod", "")).startswith(issuer + "#"):
            return False, "Invalid signature"
        value = proof.get("proofValue", "")
        if not value.startswith("z"):
            return False, "Invalid signature"
        public_key_from_did(issuer).verify(_b58decode(value[1:]), jcs(subject).encode("utf-8"))
    except Exception:
        return False, "Invalid signature"

    # 3. Recompute leaves and Merkle root
    try:
        expected = commitment_for(subject)
    except (KeyError, TypeError, ValueError):
        return False, "Merkle root mismatch"
    if subject.get("cryptographic_commitment") != expected:
        return False, "Merkle root mismatch"

    # 4. Optional: event hashes match the stored, intact audit chain
    if storage is not None:
        trace_id = subject["trace_id"]
        ok, _ = storage.verify_chain(trace_id)
        session = storage.get_session()
        try:
            stored = [e.event_hash for e in storage.get_events_for_transaction(session, trace_id)]
        finally:
            session.close()
        if not ok or stored != [e["event_hash"] for e in subject["events"]]:
            return False, "Audit chain mismatch"

    # 5. Mesh extension: energy drift
    energy = subject.get("energy_trace")
    if isinstance(energy, dict) and "energy_delta" in energy and "threshold" in energy:
        if energy["energy_delta"] > energy["threshold"]:
            return False, f"Energy drift: {energy['energy_delta']} > {energy['threshold']}"

    return True, "VALID"


# ------------------------------------------------------------------ internals


def _commit_fields(commit: CommitDecisionORM) -> Dict[str, Any]:
    return {
        "decision_id": commit.decision_id,
        "transaction_id": commit.transaction_id,
        "decision": commit.decision,
        "contract_hash": commit.contract_hash,
        "ledger_hash": commit.ledger_hash,
        "evidence_refs": commit.evidence_refs,
        "checks": commit.checks,
        "signature": commit.signature,
        "approver_type": commit.approver_type,
        "approver_id": commit.approver_id,
    }


def _reject_floats(value: Any) -> None:
    if isinstance(value, float):
        raise ValueError("certificate values must not contain floats (RFC 8785 subset)")
    if isinstance(value, dict):
        for item in value.values():
            _reject_floats(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_floats(item)


def _ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError('Provenance signing needs: pip install "cfa-shoc[provenance]"') from exc
    return ed25519


def _serialization():
    from cryptography.hazmat.primitives import serialization
    return serialization


def _private_key(signing_key: Any):
    ed25519 = _ed25519()
    if isinstance(signing_key, ed25519.Ed25519PrivateKey):
        return signing_key
    if isinstance(signing_key, (bytes, bytearray)) and len(signing_key) == 32:
        return ed25519.Ed25519PrivateKey.from_private_bytes(bytes(signing_key))
    raise ValueError("signing_key must be an Ed25519PrivateKey or a 32-byte seed")


def _b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    out = ""
    while number:
        number, remainder = divmod(number, 58)
        out = _B58_ALPHABET[remainder] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


def _b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        index = _B58_ALPHABET.find(char)
        if index < 0:
            raise ValueError("invalid base58 character")
        number = number * 58 + index
    pad = len(text) - len(text.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * pad + body


def _hex_leaves(values: Sequence[Any]) -> List[str]:
    """Leaf digests as hex (for test vectors and debugging)."""
    return [leaf_hash(i, v).hex() for i, v in enumerate(values)]
