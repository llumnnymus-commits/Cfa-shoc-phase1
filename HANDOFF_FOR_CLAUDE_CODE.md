# HANDOFF FOR CLAUDE CODE — finish CFA-SHOC Phase 1 and get it selling

Written 2026-10-15. Read `CLAUDE.md` first (owner rules). This file is the plan. Work top to
bottom, one step per commit, and update the **Status** table at the end after each step.

---

## 1. What the owner does (phone only, about 10 minutes)

1. **Create the repo.** In a phone browser open https://github.com/new while signed in as
   `llumnnymus-commits`. Name: `cfa-shoc-phase1`. Private. **Tick "Add a README file"** so the
   repo isn't empty. Claude Code will replace that README with the real one. Tap
   *Create repository*.
2. **Start Claude Code** on that repo (claude.ai/code in the phone browser, or the Claude app ›
   Code).
3. **Attach ONE file:** `CFA_SHOC_BUNDLE.txt`. It is plain text, not a zip, and holds every
   project file plus this plan. Paste this prompt:

   > The attached CFA_SHOC_BUNDLE.txt contains this whole project. Save it to the repo root
   > byte-for-byte, then follow the UNPACK section at the top of that file. After unpacking,
   > read CLAUDE.md and HANDOFF_FOR_CLAUDE_CODE.md and execute the work plan from step 0.
   > Stop only for the owner questions the handoff lists, each with your recommended default.

