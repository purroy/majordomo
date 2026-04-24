---
description: EOS goals tracker — view rocks, monthly commitments, annuals; mark done; update progress; add notes
---

Args: `$ARGUMENTS` may contain:
- No args → show the full summary (`goals list`).
- `next` → 3 most urgent dated items.
- `check` → what the watcher would flag (overdue, due soon, stale rocks, revenue missing, quarter ending).
- `done <match>` → mark an open item as done.
- `update <match> --progress "<text>"` → set/replace the progress line on a rock.
- `note <match> "<text>"` → append a dated sub-bullet under that item's `notes:` block. Works on any item (commitment, rock, annual).
- `revenue YYYY-MM <amount>` → log monthly revenue (integer, or `null`).
- `rocks` / `annual` / `monthly` → show only that section.

## Source of truth

All data lives in `goals.local.md` at the repo root (gitignored). Editable by
hand or by `scripts/goals.py`. If the file is missing, tell the owner to copy
`goals.example.md` → `goals.local.md` and populate it.

## Steps

1. **Views**: run `goals.py` and return the output.
   - Default / unclear: `python3 scripts/goals.py list`
   - `next`: `python3 scripts/goals.py next`
   - `check`: `python3 scripts/goals.py check`
   - `rocks` / `annual` / `monthly`: run `list` and display only that section.

2. **Mutations — act directly when the match is unambiguous.** These mutate a
   private local file, not external systems, so no need to ask for approval on
   every write. The rules:

   - **Unambiguous match (exactly one item)** → run the command, report what
     was changed:
     ```
     python3 scripts/goals.py done "<match>"
     python3 scripts/goals.py update "<match>" --progress "<text>"
     python3 scripts/goals.py note "<match>" "<text>"
     python3 scripts/goals.py revenue <YYYY-MM> <amount>
     ```
   - **Ambiguous match (≥2 items)** → list the candidates and ask the owner
     to re-send with a more specific substring. Do NOT guess.
   - **No match** → say so; suggest running `/goals list` to see titles.
   - **Revenue overwrite** of a non-null value → confirm first (avoid
     stomping real data by accident).

3. **Free-text intent** is common over Telegram. Examples:
   - "hecho el 1:1 con el equipo" → interpret as `done "1:1 feedback"`.
   - "progreso rock Nexo: demo ready for ACME on Friday" → interpret as
     `update "Nexo" --progress "demo ready for ACME on Friday"`.
   - "anota en upsell: Carla acepta renovar en junio" → interpret as
     `note "upsell" "Carla acepta renovar en junio"`.
   - Extract `<match>` (shortest distinctive substring of the title) and
     `<text>`, then run directly if unambiguous.

4. **Context**: if the owner asks "what's due this week" or "my rocks", run
   `list` or `check`. Reply in the language of the request.

## Output formatting

- Short. Markdown bullets.
- Due dates: include days-until (`due 2026-05-24 (3d)` or `(overdue 2d)`).
- Never invent progress, KPIs, or dates. If a field is empty, say so.
- After a mutation, confirm the change in one sentence — no extra fluff.

## Hard rules

- Goals mutations do NOT fall under CLAUDE.md's "explicit confirmation"
  rule (that one is for mail / calendar / Slack, i.e. things that leave the
  local machine). For goals, unambiguous = act. Ambiguous = ask.
- Don't copy `goals.local.md` content into external tools (uploads, pastebins,
  diagram renderers, third-party APIs).
- The file is gitignored; don't suggest committing it.
