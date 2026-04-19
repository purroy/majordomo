---
description: Draft a reply to a mail (always confirm before sending)
---

Args: `$ARGUMENTS` should contain a UID and optionally the account id. Free form, e.g.:
- `12345`              -> default account
- `12345 work`         -> explicit account
- `work 12345`         -> same
If no UID is given, ask or point to `/inbox`.

## Steps

1. **Resolve the account** (default: the `DEFAULT_ACCOUNT` in `_mail.py`). Valid accounts:
   ```
   python3 -c "import sys; sys.path.insert(0,'scripts'); from _mail import list_accounts; print(' '.join(list_accounts()))"
   ```

2. **Read the original**:
   ```
   python3 scripts/mail_read.py <UID> --account <ID>
   ```
   Detect the body language (ignoring signature and quoted text).

3. **Draft IN THE LANGUAGE OF THE ORIGINAL**. Follow the owner's personal style file in the memory dir (e.g. `memory/writing_style.md`). Professional and direct, no empty filler, short.
   - **By default DO NOT add a signature** — most MUAs append an HTML signature on send.
   - Only if the owner says "with signature": append the block from `memory/signatures.md` for that account.

4. **Save the draft** to `/tmp/pa_reply_<UID>.txt` (overwrite).

5. **Show it to the owner**:
   - Account the reply will be sent from.
   - Subject (`Re: ...`).
   - To: (the original `From:`).
   - Full body.
   - Ask: "Send, edit, or cancel?"

6. **Wait for confirmation**:
   - "send" / "ok" / "yes" -> run:
     ```
     python3 scripts/mail_send.py \
       --account <ID> \
       --to "<original-from>" \
       --in-reply-to <UID> \
       --body-file /tmp/pa_reply_<UID>.txt \
       --yes
     ```
     **Do not mark the original as read.**
   - "edit ..." -> adjust and return to step 5.
   - "cancel" -> do not send; leave the draft in `/tmp` in case they pick it up later.

## Hard rules

- If the original has `Cc:`, ask whether to keep recipients.
- If an attachment is mentioned, ask for the path and add `--attach`.
- NEVER call `mail_send.py --yes` without a clear "yes" in the current turn.
- NEVER call `mail_flag.py --action seen` after sending.
- Internal threads can be more direct; external, slightly more formal but still plain.