4. **Optional:** attach the two spec PDFs ("CFA-SHOC v0.2", 36 pages, and "Phase 1
   Implementation Addendum", 22 pages) in the same message, and Claude Code will put them in
   `docs/spec/`. They are in the claude.ai chat
   https://claude.ai/chat/a69ed85b-87e0-4d31-b97a-b77d105c1d57 and are not in Google Drive.
   With the bundle that is 3 uploads, under the 10-file limit.

Alternative with zero uploads: once the repo exists, reply "push CFA-SHOC" in the Claude chat
that made the bundle, and that session pushes everything itself.

The owner never runs code. CI (GitHub Actions) and the Claude Code session do all running.

---

## 2. Files and their exact bytes

18 files were pulled byte-for-byte from the owner's Google Drive folder "CFA-SHOC Phase 1"
(Drive id `1k7-Vg-chZ7YVOqi1Rfl2dtdE1NdOiFi-`). Sizes match what Drive reports. The
SHA-256 prefixes below are for the files as they were in Drive. Check them in step 0 before
editing anything.

| Path in repo | Bytes | SHA-256 (first 16) |
|---|---|---|
| src/domain.py | 8530 | d7e7f80e4ba59e90 |
| src/state_machine.py | 8097 | 4692ee9c84b9688e |
| src/storage.py | 9569 | 45bb4d014816e39f |
| src/services.py | 22453 | e8cddb4da453a521 |
| src/orchestrator.py | 24033 | 896f5eeeb312d649 |
| tests/__init__.py | 202 | d17e193078f0a1de |
| tests/conftest.py | 747 | f1c8f67e5a820ff2 |
| tests/test_state_machine.py | 12724 | 656b763fa56f70d4 |
| tests/test_orchestration.py | 16355 | 7c7ca70a7d8a4eb0 |
| tests/test_chaos_and_properties.py | 17724 | aadfb8d2bc3e4a3e |
| pyproject.toml | 580 | c03f6615b55f7d40 |
| pytest.ini | 696 | 9ef77e38e86f9731 |
| README.md | 13390 | 8e59483ab8308bed |
| PROJECT_STATE.md | 15600 | 9f80e79f3c0dc01c |
| QUICKSTART.md | 10094 | 453060c06a1f1355 |
| COMPLETION_SUMMARY.md | 16811 | 941c84aa4d85daef |
| MEMORY.md | 10608 | 015e257b6f363e3d |
| HANDOFF.md | 3304 | 7ad8af882995536b |

Added on 2026-10-15 (new files, place them as shown):

| Path in repo | Purpose |
|---|---|
| CLAUDE.md | Owner rules for every session |
| HANDOFF_FOR_CLAUDE_CODE.md | This plan |
| .github/workflows/ci.yml (sent as `ci.yml`) | Free CI on Python 3.11 / 3.12 / 3.13 |
| docs/DRXIT_BATON_SPEC.md | Spec for step 10 |
| docs/PROVENANCE_SPEC.md | Spec for step 11 |
| docs/SALES_PLAYBOOK.md | Step 14 and what the owner does to sell |

`src/__init__.py` does not exist (the old docs claim it does). Step 7 handles packaging.

---

## 3. Verified state on 2026-10-15 (what was actually run)

Run in a cloud sandbox with Python 3.13. PyPI was unreachable there, so pytest and
SQLAlchemy could not be installed.

| Check | Result |
|---|---|
| All 10 `.py` files compile | PASS |
| `import src.domain` on the exact Drive file | FAIL: `TypeError: non-default argument 'approved' follows default argument 'decision_id'` |
| `tests/test_state_machine.py` (exact file), run on a temp copy with only the PolicyDecision field-order fix, using a minimal pytest stand-in | **33 passed, 0 failed** |
| Terminal-state test from `test_chaos_and_properties.py` against the state machine | Would FAIL: COMMITTED is terminal but has 2 outgoing transitions |
| Legal transitions defined | **21** (docs say 22) |
| Test functions in repo | 68 (33 state machine, 14 orchestration, 21 chaos/property). Docs say "70+, all passing", which is false |
| `test_orchestration.py`, `test_chaos_and_properties.py` | NOT RUN (need SQLAlchemy and real pytest). Read-through shows they will fail; see defects D5 to D9 |

---

## 4. Defect list (all found by reading or running the code)

**Blocking import**
- **D1** `src/domain.py` `PolicyDecision`: `decision_id` (has a default) comes before required
  fields, so the whole package fails at import.

**State machine (`src/state_machine.py`)**
- **D2** COMMITTED is in `TERMINAL_STATES` but has transitions `compensation_succeeded` and
  `compensation_failed`. `test_chaos_and_properties.py::test_terminal_state_prevents_further_transitions`
  asserts terminal states have zero transitions. `test_state_machine.py` asserts COMMITTED is
  terminal *and* that COMMITTED→COMPENSATED works. The tests contradict each other.
- **D3** COMPENSATION_REQUIRED has no outgoing transitions and is not terminal, so a
  transaction that reaches it is stuck forever.
- **D4** AUTHORIZED has only one exit (`dispatch_started`). The orchestrator fires
  `policy_denied` from AUTHORIZED (illegal, raises) and leaves the transaction stranded in
  AUTHORIZED when lease acquisition fails.

**Orchestrator ↔ services mismatch (`src/orchestrator.py` vs `src/services.py`)**
- **D5** Calls that do not match the real API:

  | Orchestrator calls | Real API |
  |---|---|
  | `normalizer.normalize(proposal, proposal_id, episode_id, task_node_id)` | `normalize(proposal_input, ledger) -> (ActionProposal or None, error or None)` |
  | `preflight.check(transaction, ledger)` | `check(proposal: ActionProposal, ledger, episode_contract)` |
  | `policy_point.evaluate(transaction, ledger)` | `evaluate(proposal: ActionProposal, ledger, contract)` |
  | `tool_adapter.invoke(request)` | `dispatch(request) -> dict` |
  | `verifier.verify(transaction, raw, ledger)` | `verify(raw_result: dict, tx)` |
  | `commit_membrane.commit(request, observation)` | `action_gate(request, tx, current_ledger, current_contract) -> (bool, CommitDecision or None)` |
  | `audit_ledger.append_event(session, ...)` | `append(event: TransactionEvent)` |
  | `action_proposal.rollback_strategy` | field does not exist on ActionProposal |
  | `raw_result.external_operation_id`, `.transport_status` | `dispatch` returns a dict, not an object |
- **D6** `PreflightService.check` reads `proposal.tenant_id`; ActionProposal has no `tenant_id`.
- **D7** `ActionNormalizer.normalize` makes new UUIDs for proposal/episode/task ids, so the
  context ids the caller passed are lost.
- **D8** The ledger hash includes a timestamp (`CapabilitySnapshotService.refresh_snapshot`),
  so every refresh gets a new hash. `CommitMembrane.action_gate` rejects when the ledger hash
  changed, so **every commit would be rejected**. The hash must cover content only (tools,
  connectors, quotas, policies, sequence of the last real change), not wall-clock time.
- **D9** Transaction state is saved to storage only at a few points. The durable record and the
  in-memory state disagree after most transitions.

**Audit trail**
- **D10** `AuditLedger` keeps its hash chain in a Python list in memory. `StorageManager.append_event`
  stores events without the chain. Nothing durable is tamper-evident. The chain must be computed
  on append and stored (`prior_hash`, `event_hash` columns), and needs a `verify_chain(tx_id)`.

**Stubs presented as features**
- **D11** `_check_schema`, `_check_quota`, `_check_risk_policy` and `_check_no_conflict` all
  return True. `ResultVerifier.verify` hardcodes every check to True.
- **D12** Policy lanes B (pre-authorized in contract) and C (explicit confirmation) are not
  implemented. Everything not LOW risk with NONE/REVERSIBLE side effects is denied.
- **D13** "Signatures" are bare unkeyed SHA-256 of the data, so anyone can forge them.
  `_verify_authorization` never checks the signature.

**Tests and config**
- **D14** `test_orchestration.py` fixtures build `ActionNormalizer()` and `PreflightService()`
  with no args (both need `capability_service`). The same applies to `PreflightService()` and
  `preflight.check(tx, snapshot)` in `test_chaos_and_properties.py`.
- **D15** Several orchestration tests assert the final state is PROPOSED, which passes for broken
  code. They must assert the real expected end state.
- **D16** In `tests/conftest.py`, `tmp_path_factory.mktemp("data").join("test.db")` uses
  `.join` on a `pathlib.Path`, which raises AttributeError. Use `/ "test.db"`. The fixture is
  currently unused.
- **D17** `pytest.ini` sets `timeout = 30`, but `pytest-timeout` is not a dev dependency. It
  also sets `minversion = 3.11`, which is pytest's own minimum version, not Python's.
- **D18** Packaging: no package config. Code imports `src.*`, which can't ship to PyPI.
  `pydantic` and `python-dateutil` are declared but never imported. `storage.py` uses the
  deprecated `sqlalchemy.ext.declarative.declarative_base`. `datetime.utcnow()` is deprecated
  from 3.12 onward.
- **D19** The docs (README, PROJECT_STATE, MEMORY, QUICKSTART, COMPLETION_SUMMARY) claim
  "production-ready", "70+ tests, all passing" and "22 transitions". All false. QUICKSTART's
  example has no `risk_class`, so the default MEDIUM would be denied.

---

## 5. Work plan (one step per commit, in this order)

Each step has a **Done when** line. A step is done only when its check ran green in CI.

### Step 0. Place and verify files
- Unpack `CFA_SHOC_BUNDLE.txt` with the script in its UNPACK section. The script writes every
  file to its path and checks every SHA-256. It must print `ALL 24 FILES OK`. If anything fails,
  stop and report which file. Never retype file contents by hand.
- Delete `CFA_SHOC_BUNDLE.txt` and the unpack script after a clean unpack. They are not part of
  the project.
- If spec PDFs were attached, save them as `docs/spec/CFA-SHOC_v0.2.pdf` and
  `docs/spec/Phase1_Addendum.pdf`.
- Verify every original against the SHA-256 table below. Stop and report if any differs.
- Create `CURRENT_TASK.md`, `PROJECT_STATE_VERIFIED.md` and `DECISIONS.md` (seed DECISIONS
  with section 6 below).
- **Done when:** the tree matches section 2, all 18 hashes match, and the commit is pushed.

### Step 1. CI baseline
- Add `"pytest-timeout>=2.2"` to the dev extra. In `pytest.ini`, replace `minversion = 3.11`
  with `minversion = 7.0`.
- Push and let CI run. It is expected to go red with the D1 import error. Record that run's URL
  in PROJECT_STATE_VERIFIED.md as the baseline.
- **Done when:** CI runs on push and the failure is the known D1 error.

### Step 2. Fix D1
- In `PolicyDecision`, move `decision_id: UUID = field(default_factory=uuid4)` below
  `ledger_hash`. This exact change was tested on 2026-10-15 and is enough for
  `test_state_machine.py` to pass 33/33. Check every dataclass in domain.py for the same
  pattern.
- **Done when:** `pytest tests/test_state_machine.py` shows 33 passed in CI.

### Step 3. State-machine model (D2, D3, D4)
Recommended resolution (record it in DECISIONS.md):
- Keep `TERMINAL_STATES` meaning "no further forward processing"; COMMITTED stays in it. Add
  `FINAL_STATES` = terminal states with zero outgoing transitions (everything except
  COMMITTED). Allow compensation events out of COMMITTED only.
- Change `test_terminal_state_prevents_further_transitions` so it asserts zero transitions for
  `FINAL_STATES`, and that COMMITTED allows only the two compensation events. This is the only
  test whose meaning changes. Explain it in the commit message.
- Add `(COMPENSATION_REQUIRED, "compensation_succeeded") -> COMPENSATED` and
  `(COMPENSATION_REQUIRED, "compensation_failed") -> COMPENSATION_FAILED`.
- Add `(AUTHORIZED, "authorization_revoked") -> REJECTED` for expired or invalid policy
  decisions and for lease conflicts.
- Add tests for each new transition. Update the docstring count to the real number.
- If the spec PDFs are in the repo, check this against spec section 3.2. If the spec disagrees,
  follow the spec and note the difference in DECISIONS.md.
- **Done when:** all state-machine tests in both files pass in CI.

### Step 4. Domain fixes (D6, D7)
- ActionProposal: add `principal_id: str = ""`, `tenant_id: str = ""` and
  `rollback_strategy: RollbackStrategy = RollbackStrategy.NONE`.
- `ActionNormalizer.normalize(proposal_input, ledger, *, proposal_id=None, episode_id=None,
  task_node_id=None, principal_id="", tenant_id="")`: use the passed ids when given. Read
  `rollback_strategy` from the input. Reject unknown `risk_class` or `side_effect_class` values
  with an error tuple instead of raising ValueError.
- **Done when:** unit tests for the normalizer cover valid input, an unregistered tool, a bad
  enum, and id preservation, and they pass.

### Step 5. Rewire the orchestrator (D5, D8, D9)
- Add `contract: Dict[str, Any]` (scopes, hash, pre_authorized list, max_risk) to
  OrchestrationContext, defaulting to `{}`.
- Make every call match the real API in the D5 table. Keep the ActionProposal object and pass
  it (not the ToolTransaction) to preflight and policy.
- Wrap the dict that `ToolAdapter.dispatch` returns into `RawResult` in one helper, and use that
  helper everywhere.
- D8: make the ledger hash content-addressed. Leave timestamp and published_at out of the hash.
  Bump `snapshot_sequence` only when content changes. Step 9 re-reads the ledger and contract
  through `capability_service` and passes them to `action_gate`.
- D9: in `_transition`, persist the new state and append the event inside one DB transaction.
  Remove the scattered `transaction.state = ...` lines.
- Step 5 failure and lease failure use `authorization_revoked`.
- Commit only through `CommitMembrane.action_gate`. Save the returned CommitDecision with
  `storage.save_commit_decision`.
- **Done when:** a low-risk `tool_echo` call ends COMMITTED with a full event trail, a
  medium-risk call ends REJECTED (policy), and an unregistered tool is refused before any
  transaction is dispatched. All three are asserted in tests and pass.

### Step 6. Fix the test suite (D14, D15, D16)
- Fixtures build services with a shared `CapabilitySnapshotService` that has `tool_echo`
  registered.
- Replace every "final state is PROPOSED" assertion with the state the scenario must reach.
- Fix the conftest path join.
- **Done when:** all original 68 tests (as amended in steps 3 and 6) plus the new ones pass in CI
  on Python 3.11, 3.12 and 3.13.

### Step 7. Packaging (D18)
- Move the code to `src/cfa_shoc/` with an `__init__.py` exporting the public API and
  `__version__ = "0.1.0"`. Imports become `cfa_shoc.*`, and the tests change to match.
- In pyproject, set setuptools package discovery for the `src/` layout. Remove the unused
  `pydantic` and `python-dateutil`. Add `LICENSE` (MIT, copyright holder = the GitHub account
  name unless the owner gives another).
- Replace the `declarative_base` import with `sqlalchemy.orm.declarative_base`. Replace
  `datetime.utcnow()` with timezone-aware UTC. Keep SQLite comparisons consistent: store UTC
  and compare UTC.
- Check that `cfa-shoc` is free on PyPI (https://pypi.org/project/cfa-shoc/ should 404). If
  it is taken, propose a name in the owner question in step 13.
- **Done when:** CI builds the wheel (`python -m build`), installs it into a fresh venv, and
  `python -c "import cfa_shoc; print(cfa_shoc.__version__)"` prints 0.1.0. The full suite still
  passes.

### Step 8. Replace stubs with real checks (D11, D12, D13)
- **Schema:** every registered tool carries a JSON Schema for its arguments (and optionally
  its output). Validate with the `jsonschema` package (one dependency) or a small built-in
  validator for `type`, `required`, `properties`, `enum`, `maxLength`. Record which in
  DECISIONS.md.
- **Quota:** per-tenant, per-tool counters in the snapshot, decremented on dispatch.
- **Risk policy:** `contract["max_risk"]` caps the allowed RiskClass.
- **No conflict:** query storage for a non-final transaction with the same idempotency key, or
  the same tool and tenant when the tool is declared exclusive.
- **Lane B:** `contract["pre_authorized"]` is a list of `{tool_id, operation, max_risk}`. A
  match means approve.
- **Lane C:** policy returns `requires_confirmation`. The transaction stays PREFLIGHTED. A new
  `RUN_TOOL_NODE.confirm(transaction_id, approver_id)` issues the signed decision and fires
  `policy_approved`.
- **Signatures:** HMAC-SHA256 (stdlib `hmac`) with a key injected at construction for policy and
  commit decisions. `_verify_authorization` checks signature and expiry. Never log the key.
- **Result verifier:** real checks (status terminal, output schema if declared, tool and
  connector match, idempotency key echoed back when the adapter supports it).
- **Done when:** each check has a failing-case test and a passing-case test, all green.

### Step 9. Reconciliation and durable audit (D10)
- Store `prior_hash` and `event_hash` per event in the DB, chained per transaction. Add
  `verify_chain(transaction_id) -> (ok, first_bad_index)`. Add a test that edits a stored row
  and detects the tampering.
- `ReconciliationWorker`: polls UNKNOWN and PENDING_EXTERNAL through
  `tool_adapter.query_status` and fires the reconcile or compensation events. Test it with a
  fake adapter that returns succeeded, failed and unknown.
- **Done when:** the tamper test and the reconciliation tests pass.

### Step 10. DRxit baton module
- Implement `docs/DRXIT_BATON_SPEC.md` as `cfa_shoc/baton.py`, with tests.
- **Done when:** the acceptance tests in that spec pass.

### Step 11. Provenance certificate module
- Implement `docs/PROVENANCE_SPEC.md` as `cfa_shoc/provenance.py`, plus a console entry point
  `cfa-shoc verify <certificate.json>`. Add `cryptography` as an optional extra
  `[provenance]`.
- **Done when:** the acceptance tests in that spec pass, including tamper detection.

### Step 12. Demo and honest docs (D19)
- Add `examples/demo_echo.py`. It registers `tool_echo`, runs one approved call, one denied
  call and one lane-C call with confirmation, prints the audit trail, verifies the chain and
  issues a provenance certificate. Run it in CI as its own job.
- Rewrite README.md from scratch: what it is, a 20-line example that the demo proves works,
  the guarantees **with the test that proves each one**, and limits. Add a CI badge.
- Move PROJECT_STATE.md, MEMORY.md, QUICKSTART.md, COMPLETION_SUMMARY.md and HANDOFF.md to
  `docs/history/`. Put a one-line banner at the top of each: "Historical, contains claims that
  were false when written; see README."
- Add CHANGELOG.md with the 0.1.0 entry.
- **Done when:** the demo job is green and every claim in the README links to a passing test.

### Step 13. Release (OWNER QUESTION, ask once)
Ask: "CI is green and the demo runs. OK to make the repo public under MIT and publish
cfa-shoc 0.1.0 to PyPI? (Recommended: yes.)"
- On yes: make the repo public. Add a `release.yml` workflow using PyPI Trusted Publishing,
  which needs no stored password. Give the owner the phone steps to add the trusted publisher
  on pypi.org: Account › Publishing › add a GitHub publisher with owner, repo and workflow
  name. Then tag `v0.1.0` and create the GitHub Release with notes from CHANGELOG.
- **Done when:** https://pypi.org/project/<name>/ shows 0.1.0, and a fresh CI job runs
  `pip install <name>` and the demo against it.

### Step 14. Sales assets
Follow `docs/SALES_PLAYBOOK.md`. Build what it lists for Claude Code. Every listing or post is
an owner question before it goes live.

---

## 6. Decisions already made (seed DECISIONS.md with these)

- 2026-10-15: Open-core model. The library is MIT on GitHub and PyPI (free distribution builds
  trust). Money comes from setup and integration services, policy packs, sponsorships, and later
  a hosted audit viewer. See SALES_PLAYBOOK.
- 2026-10-15: DRxit baton and NeuroSymbolic Mesh provenance certificates are folded into this
  repo as modules, not separate products. Only CFA-SHOC has working code. The Mesh code is not
  in the owner's Drive.
- 2026-10-15: State-machine resolution in step 3 (TERMINAL vs FINAL). Recommended, revisit
  only if the v0.2 spec says otherwise.
- 2026-10-15: The original Drive files are kept byte-exact in the first commit so the history
  shows exactly what was inherited.

## 7. Do not repeat

- Do not re-derive the defect list. It was checked against the code on 2026-10-15.
- Do not claim tests pass from reading them. Only CI counts.
- Do not try to create GitHub repos from a session bound to other repos. The owner creates the
  empty repo (section 1).

## Status (update after every step)

| Step | State | Evidence (CI run URL / commit) |
|---|---|---|
| 0 Place and verify | done | unpack ALL 24 FILES OK; 18/18 hashes match; commit 3d478cb |
| 1 CI baseline | done | red with D1 as expected: https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38723395836 (commit e633b49) |
| 2 Fix D1 | done | test_state_machine 33 passed in session; CI run https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38723435334 red only on later defects (commit 8470319) |
| 3 State machine | done | 41 + chaos state tests pass; commit 9ef97df; green from step 6 CI |
| 4 Domain | done | test_normalizer 8 passed; commit 3abed57; green from step 6 CI |
| 5 Orchestrator | done | test_orchestrator_flow 14 passed; commit 261a4a7; green from step 6 CI |
| 6 Tests | done | first green CI, 98 tests on 3.11/3.12/3.13: https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38724320439 (commit 0154b0e) |
| 7 Packaging | done | CI green incl. wheel install + version 0.1.0: https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38724620489 (commit 0c88bbd) |
| 8 Real checks | done | CI green: https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38725147586 (commit f7368f4) |
| 9 Audit and reconcile | done | CI green: https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38725647866 (commit e4b0f7e) |
| 10 Baton | done | spec tests 1-9 pass; CI green: https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38725981928 (commit bcdeb91) |
| 11 Provenance | done | spec tests 1-7 pass in session (211 total on 3 Pythons); CI run https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38726301961 (commit 4be475c) |
| 12 Demo and docs | done (CI pending) | demo prints DEMO OK in session; 214 passed on 3 Pythons; see PROJECT_STATE_VERIFIED.md for the run |
| 13 Release | pending (owner question) | |
| 14 Sales assets | pending (owner question per listing) | |

**Next action:** Step 13 owner question (public repo + MIT + PyPI release), asked once with the recommended default.
