---
description: Query the calendar — today, tomorrow, this week, or a range
---

Args: `$ARGUMENTS` can be `today`, `tomorrow`, `week`, `next week`, a date `YYYY-MM-DD`, or a range `YYYY-MM-DD..YYYY-MM-DD`. Default: `today`.

## Steps

1. Compute the date range in the owner's local timezone.
2. Call the Google Calendar MCP to list events in that range.
3. Present grouped by day:

```markdown
## Mon 17 Apr
- 09:00–09:30 · Team standup · (Meet)
- 11:00–12:00 · Customer X · (Office)

## Tue 18 Apr
- No meetings
```

4. Flag overlaps or long free windows: `overlap with ...` or `open 14:00–17:00`.

5. Read-only: never create or modify anything here.

If Calendar MCP is not authenticated, say so and point to `/auth`.
