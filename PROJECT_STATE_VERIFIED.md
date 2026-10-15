# PROJECT STATE (VERIFIED) — CFA-SHOC Phase 1

This file holds only what was checked. Anything not checked is marked UNVERIFIED.
The older `PROJECT_STATE.md` contains false claims; do not rely on it.

## Purpose
A Python library that sits between an AI agent and the tools it calls. Each tool call becomes
a transaction that goes through preflight, policy, a dispatch lease, result verification and a
commit gate, with a hash-chained audit log.

## Verified so far
| Date | Check | Where it ran | Result |
|---|---|---|---|
| 2026-10-15 | Bundle unpack, size + SHA-256 per file | Claude Code session | ALL 24 FILES OK |
| 2026-10-15 | 18 originals vs. independent table in handoff section 2 | Claude Code session | 18/18 match |
| 2026-10-15 | Baseline `pytest` on the imported code (Python 3.13) | Claude Code session | 3 collection errors, all D1 (`non-default argument 'approved' follows default argument 'decision_id'`) |
| 2026-10-15 | Full suite after step 8 (151 tests), Python 3.11 / 3.12 / 3.13, DeprecationWarning as error | Claude Code session | 151 passed on each |
| 2026-10-15 | Wheel build + fresh-venv install + `import cfa_shoc` | Claude Code session and CI | 0.1.0 |
| 2026-10-15 | PyPI name `cfa-shoc` | pypi.org JSON API | 404 (free) |
| 2026-10-15 | Full suite after step 12 (214 tests) on Python 3.11 / 3.12 / 3.13 | Claude Code session | 214 passed on each |
| 2026-10-15 | `examples/demo_echo.py` | Claude Code session | DEMO OK (approved COMMITTED, denied REJECTED and never dispatched, lane C confirmed COMMITTED, 3 chains intact, certificate VALID) |
| 2026-10-15 | Wheel + `[provenance]` in a fresh venv, `cfa-shoc --version` | Claude Code session | cfa-shoc 0.1.0 |

## CI runs
| Step | Commit | Run | Result |
|---|---|---|---|
| 1 baseline | e633b49 | https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38723395836 | red: D1 TypeError at collection (expected) |
| 2-5 | 8470319..261a4a7 | runs 3-6 | red: remaining known defects, fixed in step 6 |
| 6 tests | 0154b0e | https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38724320439 | **green**, 98 tests on 3.11/3.12/3.13 |
| 7 packaging | 0c88bbd | https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38724620489 | **green**, tests x3 + package job (wheel, fresh venv, 0.1.0) |
| 8 real checks | f7368f4 | https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38725147586 | **green** |
| 9 audit + reconcile | e4b0f7e | https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38725647866 | **green** |
| 10 baton | bcdeb91 | https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38725981928 | **green** |
| 11 provenance | 4be475c | https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38726301961 | **green** (package job ran `cfa-shoc --version`) |
| 12 demo + docs | 73415c7 | https://github.com/llumnnymus-commits/Cfa-shoc-phase1/actions/runs/38726669036 | **green**: tests x3, package, demo (log ends `DEMO OK`) |

## Known defects
D1 to D19 from `HANDOFF_FOR_CLAUDE_CODE.md` section 4 are fixed (steps 1 to 12). Known limits
of 0.1.0 are listed in README "Limits" (SQLite-only testing, single-process quotas and
leases, per-process default HMAC keys, unsigned tool results, source-form certificates).

## Not done
- Step 13 (release): asked 2026-10-15; the answer was "No preference", which is not the explicit
  yes CLAUDE.md requires, so nothing was made public or published. Prepared:
  `.github/workflows/release.yml` (Trusted Publishing; runs only on a published GitHub Release;
  YAML parses; never run, so UNVERIFIED) and `docs/RELEASE_STEPS.md` (owner phone steps).
- Step 14 (sales assets) starts after step 13.
- Spec PDFs (CFA-SHOC v0.2, Phase 1 Addendum) were not attached, so the step 3 state-machine
  choice was not checked against spec section 3.2.

## Signals (fill weekly after launch)
GitHub stars, issues, PyPI downloads, Sponsors, pack sales, gig inquiries: none yet (not
launched).
