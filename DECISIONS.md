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
