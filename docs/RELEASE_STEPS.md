# Release steps for the owner (phone only, about 10 minutes)

Do these only if you want CFA-SHOC public and on PyPI. Each step is reversible except
step 4 (a published PyPI version can be yanked but never re-used).

1. **Merge the work into main.** GitHub app or github.com in the phone browser > repository
   `Cfa-shoc-phase1` > the yellow banner "claude/new-session-bszvgt had recent pushes" >
   *Compare & pull request* > *Create pull request* > wait for the green checks >
   *Merge pull request* > *Confirm merge*.
2. **Make the repository public.** Repository > *Settings* > scroll to *Danger Zone* >
   *Change visibility* > *Make public* > confirm.
3. **Tell PyPI to trust this repository** (no password is stored anywhere).
   pypi.org > create an account or log in (turn on 2-factor when asked) > your account menu >
   *Your account* > *Publishing* > *Add a new pending publisher* > tab **GitHub**, and fill in:
   - PyPI Project Name: `cfa-shoc`
   - Owner: `llumnnymus-commits`
   - Repository name: `Cfa-shoc-phase1`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
   Then *Add*.
4. **Publish the release.** Repository > *Releases* > *Draft a new release* > *Choose a tag* >
   type `v0.1.0` > *Create new tag on publish* > target `main` > title `v0.1.0` > paste the
   0.1.0 section of `CHANGELOG.md` as the description > *Publish release*.
   The *Release* workflow then builds, publishes to PyPI, installs `cfa-shoc` back from PyPI
   and runs the demo. Its result appears under *Actions*.
5. Reply "released" in the Claude Code session. Claude checks the run and
   https://pypi.org/project/cfa-shoc/ and records the evidence.
