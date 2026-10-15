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

## CI runs
| Step | Commit | Run | Result |
|---|---|---|---|

## Known defects
See `HANDOFF_FOR_CLAUDE_CODE.md` section 4 (D1 to D19). Each step's status is in that file's
Status table.

## Signals (fill weekly after launch)
GitHub stars, issues, PyPI downloads, Sponsors, pack sales, gig inquiries: none yet (not
launched).
