# Provenance Certificates — implementation spec for `cfa_shoc/provenance.py`

Source: the owner's NeuroSymbolic Mesh SPEC.md, sections 2.1 to 2.3 (certificate shape, Merkle
construction, verification procedure). The Mesh code itself is not in the owner's Drive, so this
is a fresh implementation of that spec, adapted to CFA-SHOC transactions. Lines marked
**(added)** fill gaps the source leaves open.

## Why it sells
A committed CFA-SHOC transaction can be exported as a signed certificate. Anyone holding the
issuer's public key can check it offline: what tool was called, with what input, what came
back, what policy approved it, and that none of it was edited afterwards. Auditors,
compliance teams and clients paying for agent work all need this proof.

## Certificate shape (W3C Verifiable Credential layout, from the source)
```json
{
  "@context": ["https://www.w3.org/ns/credentials/v2"],
  "type": ["VerifiableCredential", "AgentActionProvenance"],
  "issuer": "did:key:<issuer public key>",
  "validFrom": "ISO-8601 UTC",
  "credentialSubject": {
    "trace_id": "<transaction uuid>",
    "task": {"tool_id": "...", "operation": "...", "principal_id": "...", "tenant_id": "..."},
    "input":  {"hash": "sha256 of canonical input_payload"},
    "output": {"hash": "sha256 of canonical observation content"},
    "events": [{"index": 0, "event_type": "...", "event_hash": "..."}],
    "policy_decision": {"decision_id": "...", "lane": "A|B|C", "hash": "..."},
    "commit_decision": {"decision_id": "...", "hash": "..."},
    "baton_ref": "optional DRxit baton hash",
    "cryptographic_commitment": {
      "merkle_root": "hex", "tree_depth": 0, "leaf_count": 0, "algorithm": "SHA256"
    }
  },
  "proof": {
    "type": "Ed25519Signature2020",
    "created": "ISO-8601 UTC",
    "verificationMethod": "did:key:<...>#<...>",
    "proofPurpose": "assertionMethod",
    "proofValue": "z<base58btc signature>"
  }
}
```
The source uses `did:mesh:orchestrator_01#key-1` and `Ed25519Signature2020`.
**(added)** For interoperability, also support the W3C Data Integrity form
`{"type": "DataIntegrityProof", "cryptosuite": "eddsa-jcs-2022"}`, which signs a
JSON-canonicalized document. Check the current W3C "Data Integrity EdDSA Cryptosuites"
document before finalizing. If you implement only the source form, say so in the README: the
certificate is VC-shaped and verifiable with this library, not with generic VC tooling.

## Merkle tree (from the source, adapted)
Leaves, in this exact order:
`0: trace_id`, `1: task`, `2: input.hash`, `3: output.hash`, `4..N-1: each event_hash in
order`, `N: policy_decision.hash`, `N+1: commit_decision.hash`, `N+2: baton_ref` (only if
present).

- Leaf hash = `SHA256( str(leaf_index) + ":" + JCS(value) )` using UTF-8 bytes.
- Internal node = `SHA256( left_digest_bytes + right_digest_bytes )`.
- **(added)** When a level has an odd number of nodes, carry the last node up unchanged. Do not
  duplicate it.
- **(added)** JCS = RFC 8785 JSON canonicalization. For Phase 1, use
  `json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`. Restrict
  certificate values to strings, ints, bools, null, lists and objects with no floats, so this
  matches RFC 8785 exactly.
- `tree_depth` = number of levels above the leaves. `leaf_count` = number of leaves.

Mesh-specific leaves from the source (`expert_calls`, `symbolic_steps`, `adjudication`,
`energy_trace`) are an optional extension: accept them if present and append them after the
fixed leaves in the order given here.

## Verification procedure (from the source, adapted)
```
verify_certificate(cert, trusted_issuers) -> (ok: bool, reason: str)
1. issuer must be in trusted_issuers                     else "Untrusted issuer"
2. Ed25519 signature over JCS(credentialSubject) verifies else "Invalid signature"
3. recompute leaves and Merkle root; must equal
   credentialSubject.cryptographic_commitment.merkle_root else "Merkle root mismatch"
4. (optional, with storage access) every event_hash matches the stored audit chain
   for trace_id                                          else "Audit chain mismatch"
5. (Mesh extension) energy_delta <= threshold            else "Energy drift: ..."
```

## API
```python
issue_certificate(transaction_id, storage, signing_key) -> dict
verify_certificate(cert: dict, trusted_issuers: set[str], storage=None) -> tuple[bool, str]
merkle_root(leaves: list[bytes]) -> tuple[str, int]     # (hex root, depth)
```
Command line: `cfa-shoc verify cert.json --trust did:key:...` prints `VALID` or the reason,
and exits 0 or 1.

Keys: Ed25519 via the `cryptography` package, an optional extra `cfa-shoc[provenance]`. The
signing key is passed in and never written to the DB or logs.

## Acceptance tests (step 11 is done when all pass in CI)
1. Issue then verify a committed transaction from the demo. Result: VALID.
2. Flip one character of `output.hash` gives "Invalid signature". Re-sign with the same key
   after the flip gives "Merkle root mismatch".
3. Remove one event gives "Merkle root mismatch".
4. Wrong issuer gives "Untrusted issuer".
5. Odd leaf counts 1, 3, 5 and 7 give the documented roots. Put fixed test vectors in the test
   file so other implementations can check against them.
6. Certificate requested for a transaction not in COMMITTED raises an error.
7. With storage: tamper with a stored event row, and verification step 4 fails.
