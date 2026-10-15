# CURRENT TASK

**Task:** Execute `HANDOFF_FOR_CLAUDE_CODE.md` section 5, one step per commit.

**Progress:** steps 0-12 done and green in CI. Step 13 prepared but not released: the owner
answered "No preference", which is not an explicit yes, so nothing is public or published.

**Success criteria:** step 13 done = https://pypi.org/project/cfa-shoc/ shows 0.1.0 and the
Release workflow's `verify-from-pypi` job (pip install + demo) is green. Step 14 only after.

**Next action:** Get an explicit yes or no from the owner on the release. On yes: owner follows
`docs/RELEASE_STEPS.md`; Claude verifies the Release run and the PyPI page, updates the README
status line ("not yet published"), CHANGELOG (release date) and these files.
