# DECISIONS — CFA-SHOC Phase 1

Format: date | decision | why | consequence. Newest at the bottom. A later entry that
supersedes an earlier one says so.

## Seeded from HANDOFF_FOR_CLAUDE_CODE.md section 6

- **2026-10-15 | Open-core model.** The library is MIT on GitHub and PyPI. Money comes from
  setup and integration services, policy packs, sponsorships and, later, a hosted audit viewer.
  | Free distribution builds trust. | See `docs/SALES_PLAYBOOK.md`.
- **2026-10-15 | DRxit baton and provenance certificates are modules of this repo**, not
  separate products. | Only CFA-SHOC has working code; the Mesh code is not in the owner's Drive.
  | `cfa_shoc/baton.py`, `cfa_shoc/provenance.py`.
- **2026-10-15 | State-machine resolution (TERMINAL vs FINAL)**, per step 3 of the handoff.
  Revisit only if the v0.2 spec says otherwise. | Recommended by the handoff. | See step 3 entry.
- **2026-10-15 | Original Drive files kept byte-exact in the first commit.** | History shows
  exactly what was inherited. | Commit `3d478cb`.

## Made during implementation
- **2026-10-15 | Step 3: TERMINAL vs FINAL.** `TERMINAL_STATES` keeps meaning "no further
  forward processing" and still includes COMMITTED. New `FINAL_STATES` = terminal states with
  zero outgoing transitions (all terminal states except COMMITTED). Only
  `compensation_succeeded` / `compensation_failed` may leave COMMITTED. Added
  COMPENSATION_REQUIRED -> COMPENSATED / COMPENSATION_FAILED (D3) and AUTHORIZED +
  `authorization_revoked` -> REJECTED (D4). The table now has 24 transitions. | The two test files
  contradicted each other (D2), and two states could strand a transaction. | One test changed
  meaning: `test_terminal_state_prevents_further_transitions` now asserts zero transitions for
  FINAL_STATES and compensation-only exits for COMMITTED. Not checked against spec section 3.2
  because the spec PDFs are not in the repo.
- **2026-10-15 | Step 5: content-addressed ledger hash (D8).** The hash covers a deep copy of
  tools/connectors/models/skills/quotas/policies plus `content_version`, which bumps only when
  that content changes. The publication `sequence` stays monotonic (the property test relies on
  it) but is not hashed, and neither is wall-clock time. | Every commit was being rejected
  because the hash changed on every refresh. | Commit-time re-reads now match unless something
  really changed. Step 8 keeps quota *usage* outside the hash for the same reason.
- **2026-10-15 | Step 5: contract hash = SHA-256 of the whole contract dict.** Contracts can be
  registered per episode in `CapabilitySnapshotService.register_contract`; otherwise
  `OrchestrationContext.contract` is used. The commit gate re-reads it. | Any change to scopes,
  pre-authorizations or caps must block a commit that was authorized under the old contract. |
  A caller-supplied `"hash"` key is just part of the content.
- **2026-10-15 | Step 5: ToolAdapter needs registered handlers; no simulated success.** A tool
  with no handler fails closed (`connector_error` -> FAILED). `query_status` without a status
  handler answers `unknown`, never a guessed success. | The old adapter returned success for
  every call (a stub presented as a feature, D11). | Tests and the demo register handlers.
- **2026-10-15 | Step 5: a normalization failure still creates an audited transaction**
  (PROPOSED -> `preflight_failed` -> REJECTED) and is never dispatched. | A refused proposal
  should leave evidence. | `execute` always returns a transaction.
- **2026-10-15 | Step 5: storage enforces dispatch leases.** `acquire_lease` raises
  `LeaseConflictError` when an unexpired lease exists for the same transaction; the orchestrator
  maps that to `authorization_revoked`. | The test `test_concurrent_lease_acquisition_conflict`
  asserted the defect ("SQLite doesn't enforce"); it now asserts what its own docstring says.
- **2026-10-15 | Step 6: idempotent replay.** If the same tenant + idempotency key already has
  a COMMITTED transaction, `execute` returns that transaction (`allowed=True`, reason
  "Idempotent replay") and does not dispatch again. REJECTED or FAILED attempts may be retried
  with the same key. | `test_duplicate_proposal_uses_same_transaction` claimed deduplication
  that did not exist. | Retries after a lost response are safe.
