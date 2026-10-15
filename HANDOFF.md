# HANDOFF — CFA-SHOC Phase 1 (updated 2026-09-30)

## Read this first

The other docs in this folder (README, PROJECT_STATE, MEMORY, QUICKSTART,
COMPLETION_SUMMARY) say "complete" and "70+ tests, all passing". **Those claims
are false.** The tests were never run, and the code does not import.

## Backup status

- **Google Drive:** DONE. All 17 project files uploaded to the "CFA-SHOC Phase 1"
  folder (id 1k7-Vg-chZ7YVOqi1Rfl2dtdE1NdOiFi-), with src/ and tests/ subfolders.
  Every file's byte size in Drive matches the local original.
- **GitHub:** BLOCKED. The session can push to existing repos under
  `llumnnymus-commits`, but it cannot create new repos (the API returns
  "sessions are bound to their configured repositories"). The two existing repos
  (Mastermind-triad, beat-synced-music-video-gen) are public and unrelated, so
  the project was not pushed into them.
  - To unblock: create an empty repo at https://github.com/new named
    `cfa-shoc-phase1` (private, no README). Then the next session runs
    add_repo(llumnnymus-commits, cfa-shoc-phase1, push) and pushes commit history.

## Verified state of the code

- Syntax: all .py files compile.
- Test functions: 68 (33 state machine, 14 orchestration, 21 chaos/property),
  not "70+".
- Tests executed: NONE. pytest is not installed and pip cannot reach PyPI from
  the cloud session.

## Known defects (found by reading the code)

1. **Blocks everything:** `src/domain.py` `PolicyDecision` has a defaulted field
   (`decision_id`) before non-default fields. `import src.domain` raises
   `TypeError: non-default argument 'approved' follows default argument`, so
   every module and every test fails at import.
2. `src/orchestrator.py` calls service methods that don't exist or have
   different signatures:
   - `normalizer.normalize(proposal, id, id, id)`: the real signature is
     `normalize(input, ledger)` and it returns a `(proposal, error)` tuple.
   - `preflight.check(tx, ledger)` is missing the `episode_contract` arg.
   - `policy_point.evaluate(tx, ledger)` is missing the `contract` arg.
   - `tool_adapter.invoke(...)`: the method is named `dispatch`.
   - `verifier.verify(tx, raw, ledger)`: the real signature is `verify(raw, tx)`.
   - `commit_membrane.commit(...)`: the method is named `action_gate` and takes
     different args.
   - `audit_ledger.append_event(session, ...)`: the method is `append(event)`.
   - `action_proposal.rollback_strategy`: this field doesn't exist on
     ActionProposal.
   - `raw_result.external_operation_id` is attribute access, but ToolAdapter
     returns a dict.
3. `PreflightService.check` reads `proposal.tenant_id`, which isn't on
   ActionProposal.
4. The tests construct `ActionNormalizer()` and `PreflightService()` with no
   args, but both require `capability_service`.
5. `pytest.ini` sets `timeout = 30`, but `pytest-timeout` isn't in the dev deps.
6. The docs list `src/__init__.py`, but it doesn't exist.
7. Several orchestration tests assert the final state is PROPOSED, which is
   weak or incorrect.

## Next action (single)

Fix defect #1, then reconcile the orchestrator with the services API (#2 to #4),
then get the tests to run in an environment that has pytest and correct the docs
to match reality. Do not claim anything passes until it has been run.
