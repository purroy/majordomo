---
description: List recent / unread mail with suggested triage
---

Args: `$ARGUMENTS` may contain:
- An account id (or `all` for every configured account — default `all`).
- A number (limit, default 15).
- `all` (include read messages too).
- `24h` / `7d` (time window; default is unread with no window).

## Steps

1. Resolve the accounts to query:
   - If the owner names one, use it.
   - If `all` or unspecified, list every configured account:
     ```
     python3 -c "import sys; sys.path.insert(0,'scripts'); from _mail import list_accounts; print(' '.join(list_accounts()))"
     ```

2. For each account, build the fetch command:
   - Default: `python3 scripts/mail_fetch.py --account <ID> --unread --limit 15 --with-body`
   - If `all`: drop `--unread`.
   - If `24h` / `7d` / `30d`: add `--since <value>`.
   - If a number: use as `--limit`.

3. Present grouped by account (when more than one):

```markdown
## Inbox (N total · window)

### <account-1> (K messages)
| UID | From | Subject | Snippet |
|-----|------|---------|---------|
| 1234 | Alice <alice@example.com> | Proposal April | "Hi, attaching the quote..." |

### <account-2> (M messages)
| ... |

### Suggested triage
- **Fire**: [account/UID] ...
- **Important**: [account/UID] ...
- **Review**: [account/UID] ...
- **Noise**: group

What next? (`/reply UID account`, `archive UID account`, etc.)
```

4. **Never mark anything as read or archive in this step.** List only.

5. If the owner replies with actions like "archive work/1230, side/4567" → run `python3 scripts/mail_flag.py UID --account ID --action archive` for each, then report.