- **2026-10-15 | Step 6: test end states.** Every "final state is PROPOSED" or "anything goes"
  assertion now names the state the scenario must reach, taken from each test's docstring:
  default MEDIUM risk -> REJECTED by policy; low risk -> COMMITTED. Two scenarios gained one
  input so that they test what their docstring says: `test_full_transaction_lifecycle` passes
  the `read` scope it registers on the tool, and `test_terminal_state_reached_on_success` passes
  `risk_class: low`. | D15. | Weak assertions can no longer pass on broken code.
- **2026-10-15 | Step 7: package `cfa_shoc` in a `src/` layout; PyPI name `cfa-shoc`.**
  `https://pypi.org/pypi/cfa-shoc/json` returned 404 on 2026-10-15 (name free). Runtime
  dependency: `sqlalchemy` only (`pydantic`, `python-dateutil` removed, they were never
  imported). MIT LICENSE, copyright holder `llumnnymus-commits` (the GitHub account; the owner
  can change it). | D18. | Tests import `cfa_shoc.*`; `pytest.ini` sets `pythonpath = src`.
- **2026-10-15 | Step 7: timezone-aware UTC everywhere.** `cfa_shoc.utcnow()` returns
  aware UTC. Storage columns use `UTCDateTime` (store naive UTC, return aware UTC), so SQLite
  and PostgreSQL compare UTC with UTC. | `datetime.utcnow()` is deprecated from 3.12. | The
  suite runs with `-W error::DeprecationWarning` cleanly.
- **2026-10-15 | Step 7: CI actions bumped to checkout@v5 / setup-python@v6.** | CI warned
  that Node 20 actions are deprecated. | No behavior change.
- **2026-10-15 | Step 8: built-in schema validator (`cfa_shoc/schema.py`), not `jsonschema`.**
  Supports type, required, properties, additionalProperties(false), enum, min/maxLength,
  minimum/maximum, items, maxItems. Tools declare `input_schema` and optionally `output_schema`.
  | Keeps the runtime dependency list to SQLAlchemy (CLAUDE.md). | Unknown keywords are
  ignored; a full JSON Schema implementation can replace it later without API change.
- **2026-10-15 | Step 8: quotas.** Limits live in the ledger `quotas` as
  `{tenant_id | "*": {tool_id: max_calls}}` (hashed). Usage counters are kept outside the
  hash and consumed at `dispatch_started`. | Consuming quota must not change the ledger hash
  and block the commit. | Counters are in-memory per CapabilitySnapshotService (Phase 1 limit).
- **2026-10-15 | Step 8: conflicts.** Preflight refuses a proposal when another in-flight
  (non-terminal) transaction of the same tenant has the same idempotency key, or when the tool is
  declared `exclusive` and has an in-flight transaction for the tenant. RUN_TOOL_NODE gives
  PreflightService its storage when none was passed. | D11. | Without storage the check reports
  `no_conflict_skipped`.
- **2026-10-15 | Step 8: policy lanes.** A = LOW + NONE/REVERSIBLE. B = contract
  `pre_authorized` entry `{tool_id, operation ("*" ok), max_risk}` covers the proposal. C =
  opt-in with contract `confirm_up_to`: proposals at or below it wait in PREFLIGHTED
  (`confirmation_requested` event) until `RUN_TOOL_NODE.confirm(tx_id, approver_id)` or
  `decline(...)`. A confirmation request expires after 1 hour (then REJECTED). Without
  `confirm_up_to` behavior stays deny-by-default. | D12. | Medium risk without B or C is
  still REJECTED, as step 5 requires.
- **2026-10-15 | Step 8: HMAC-SHA256 signatures** over all decision fields for policy and
  commit decisions, with keys injected at construction (`signing_key=`, >= 16 bytes). Default is
  a random per-instance key, so decisions verify only within that process unless a key is
  supplied. Keys are held in `_SecretKey` (repr `<hidden>`) and never stored or logged. Step 5
  verifies signature + expiry; a failure fires `authorization_revoked`. | D13. | Asymmetric
  (Ed25519) signing arrives with provenance in step 11.
- **2026-10-15 | Step 8: result verifier.** Checks terminal success status, declared output
  schema, tool/connector match and idempotency-key echo. Tool results are not signed in
  Phase 1: `signature_valid` is reported False and is not required. | D11. | Honest reporting.
