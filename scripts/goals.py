#!/usr/bin/env python3
"""Goals tracker for PA — read and mutate an EOS-style `goals.local.md`.

Sections (level-2 Markdown headers):
  - Period              key/value: quarter, month
  - Monthly Commitments `- [ ] <Area> | <YYYY-MM-DD> | <title>` + indented attrs
  - Quarterly Rocks     `- [ ] <Area> | <title>` + indented attrs
  - Annual Goals        `- [ ] <Area> | <YYYY-MM-DD> | <title>` + indented attrs
  - Monthly Revenue     `- <YYYY-MM>: <int|null>`
  - Done log            `- <YYYY-MM-DD> | <Area> | <description>`

Indented attrs on continuation lines (>=2 spaces): `KPI: ...`, `progress: ...`,
`done: ...`, `notes: ...`. Unknown sections and comment lines are preserved
verbatim on write — the script does surgical edits, never a full rewrite.

Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PATH = REPO_DIR / "goals.local.md"

SECTIONS = {
    "period": "Period",
    "monthly": "Monthly Commitments",
    "rocks": "Quarterly Rocks",
    "annual": "Annual Goals",
    "revenue": "Monthly Revenue",
    "done": "Done log",
}

AREAS = {"Cash", "People", "Execution", "Strategy"}

ITEM_RE_DATED = re.compile(
    r"^- \[(?P<check>[ xX])\]\s+(?P<area>\S+)\s*\|\s*(?P<due>\d{4}-\d{2}-\d{2})\s*\|\s*(?P<title>.+?)\s*$"
)
ITEM_RE_UNDATED = re.compile(
    r"^- \[(?P<check>[ xX])\]\s+(?P<area>\S+)\s*\|\s*(?P<title>.+?)\s*$"
)
ATTR_RE = re.compile(r"^\s{2,}(?P<key>[A-Za-z][A-Za-z0-9_ -]*?):\s*(?P<val>.*?)\s*$")
REVENUE_RE = re.compile(r"^-\s*(?P<month>\d{4}-\d{2})\s*:\s*(?P<val>.+?)\s*$")
DONE_RE = re.compile(
    r"^-\s*(?P<date>\d{4}-\d{2}-\d{2})\s*\|\s*(?P<area>\S+)\s*\|\s*(?P<desc>.+?)\s*$"
)
PERIOD_RE = re.compile(r"^-\s*(?P<key>quarter|month)\s*:\s*(?P<val>.+?)\s*$")


@dataclass
class Item:
    kind: str  # "monthly" | "rocks" | "annual"
    area: str
    title: str
    checked: bool
    due: Optional[dt.date]
    attrs: dict = field(default_factory=dict)
    line_start: int = -1  # index of the `- [ ]` line
    line_end: int = -1    # exclusive; first line NOT part of this item


@dataclass
class Document:
    path: Path
    lines: list[str]
    sections: dict[str, tuple[int, int]]  # name -> (start, end) inclusive-exclusive
    items: dict[str, list[Item]]          # "monthly"/"rocks"/"annual" -> [Item]
    revenue: list[tuple[str, Optional[int], int]]  # (YYYY-MM, amount|None, line_idx)
    done_log: list[tuple[str, str, str, int]]      # (date, area, desc, line_idx)
    period: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> "Document":
        if not path.exists():
            raise SystemExit(f"goals file not found: {path} (copy goals.example.md to {path.name})")
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        doc = cls(
            path=path, lines=lines, sections={}, items={"monthly": [], "rocks": [], "annual": []},
            revenue=[], done_log=[], period={},
        )
        doc._index()
        return doc

    def _index(self) -> None:
        # 1. Identify section ranges by `## Name` headers.
        header_idx: list[tuple[str, int]] = []
        for i, line in enumerate(self.lines):
            if line.startswith("## "):
                header_idx.append((line[3:].strip(), i))
        for pos, (name, start) in enumerate(header_idx):
            end = header_idx[pos + 1][1] if pos + 1 < len(header_idx) else len(self.lines)
            self.sections[name] = (start + 1, end)

        # 2. Parse each known section.
        for kind, header in (("monthly", SECTIONS["monthly"]),
                             ("rocks", SECTIONS["rocks"]),
                             ("annual", SECTIONS["annual"])):
            if header not in self.sections:
                continue
            self.items[kind] = self._parse_items(kind, *self.sections[header])

        if SECTIONS["revenue"] in self.sections:
            s, e = self.sections[SECTIONS["revenue"]]
            for i in range(s, e):
                m = REVENUE_RE.match(self.lines[i])
                if m:
                    val = m.group("val").strip()
                    amt = None if val.lower() in {"null", "none", "-", ""} else _parse_int(val)
                    self.revenue.append((m.group("month"), amt, i))

        if SECTIONS["done"] in self.sections:
            s, e = self.sections[SECTIONS["done"]]
            for i in range(s, e):
                m = DONE_RE.match(self.lines[i])
                if m:
                    self.done_log.append((m.group("date"), m.group("area"), m.group("desc"), i))

        if SECTIONS["period"] in self.sections:
            s, e = self.sections[SECTIONS["period"]]
            for i in range(s, e):
                m = PERIOD_RE.match(self.lines[i])
                if m:
                    self.period[m.group("key")] = m.group("val").strip()

    def _parse_items(self, kind: str, start: int, end: int) -> list[Item]:
        items: list[Item] = []
        i = start
        while i < end:
            line = self.lines[i]
            m_dated = ITEM_RE_DATED.match(line)
            m_undated = None if m_dated else ITEM_RE_UNDATED.match(line)
            if not (m_dated or m_undated):
                i += 1
                continue
            m = m_dated or m_undated
            item = Item(
                kind=kind,
                area=m.group("area"),
                title=m.group("title").strip(),
                checked=m.group("check").lower() == "x",
                due=_parse_date(m.group("due")) if m_dated else None,
                line_start=i,
            )
            j = i + 1
            while j < end:
                next_line = self.lines[j]
                if ITEM_RE_DATED.match(next_line) or ITEM_RE_UNDATED.match(next_line):
                    break
                attr_m = ATTR_RE.match(next_line)
                if attr_m:
                    item.attrs[attr_m.group("key").strip()] = attr_m.group("val")
                j += 1
            item.line_end = j
            items.append(item)
            i = j
        return items

    def save(self) -> None:
        text = "\n".join(self.lines)
        if not text.endswith("\n"):
            text += "\n"
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)

    # --- queries ----------------------------------------------------------

    def all_items(self) -> list[Item]:
        return self.items["monthly"] + self.items["rocks"] + self.items["annual"]

    def find(self, match: str, *, kind: Optional[str] = None, open_only: bool = True) -> list[Item]:
        needle = match.lower().strip()
        pool = self.items[kind] if kind else self.all_items()
        return [it for it in pool
                if (not open_only or not it.checked)
                and (needle in it.title.lower() or needle == str(it.due))]

    # --- mutations (line-level) -------------------------------------------

    def mark_done(self, item: Item, when: dt.date) -> None:
        # 1. Flip checkbox on the item line.
        line = self.lines[item.line_start]
        self.lines[item.line_start] = line.replace("- [ ]", "- [x]", 1)
        # 2. Add / update `done:` attr inside the item's block.
        done_str = when.isoformat()
        done_line_idx = None
        for i in range(item.line_start + 1, item.line_end):
            if ATTR_RE.match(self.lines[i]) and re.match(r"^\s+done\s*:", self.lines[i]):
                done_line_idx = i
                break
        indent = _infer_indent(self.lines, item.line_start + 1, item.line_end)
        new_done_line = f"{indent}done: {done_str}"
        if done_line_idx is not None:
            self.lines[done_line_idx] = new_done_line
        else:
            self._insert(item.line_end, new_done_line)
        item.attrs["done"] = done_str
        item.checked = True
        # 3. Append to Done log (if section exists).
        log_entry = f"- {done_str} | {item.area} | {item.title}"
        self._append_to_section(SECTIONS["done"], log_entry)

    def set_progress(self, item: Item, progress: str) -> None:
        if item.kind != "rocks":
            raise SystemExit("progress: only supported on Quarterly Rocks items")
        indent = _infer_indent(self.lines, item.line_start + 1, item.line_end)
        new_line = f'{indent}progress: "{progress}"'
        for i in range(item.line_start + 1, item.line_end):
            if re.match(r"^\s+progress\s*:", self.lines[i]):
                self.lines[i] = new_line
                item.attrs["progress"] = progress
                return
        self._insert(item.line_end, new_line)
        item.attrs["progress"] = progress

    def append_note(self, item: Item, text: str, when: dt.date) -> None:
        """Append a dated sub-bullet under the item's `notes:` block.

        - If `notes:` line is missing, create it at the end of the item's
          attribute block, then append the sub-bullet below it.
        - Sub-bullets are indented one more level than the attributes.
        """
        indent = _infer_indent(self.lines, item.line_start + 1, item.line_end)
        bullet_indent = indent + "  "
        bullet = f"{bullet_indent}- {when.isoformat()}: {text}"
        notes_idx = None
        for i in range(item.line_start + 1, item.line_end):
            if re.match(r"^\s+notes\s*:\s*$", self.lines[i]) or \
               re.match(r"^\s+notes\s*:\s*\".*\"\s*$", self.lines[i]):
                notes_idx = i
                break
        if notes_idx is None:
            self._insert(item.line_end, f"{indent}notes:")
            notes_idx = item.line_end - 1
        # Find last sub-bullet belonging to notes (lines starting with bullet_indent + "- ").
        insert_at = notes_idx + 1
        while insert_at < item.line_end and self.lines[insert_at].startswith(bullet_indent + "- "):
            insert_at += 1
        self._insert(insert_at, bullet)
        item.attrs["notes"] = (item.attrs.get("notes", "") + f" | {when.isoformat()}: {text}").strip(" |")

    def set_revenue(self, month: str, amount: Optional[int]) -> None:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise SystemExit(f"revenue month must be YYYY-MM, got {month!r}")
        val_str = "null" if amount is None else str(amount)
        new_line = f"- {month}: {val_str}"
        for m, _amt, idx in self.revenue:
            if m == month:
                self.lines[idx] = new_line
                return
        # Append to revenue section (keep calendar order when possible).
        self._append_to_section(SECTIONS["revenue"], new_line)

    def _insert(self, index: int, line: str) -> None:
        self.lines.insert(index, line)
        # Shift all downstream section/item indices by +1.
        for name, (s, e) in list(self.sections.items()):
            new_s = s + 1 if s >= index else s
            new_e = e + 1 if e >= index else e
            self.sections[name] = (new_s, new_e)
        for items in self.items.values():
            for it in items:
                if it.line_start >= index:
                    it.line_start += 1
                if it.line_end >= index:
                    it.line_end += 1
        self.revenue = [(m, a, i + 1 if i >= index else i) for m, a, i in self.revenue]
        self.done_log = [(d, ar, de, i + 1 if i >= index else i) for d, ar, de, i in self.done_log]

    def _append_to_section(self, header: str, line: str) -> None:
        if header not in self.sections:
            # Create section at EOF.
            if self.lines and self.lines[-1].strip():
                self.lines.append("")
            self.lines.append(f"## {header}")
            start = len(self.lines)
            self.lines.append(line)
            self.sections[header] = (start, len(self.lines))
            if header == SECTIONS["done"]:
                self.done_log.append((_today_iso(), "", line[2:].strip(), len(self.lines) - 1))
            return
        start, end = self.sections[header]
        # Find last non-blank line inside the section.
        insert_at = end
        while insert_at > start and not self.lines[insert_at - 1].strip():
            insert_at -= 1
        self._insert(insert_at, line)


# --- helpers -----------------------------------------------------------------

def _parse_date(s: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


def _parse_int(s: str) -> Optional[int]:
    s = s.replace(",", "").replace("€", "").replace(" ", "").strip()
    try:
        return int(float(s))
    except ValueError:
        return None


def _today_iso() -> str:
    return dt.date.today().isoformat()


def _infer_indent(lines: list[str], start: int, end: int) -> str:
    for i in range(start, end):
        m = re.match(r"^(\s+)\S", lines[i])
        if m:
            return m.group(1)
    return "      "  # 6 spaces — default for EOS template


def _days_until(d: Optional[dt.date]) -> Optional[int]:
    if d is None:
        return None
    return (d - dt.date.today()).days


def _quarter_end(quarter_label: str) -> Optional[dt.date]:
    """Parse 'Q2 2026' or 'Q4 2025' -> last day of that quarter."""
    m = re.match(r"Q([1-4])\s+(\d{4})", quarter_label.strip())
    if not m:
        return None
    q, year = int(m.group(1)), int(m.group(2))
    end_month = q * 3
    # Last day of end_month.
    if end_month == 12:
        return dt.date(year, 12, 31)
    return dt.date(year, end_month + 1, 1) - dt.timedelta(days=1)


# --- commands ----------------------------------------------------------------

def cmd_list(doc: Document, *, as_json: bool) -> int:
    data = _snapshot(doc)
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        return 0
    # Human-readable.
    print(f"# Goals — {doc.period.get('quarter', '?')} / {doc.period.get('month', '?')}")
    print()
    _print_items(doc, "Monthly Commitments", doc.items["monthly"])
    _print_items(doc, "Quarterly Rocks", doc.items["rocks"])
    _print_items(doc, "Annual Goals", doc.items["annual"])
    if doc.revenue:
        print("## Monthly Revenue")
        filled = [(m, a) for m, a, _ in doc.revenue if a is not None]
        total = sum(a for _, a in filled)
        print(f"YTD filled ({len(filled)}/{len(doc.revenue)} months): €{total:,}")
        for m, a, _ in doc.revenue[-6:]:
            shown = f"€{a:,}" if a is not None else "—"
            print(f"- {m}: {shown}")
        print()
    if doc.done_log:
        print("## Recently done")
        for d, ar, de, _ in doc.done_log[-5:]:
            print(f"- {d} | {ar} | {de}")
    return 0


def _print_items(doc: Document, title: str, items: list[Item]) -> None:
    if not items:
        return
    print(f"## {title}")
    for it in items:
        check = "[x]" if it.checked else "[ ]"
        due = ""
        if it.due is not None:
            days = _days_until(it.due)
            suffix = "overdue" if days < 0 else ("today" if days == 0 else f"{days}d")
            due = f" · due {it.due.isoformat()} ({suffix})"
        kpi = f" — KPI: {it.attrs['KPI']}" if "KPI" in it.attrs else ""
        prog = f" — progress: {it.attrs['progress']}" if "progress" in it.attrs else ""
        print(f"- {check} {it.area}{due} — {it.title}{kpi}{prog}")
    print()


def _snapshot(doc: Document) -> dict:
    def item_dict(it: Item) -> dict:
        return {
            "area": it.area, "title": it.title, "checked": it.checked,
            "due": it.due.isoformat() if it.due else None,
            "attrs": it.attrs, "days_until": _days_until(it.due),
        }
    return {
        "period": doc.period,
        "monthly": [item_dict(i) for i in doc.items["monthly"]],
        "rocks": [item_dict(i) for i in doc.items["rocks"]],
        "annual": [item_dict(i) for i in doc.items["annual"]],
        "revenue": [{"month": m, "amount": a} for m, a, _ in doc.revenue],
        "done_log": [{"date": d, "area": ar, "desc": de} for d, ar, de, _ in doc.done_log],
    }


def cmd_done(doc: Document, match: str) -> int:
    matches = doc.find(match, open_only=True)
    if not matches:
        print(f"no open item matches {match!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"ambiguous: {len(matches)} open items match {match!r}:", file=sys.stderr)
        for it in matches:
            print(f"  - ({it.kind}) {it.area} | {it.title}", file=sys.stderr)
        return 2
    item = matches[0]
    doc.mark_done(item, dt.date.today())
    doc.save()
    print(f"done: ({item.kind}) {item.area} | {item.title}")
    return 0


def cmd_update(doc: Document, match: str, *, progress: Optional[str]) -> int:
    if progress is None:
        print("nothing to update (pass --progress)", file=sys.stderr)
        return 2
    matches = doc.find(match, kind="rocks", open_only=True)
    if not matches:
        print(f"no open rock matches {match!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"ambiguous: {len(matches)} rocks match {match!r}:", file=sys.stderr)
        for it in matches:
            print(f"  - {it.area} | {it.title}", file=sys.stderr)
        return 2
    item = matches[0]
    doc.set_progress(item, progress)
    doc.save()
    print(f"progress: {item.area} | {item.title} → {progress}")
    return 0


def cmd_note(doc: Document, match: str, text: str) -> int:
    matches = doc.find(match, open_only=False)
    if not matches:
        print(f"no item matches {match!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"ambiguous: {len(matches)} items match {match!r}:", file=sys.stderr)
        for it in matches:
            print(f"  - ({it.kind}) {it.area} | {it.title}", file=sys.stderr)
        return 2
    item = matches[0]
    doc.append_note(item, text, dt.date.today())
    doc.save()
    print(f"note: ({item.kind}) {item.area} | {item.title} ← {text}")
    return 0


def cmd_revenue(doc: Document, month: str, amount_s: str) -> int:
    amount = None if amount_s.lower() in {"null", "none", "-"} else _parse_int(amount_s)
    if amount_s and amount_s.lower() not in {"null", "none", "-"} and amount is None:
        print(f"could not parse amount {amount_s!r}", file=sys.stderr)
        return 2
    doc.set_revenue(month, amount)
    doc.save()
    shown = "null" if amount is None else f"€{amount:,}"
    print(f"revenue: {month} → {shown}")
    return 0


def cmd_next(doc: Document, *, as_json: bool) -> int:
    items = [it for it in doc.all_items() if not it.checked and it.due is not None]
    items.sort(key=lambda it: it.due)
    top = items[:3]
    if as_json:
        print(json.dumps([{
            "kind": it.kind, "area": it.area, "title": it.title,
            "due": it.due.isoformat(), "days_until": _days_until(it.due),
        } for it in top], indent=2, ensure_ascii=False))
        return 0
    if not top:
        print("nothing pending with a due date")
        return 0
    for it in top:
        d = _days_until(it.due)
        label = "overdue" if d < 0 else ("today" if d == 0 else f"{d}d")
        print(f"- ({it.kind}) {it.area} | {it.title} · due {it.due} ({label})")
    return 0


def cmd_check(doc: Document, *, as_json: bool) -> int:
    """What the watcher should remind about."""
    today = dt.date.today()
    report = {
        "today": today.isoformat(),
        "overdue": [],
        "due_soon": [],         # due in 0..2 days
        "stale_rocks": [],      # no `progress:` updated in >7 days (proxy: no progress attr or progress looks stale)
        "revenue_missing": [],  # months before current with null amount
        "quarter_ending": None, # set if quarter_end - today <= 14 days
    }
    for it in doc.items["monthly"] + doc.items["annual"]:
        if it.checked or it.due is None:
            continue
        d = _days_until(it.due)
        entry = {
            "kind": it.kind, "area": it.area, "title": it.title,
            "due": it.due.isoformat(), "days_until": d,
        }
        if d < 0:
            report["overdue"].append(entry)
        elif d <= 2:
            report["due_soon"].append(entry)
    for it in doc.items["rocks"]:
        if it.checked:
            continue
        if "progress" not in it.attrs or not it.attrs["progress"].strip(' "\''):
            report["stale_rocks"].append({
                "area": it.area, "title": it.title,
                "progress": it.attrs.get("progress", ""),
            })
    # Revenue: any month strictly before current one that is None.
    current_month = today.strftime("%Y-%m")
    for month, amount, _ in doc.revenue:
        if amount is None and month < current_month:
            report["revenue_missing"].append(month)
    # Quarter end.
    q_end = _quarter_end(doc.period.get("quarter", ""))
    if q_end is not None:
        delta = (q_end - today).days
        if 0 <= delta <= 14:
            report["quarter_ending"] = {"quarter": doc.period.get("quarter"),
                                        "ends": q_end.isoformat(), "days_until": delta}
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    print(f"today: {report['today']}")
    if report["overdue"]:
        print("OVERDUE:")
        for e in report["overdue"]:
            print(f"  - {e['area']} | {e['title']} · {e['due']} ({e['days_until']}d)")
    if report["due_soon"]:
        print("DUE SOON:")
        for e in report["due_soon"]:
            print(f"  - {e['area']} | {e['title']} · {e['due']} ({e['days_until']}d)")
    if report["stale_rocks"]:
        print("STALE ROCKS:")
        for e in report["stale_rocks"]:
            print(f"  - {e['area']} | {e['title']}")
    if report["revenue_missing"]:
        print(f"REVENUE MISSING: {', '.join(report['revenue_missing'])}")
    if report["quarter_ending"]:
        qe = report["quarter_ending"]
        print(f"QUARTER ENDING: {qe['quarter']} in {qe['days_until']}d ({qe['ends']})")
    if not any([report["overdue"], report["due_soon"], report["stale_rocks"],
                report["revenue_missing"], report["quarter_ending"]]):
        print("nothing to flag")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=DEFAULT_PATH,
                    help=f"goals file (default: {DEFAULT_PATH})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="Show commitments, rocks, annuals, revenue.")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("done", help="Mark an open item as done.")
    sp.add_argument("match", help="Substring of the title (case-insensitive).")

    sp = sub.add_parser("update", help="Update a rock's progress line.")
    sp.add_argument("match")
    sp.add_argument("--progress", help="New progress string.")

    sp = sub.add_parser("note", help="Append a dated note to any item's notes block.")
    sp.add_argument("match")
    sp.add_argument("text")

    sp = sub.add_parser("revenue", help="Set revenue for a month.")
    sp.add_argument("month", help="YYYY-MM.")
    sp.add_argument("amount", help="Integer (no commas) or 'null'.")

    sp = sub.add_parser("next", help="Top 3 most urgent items with a due date.")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("check", help="What the watcher should remind about.")
    sp.add_argument("--json", action="store_true")

    args = ap.parse_args()
    doc = Document.load(args.file)

    if args.cmd == "list":
        return cmd_list(doc, as_json=args.json)
    if args.cmd == "done":
        return cmd_done(doc, args.match)
    if args.cmd == "update":
        return cmd_update(doc, args.match, progress=args.progress)
    if args.cmd == "note":
        return cmd_note(doc, args.match, args.text)
    if args.cmd == "revenue":
        return cmd_revenue(doc, args.month, args.amount)
    if args.cmd == "next":
        return cmd_next(doc, as_json=args.json)
    if args.cmd == "check":
        return cmd_check(doc, as_json=args.json)
    return 2


if __name__ == "__main__":
    sys.exit(main())
