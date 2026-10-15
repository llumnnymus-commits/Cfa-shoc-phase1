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