- **2026-10-15 | Step 8: commit gate** also requires state VERIFIED, a passed verification and
  a result envelope, besides unchanged ledger/contract and preconditions. | Invariant 3.3. |
- **2026-10-15 | Step 9: storage is the authority for the audit chain.** Each event row has
  `sequence` (unique per transaction), `prior_hash` and `event_hash`; `StorageManager` computes
  them on append (caller-supplied hashes are ignored). The hash covers event id, transaction id,
  sequence, type, states, actor, payload, UTC timestamp and prior hash (`cfa_shoc/audit.py`,
  shared with the in-memory `AuditLedger`, which now chains per transaction and raises if it
  ever diverges from storage). `verify_chain(tx_id) -> (ok, first_bad_index)`. | D10. |
  Limitation: rewriting the *last* event together with its hash is undetectable without an
  external anchor; provenance certificates (step 11) provide that anchor.
- **2026-10-15 | Step 9: reconciliation lives in `RUN_TOOL_NODE.reconcile(tx_id, now=None)`**
  and `ReconciliationWorker(adapter, verifier, orchestrator).run_once()` loops over it. Provider
  succeeded -> verify -> VERIFIED -> commit through the action gate; failed -> FAILED; unknown ->
  wait until the deadline (async 24 h, unknown 1 h), then PENDING_EXTERNAL -> UNKNOWN and
  UNKNOWN -> FAILED (side effect none) or COMPENSATION_REQUIRED. Compensation uses an optional
  per-tool compensator; without one a human must act. A DISPATCHED transaction with no live
  lease (dead dispatcher) -> UNKNOWN. | Reuses the one transition/commit path instead of a
  second implementation. | The old `reconcile(tx)` query method is kept.
- **2026-10-15 | Step 10: DRxit baton design choices** (spec gaps). Baton id = SHA-256 of the
  canonical baton without `signature`; successors cite it as `parent_baton_hash`. `seal`
  validates content (structure, evidence, contradictions, legs/parent, transactions, rollback,
  evaluation) and refuses with `BatonRejected` before anything is stored. `validate` adds the
  signature, registry (`{capability: [authorities]}`, checks 3-4) and artifact checks and
  records the outcome on the stored baton: failure -> REJECTED (fail closed). `acknowledge`
  (no key) only accepts a SEALED baton whose last validation passed and whose content still
  hashes to its id. "Final state" for cited transactions means terminal (COMMITTED counts).
  Unknown fields are rejected, which keeps raw hidden reasoning out (rule 5). Signing is
  HMAC-SHA256 until provenance (Ed25519) lands. | The spec leaves these open. | Recorded in
  `cfa_shoc/baton.py` docstrings.
- **2026-10-15 | Step 11: provenance implements the spec's source form only.** Proof type
  `Ed25519Signature2020`, signature over JCS(credentialSubject), issuer `did:key` (Ed25519,
  multicodec 0xed01, base58btc). Certificates are VC-shaped and verifiable with this library
  (`verify_certificate`, `cfa-shoc verify`), not claimed verifiable by generic VC tooling; the
  README says so. The W3C `eddsa-jcs-2022` Data Integrity form is not implemented (future
  work), so the W3C cryptosuite document was not needed for this step. | Spec allows it if
  stated. | `cryptography` is the optional extra `[provenance]` (also in `[dev]` for CI).
- **2026-10-15 | Step 11: hashes inside certificates.** input/output/policy/commit hashes are
  SHA-256 of `canonical_json` (sorted keys, compact). Merkle leaves use strict JCS, which
  refuses floats, so a float in an extension is an error rather than a silent RFC 8785
  mismatch. Leaf order is fixed by the spec; Mesh extension leaves follow in the order
  expert_calls, symbolic_steps, adjudication, energy_trace. Fixed vectors for 1/3/5/7 leaves
  live in `tests/test_provenance.py`, cross-checked against hand-built trees.
- **2026-10-15 | Step 12: README claims are tested.** `tests/test_readme.py` executes the
  README quick example and checks its printed output, checks that every linked file exists,
  that every test or class named in the guarantees table exists in the linked file, and that
  the state/transition counts in the text match the code. The demo runs as its own CI job and
  asserts every outcome. | "Every claim in the README links to a passing test" must not rot.
  | Historical docs moved to `docs/history/` with the required banner; CLAUDE.md paths updated.
