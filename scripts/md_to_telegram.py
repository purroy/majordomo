#!/usr/bin/env python3
"""Convierte el Markdown de un briefing a texto plano legible en Telegram.

Reglas:
  # Título / ## Sección / ### Sub  → líneas en MAYÚSCULAS y separadas
  **bold** / __bold__              → quita marcadores
  *italic* / _italic_              → quita marcadores
  `code`                           → quita backticks
  - item / * item                  → "• item"
  > quote                          → "» quote"
  Tablas y enlaces se simplifican.

Lee de stdin, escribe a stdout. Sin parse_mode al enviar.
"""
import re
import sys


def convert(md: str) -> str:
    out_lines = []
    in_table = False
    for raw in md.splitlines():
        line = raw.rstrip()

        # Headers
        m = re.match(r'^(#{1,6})\s+(.*)$', line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            if level == 1:
                out_lines.append(title.upper())
                out_lines.append("=" * min(40, len(title)))
            else:
                out_lines.append(f"\n— {title} —")
            continue

        # Skip table separator lines like |---|---|
        if re.match(r'^\s*\|?\s*[:\-\s\|]+\s*$', line) and "|" in line:
            in_table = True
            continue
        # Table rows: convert | a | b | → "a · b"
        if "|" in line and line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out_lines.append("  " + " · ".join(cells))
            in_table = True
            continue
        if in_table and not line.strip():
            in_table = False

        # Bullets
        line = re.sub(r'^(\s*)[\-\*]\s+', r'\1• ', line)
        # Numbered lists keep "1. " etc.

        # Quotes
        line = re.sub(r'^\s*>\s?', '» ', line)

        # Inline formatting strip
        line = re.sub(r'\*\*([^*]+)\*\*', r'\1', line)
        line = re.sub(r'__([^_]+)__', r'\1', line)
        line = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'\1', line)
        line = re.sub(r'(?<!_)_([^_\n]+)_(?!_)', r'\1', line)
        line = re.sub(r'`([^`]+)`', r'\1', line)
        # Markdown links [text](url) → "text (url)"
        line = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', line)

        out_lines.append(line)

    text = "\n".join(out_lines)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


if __name__ == "__main__":
    sys.stdout.write(convert(sys.stdin.read()) + "\n")
