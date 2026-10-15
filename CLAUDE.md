# CLAUDE.md — CFA-SHOC Phase 1 (rules for every Claude Code session)

Read this file, then `HANDOFF_FOR_CLAUDE_CODE.md`, before touching anything.

## What this repo is
A Python library that sits between an AI agent and the tools it calls. Every tool call becomes a
transaction that must pass preflight checks, a policy decision, a dispatch lease, result
verification and a commit gate, with a hash-chained audit log. Goal of this repo: make it work,
prove it with CI, then release it so it can be sold (see `docs/SALES_PLAYBOOK.md`).

## Source of truth, in order
1. The code and the CI result.
2. `HANDOFF_FOR_CLAUDE_CODE.md` (written 2026-10-15 after byte-exact extraction and test runs).
3. `docs/DRXIT_BATON_SPEC.md`, `docs/PROVENANCE_SPEC.md`.
4. The spec PDFs "CFA-SHOC v0.2" and "Phase 1 Implementation Addendum" if present in `docs/spec/`.
5. `HANDOFF.md` (repo's own, 2026-09-30): accurate list of defects, keep it.
6. `README.md`, `PROJECT_STATE.md`, `MEMORY.md`, `QUICKSTART.md`, `COMPLETION_SUMMARY.md`:
   **contain false claims** ("70+ tests, all passing", "production-ready", "22 transitions").
   Do not repeat any claim from them. Rewrite them in work step 12.

## Owner rules (non-negotiable)
- The owner works from an Android phone only and is not a programmer. Every instruction you
  give the owner must be doable in a phone browser or the GitHub / Claude apps.
- Never tell the owner to run anything locally, install anything, use a terminal, or host
  anything on their own machine. All running happens in CI (GitHub Actions) or in your session.
- Never deliver zip files to the owner. Individual plain files only.
- Never recommend vidIQ for anything.
- Verified-only claims. Never say a test passes unless it ran and passed, and say where it ran.
  Mark anything unrun as UNVERIFIED.
- Ask the owner exactly one question (with your recommended default) before anything
  irreversible, public, paid, or published: making the repo public, publishing to PyPI,
  creating a release, listing anything for sale. Routine code choices: decide, then record them
  in `DECISIONS.md`.
- Any visual asset (logo, social card, diagram for the README) is black and white, with an
  occasional fluorescent, neon or metallic highlight used only for emphasis.

## Engineering rules
- Python >= 3.11. Keep the runtime dependency list short (`sqlalchemy`, and `cryptography` once
  signing lands). `pydantic` and `python-dateutil` are declared but unused: remove them unless
  you start using them.
- Do not weaken a test to make it pass. If a test is wrong, fix it and say why in the commit.
- Every state change goes through `TransactionStateMachine.transition`. Never set `.state` by hand
  without the matching transition call.
- Keep state files current after each work step: `CURRENT_TASK.md`, `PROJECT_STATE_VERIFIED.md`,
  `DECISIONS.md`, `HANDOFF_FOR_CLAUDE_CODE.md` (update its "Status" table). Create the first
  three on your first run if they don't exist.
- Small commits, one work step per commit, message says what was verified.
