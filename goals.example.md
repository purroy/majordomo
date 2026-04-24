# EOS — <Your Name> / <Business>

Template for the PA's EOS-style goals tracker. Copy this file to
`goals.local.md` (gitignored) and fill it in. The PA reads and updates it via
`scripts/goals.py` and the `/goals` slash command.

Format rules:

- Section headers are level-2 (`## Name`). Don't rename them.
- Items: `- [ ] <Area> | <YYYY-MM-DD> | <title>` (monthly / annual) or
  `- [ ] <Area> | <title>` (rocks, no date).
- Area ∈ Cash / People / Execution / Strategy (EOS four corners).
- Continuation lines use ≥2-space indent: `KPI:`, `progress:`, `done:`, `notes:`.
- `notes:` can hold dated sub-bullets appended by `goals.py note <match> "..."`.
- The parser preserves unknown text, so you can keep notes, comments, or extra
  sections and they'll survive `goals.py` edits.

## Period
- quarter: Q1 2026
- month: 2026-01

## Monthly Commitments
- [ ] Execution | 2026-01-31 | <priority for this month>
      KPI: <measurable outcome>
- [ ] People | 2026-01-31 | <priority>
      KPI: <measurable outcome>

## Quarterly Rocks
- [ ] Cash | <big goal for the quarter — 1 sentence>
      KPI: <what "done" looks like>
      progress: ""
- [ ] Execution | <another rock>
      KPI: ...
      progress: ""

## Annual Goals
- [ ] Cash | 2026-12-31 | <annual target>
      KPI: <measurable>
- [ ] Strategy | 2026-12-31 | <annual goal>
      KPI: <measurable>

## Monthly Revenue
- 2025-07: null
- 2025-08: null
- 2025-09: null
- 2025-10: null
- 2025-11: null
- 2025-12: null
- 2026-01: null
- 2026-02: null
- 2026-03: null
- 2026-04: null
- 2026-05: null
- 2026-06: null

## Done log
<!-- Items auto-moved here when marked done via `goals.py done <match>`. -->
