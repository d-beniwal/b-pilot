"""Render the report's Markdown as themed HTML for a ``QTextBrowser``.

**Why a hand-rolled renderer rather than a Markdown library.** The beamline
deployment environment (``environments/bpilot_mpe_dev.yml``) carries no
Markdown dependency, and this project has already paid once for adding a
package casually -- the 2026-08-10 pip-Qt incident, where pip's PyQt5 wheels
broke GUI launch on redwood outright. A renderer that only has to handle the
constructs :mod:`report_builder` actually emits is a couple of hundred lines,
has no install story, and cannot drift from what we generate.

**Why the output looks the way it does.** Qt's rich-text engine is not a
browser. It has no flexbox, no ``border-radius``, and -- the one that bites --
no background painting on a full-width ``<div>``. Both of the existing HTML
surfaces in this codebase hit the same wall and solved it the same way, with a
one-cell ``<table>``: see ``session_log._entry_html`` and
``chat_panel._bubble_html``. This module follows that precedent.

Colors are read from :mod:`style` *inside* the functions, never captured at
import time -- ``style.apply_theme`` rebinds those module globals at startup,
so a module-level capture would freeze the light-theme palette into a dark
session.
"""
from __future__ import annotations

import html
import re

from . import style as S

# --- inline spans, applied to already-escaped text ---------------------------
# Escaping only rewrites & < > ", none of which appear in these markers, so
# running them over escaped text is safe and avoids double-escaping.
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![\w*])_([^_]+)_(?![\w*])")

_FENCE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|$")
_COMMENT_OPEN = re.compile(r"^\s*<!--")
_COMMENT_CLOSE = re.compile(r"-->")


def _inline(text: str) -> str:
    """Escape `text`, then apply the inline Markdown spans we emit."""
    out = html.escape(text)
    out = _CODE.sub(
        lambda m: f'<code style="font-family:{S.MONO_CSS}; '
        f'background-color:{S.ALT_ROW_BG};">{m.group(1)}</code>',
        out,
    )
    out = _BOLD.sub(r"<b>\1</b>", out)
    out = _ITALIC.sub(r"<i>\1</i>", out)
    return out


def _heading_html(level: int, text: str) -> str:
    """A heading, sized and coloured by level rather than by Qt's defaults."""
    sizes = {1: 20, 2: 16, 3: 14, 4: 12}
    size = S.px(sizes.get(level, 12))
    color = S.TEXT if level <= 2 else S.ACCENT
    rule = (
        f" border-bottom:1px solid {S.BORDER}; padding-bottom:{S.px(3)}px;"
        if level <= 2
        else ""
    )
    return (
        f'<div style="color:{color}; font-size:{size}px; font-weight:bold; '
        f'margin-top:{S.px(14)}px; margin-bottom:{S.px(4)}px;{rule}">{_inline(text)}</div>'
    )


def _code_html(lines: list[str]) -> str:
    """A fenced code block, as a one-cell shaded table (see module docstring)."""
    body = html.escape("\n".join(lines))
    return (
        f'<table width="100%" cellspacing="0" cellpadding="6" '
        f'style="background-color:{S.INPUT_BG}; border:1px solid {S.BORDER}; '
        f'margin:{S.px(4)}px 0;"><tr><td>'
        f'<pre style="font-family:{S.MONO_CSS}; color:{S.CMD_RE}; margin:0;">{body}</pre>'
        f"</td></tr></table>"
    )


def _quote_html(lines: list[str]) -> str:
    """A blockquote -- what run notes and error summaries render as."""
    body = "<br>".join(_inline(ln) for ln in lines)
    return (
        f'<table width="100%" cellspacing="0" cellpadding="6" '
        f'style="background-color:{S.ALT_ROW_BG}; border-left:3px solid {S.ACCENT}; '
        f'margin:{S.px(4)}px 0;"><tr><td>'
        f'<span style="color:{S.TEXT};">{body}</span></td></tr></table>'
    )


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table_html(rows: list[list[str]]) -> str:
    """A pipe table. First row is the header."""
    if not rows:
        return ""
    head, body = rows[0], rows[1:]
    out = [
        f'<table cellspacing="0" cellpadding="5" '
        f'style="border:1px solid {S.BORDER}; margin:{S.px(6)}px 0;">'
    ]
    out.append(f'<tr style="background-color:{S.ALT_ROW_BG};">')
    for cell in head:
        out.append(
            f'<td style="color:{S.MUTED}; font-weight:bold; '
            f'border-bottom:1px solid {S.BORDER};">{_inline(cell)}</td>'
        )
    out.append("</tr>")
    for row in body:
        out.append("<tr>")
        for cell in row:
            out.append(f'<td style="color:{S.TEXT};">{_inline(cell)}</td>')
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def to_html(markdown: str) -> str:
    """Render `markdown` to a themed HTML document for a ``QTextBrowser``.

    Handles exactly what :mod:`report_builder` emits: HTML comments (dropped),
    ATX headings, ``---`` rules, fenced code, pipe tables, ``>`` quotes, and
    paragraphs with inline code/bold/italic. Anything else falls through as a
    paragraph rather than being lost -- a lab record should never silently
    drop a line it did not recognise.
    """
    parts: list[str] = []
    lines = (markdown or "").splitlines()
    i = 0
    in_comment = False

    while i < len(lines):
        line = lines[i]

        if in_comment:
            in_comment = not _COMMENT_CLOSE.search(line)
            i += 1
            continue
        if _COMMENT_OPEN.match(line):
            in_comment = not _COMMENT_CLOSE.search(line)
            i += 1
            continue

        if not line.strip():
            i += 1
            continue

        fence = _FENCE.match(line.strip())
        if fence:
            i += 1
            block: list[str] = []
            while i < len(lines) and not _FENCE.match(lines[i].strip()):
                block.append(lines[i])
                i += 1
            i += 1  # closing fence (or EOF, which is fine)
            parts.append(_code_html(block))
            continue

        if line.strip() in ("---", "***", "___"):
            parts.append(
                f'<hr style="border:none; border-top:1px solid {S.BORDER}; '
                f'margin:{S.px(12)}px 0;">'
            )
            i += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            parts.append(_heading_html(len(heading.group(1)), heading.group(2)))
            i += 1
            continue

        if line.lstrip().startswith("|"):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                if not _TABLE_SEP.match(lines[i].strip()):
                    rows.append(_split_row(lines[i]))
                i += 1
            parts.append(_table_html(rows))
            continue

        if line.lstrip().startswith(">"):
            quote: list[str] = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(lines[i].lstrip()[1:].strip())
                i += 1
            parts.append(_quote_html(quote))
            continue

        para: list[str] = []
        while i < len(lines) and lines[i].strip() and not _is_block_start(lines[i]):
            para.append(lines[i].strip())
            i += 1
        parts.append(
            f'<p style="color:{S.TEXT}; margin:{S.px(4)}px 0;">'
            f'{"<br>".join(_inline(p) for p in para)}</p>'
        )

    return (
        f'<div style="font-size:{S.px(12)}px; line-height:140%;">'
        + "".join(parts)
        + "</div>"
    )


def _is_block_start(line: str) -> bool:
    """True if `line` opens a construct a paragraph must not swallow."""
    stripped = line.strip()
    return bool(
        _FENCE.match(stripped)
        or _HEADING.match(line)
        or stripped in ("---", "***", "___")
        or stripped.startswith("|")
        or stripped.startswith(">")
        or _COMMENT_OPEN.match(line)
    )
