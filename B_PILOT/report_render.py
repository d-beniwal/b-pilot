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
import os
import re

from PyQt5 import QtCore
from PyQt5 import QtGui

from . import report_images as ri
from . import style as S
from .report_builder import CONTROL_SCHEME

#: Widest a figure is drawn in the panel. Qt's rich-text engine has no
#: ``max-width``, so a width *attribute* is the only way to bound an image --
#: which means the intrinsic size has to be read to avoid upscaling a small
#: one. ``QImageReader`` answers that from the file header alone, without
#: decoding the pixels.
IMAGE_DISPLAY_PX = 560

# --- inline spans, applied to already-escaped text ---------------------------
# Escaping only rewrites & < > ", none of which appear in these markers, so
# running them over escaped text is safe and avoids double-escaping.
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![\w*])_([^_]+)_(?![\w*])")
# `[label](url)`. Runs last so a link's label can carry the spans above.
# Deliberately does not match `![alt](path)` -- images are a block construct,
# handled in `to_html` before any inline pass sees the line.
_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)")

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
    out = _LINK.sub(_link_html, out)
    return out


def _link_html(match: re.Match) -> str:
    """One inline ``[label](url)``.

    **Only the report's own controls become clickable.** ``bpilot:hide/<id>``
    and friends (see ``report_builder.CONTROL_SCHEME``) are drawn small and
    muted, so a heading still reads as a heading rather than as a row of
    buttons; they are emitted for the live panel only, never into an export.

    Everything else renders as inert text showing both label and target. That
    is deliberate, and it is a security boundary rather than a limitation: the
    body of this document is arbitrary text from the kernel, from a pasted
    note, or drafted by AutoPILOT, and ``report_panel._on_anchor`` hands a
    local-file anchor to ``QDesktopServices.openUrl``. Turning bracket syntax
    in untrusted prose into a live ``file://`` link would make "click here" in
    a note enough to open something on the operator's machine. Prose links in
    a lab record were never a requirement; this surface is not worth having.
    """
    label, url = match.group(1), match.group(2)
    if url.startswith(f"{CONTROL_SCHEME}:"):
        return (
            f'<a href="{html.escape(url, quote=True)}" style="color:{S.MUTED}; '
            f'font-size:{S.px(10)}px; font-weight:normal; '
            f'text-decoration:none;">[{label}]</a>'
        )
    return f'{label} (<span style="color:{S.MUTED};">{url}</span>)'


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
    """A fenced code block, as a one-cell shaded table (see module docstring).

    Rendered as a wrapping ``<div>``, **not** a ``<pre>``. A real
    ``RE(mpe_step_grid_scan(...))`` call with a dozen arguments is far wider
    than a docked panel, and Qt's rich-text engine ignores
    ``white-space:pre-wrap`` inside a ``<pre>`` (verified by pixel grab: the
    line stayed clipped and the whole document grew a horizontal scrollbar).
    So newlines become ``<br>`` and leading indentation becomes ``&nbsp;``,
    which wraps like ordinary text while still preserving the shape of the
    code.
    """
    rendered = []
    for line in lines:
        escaped = html.escape(line)
        indent = len(line) - len(line.lstrip(" "))
        rendered.append("&nbsp;" * indent + escaped.lstrip(" ") if indent else escaped)
    return (
        f'<table width="100%" cellspacing="0" cellpadding="6" '
        f'style="margin:{S.px(4)}px 0;"><tr>'
        f'<td style="background-color:{S.INPUT_BG}; border:1px solid {S.BORDER};">'
        f'<div style="font-family:{S.MONO_CSS}; color:{S.CMD_RE};">'
        + "<br>".join(rendered)
        + "</div></td></tr></table>"
    )


def _quote_html(lines: list[str]) -> str:
    """A blockquote -- what run notes and error summaries render as."""
    body = "<br>".join(_inline(ln) for ln in lines)
    return (
        f'<table width="100%" cellspacing="0" cellpadding="6" '
        f'style="margin:{S.px(4)}px 0;"><tr>'
        f'<td style="background-color:{S.ALT_ROW_BG}; '
        f'border-left:{S.px(3)}px solid {S.ACCENT};">'
        f'<span style="color:{S.TEXT};">{body}</span></td></tr></table>'
    )


def _image_html(
    alt: str, rel: str, base_dir: str, embed: bool, cap_px: int | None = None
) -> str:
    """A figure, bounded to the panel width and captioned underneath.

    `base_dir` is the experiment folder the stored path is relative to. With
    `embed`, the file is inlined as a ``data:`` URI (HTML export, which
    promises a single self-contained file); otherwise it is referenced as a
    ``file:`` URL and wrapped in a link, so clicking opens it full size.

    A figure whose file has gone missing renders as a visible placeholder
    rather than a broken box -- a lab record should say what it lost.
    """
    path = os.path.join(base_dir, rel) if base_dir else rel
    caption = _inline(alt) if alt else ""

    if not os.path.isfile(path):
        return (
            f'<p style="color:{S.MUTED}; font-style:italic; margin:{S.px(4)}px 0;">'
            f"[missing figure: {html.escape(rel)}]</p>"
        )

    url = QtCore.QUrl.fromLocalFile(os.path.abspath(path)).toString()
    if embed:
        source = ri.data_uri(path)
        if not source:
            return (
                f'<p style="color:{S.MUTED}; font-style:italic; margin:{S.px(4)}px 0;">'
                f"[unreadable figure: {html.escape(rel)}]</p>"
            )
    else:
        source = url

    natural = QtGui.QImageReader(path).size()
    cap = S.px(cap_px or IMAGE_DISPLAY_PX)
    width = min(natural.width(), cap) if natural.isValid() and natural.width() > 0 else cap

    img = (
        f'<img src="{html.escape(source, quote=True)}" width="{width}" '
        f'alt="{html.escape(alt, quote=True)}">'
    )
    if not embed:
        img = f'<a href="{html.escape(url, quote=True)}">{img}</a>'

    body = img
    if caption:
        body += (
            f'<div style="color:{S.MUTED}; font-size:{S.px(11)}px; '
            f'margin-top:{S.px(3)}px;">{caption}</div>'
        )
    return (
        f'<table cellspacing="0" cellpadding="6" style="margin:{S.px(4)}px 0;"><tr>'
        f'<td style="background-color:{S.ALT_ROW_BG}; border:1px solid {S.BORDER};">'
        f"{body}</td></tr></table>"
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


def to_html(
    markdown: str,
    *,
    base_dir: str = "",
    embed_images: bool = False,
    image_px: int | None = None,
) -> str:
    """Render `markdown` to a themed HTML document for a ``QTextBrowser``.

    Handles exactly what :mod:`report_builder` emits: HTML comments (dropped),
    ATX headings, ``---`` rules, fenced code, pipe tables, ``>`` quotes,
    ``![](...)`` figures, and paragraphs with inline code/bold/italic. Anything
    else falls through as a paragraph rather than being lost -- a lab record
    should never silently drop a line it did not recognise.

    `base_dir` is the experiment folder that figure paths are relative to;
    without it images are looked up relative to the process's own directory,
    which is why every real caller passes it. `embed_images` inlines them as
    ``data:`` URIs for a self-contained HTML export. `image_px` overrides the
    figure width cap, which the PDF exporter uses to size figures for a page
    rather than for a docked panel.
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

        image = ri.IMAGE_MD.match(line.strip())
        if image:
            parts.append(
                _image_html(
                    image.group(1), image.group(2), base_dir, embed_images, image_px
                )
            )
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
        or ri.IMAGE_MD.match(stripped)
        or _COMMENT_OPEN.match(line)
    )


# ── PDF export ───────────────────────────────────────────────────────────────
# Qt's own print pipeline, not a new dependency: `QTextDocument.print_()` lays
# the same HTML this module already produces onto pages and `QPrinter` writes
# them out. QtPrintSupport ships with the conda `pyqt` build the beamline env
# pins, but the import is guarded anyway so a stripped install loses the PDF
# option rather than the whole Report panel (see report_panel's export menu).

try:
    from PyQt5 import QtPrintSupport
    PDF_AVAILABLE = True
except ImportError:  # pragma: no cover -- depends on how PyQt5 was installed
    QtPrintSupport = None
    PDF_AVAILABLE = False

#: Theme a printed report is rendered under, whatever the session is using.
#: A dark session's near-white body text on white paper is unreadable, and the
#: palette is read from `style`'s globals at call time, so the only way to fix
#: it is to rebind them for the duration of the render.
PRINT_THEME = "light"

#: Figure width on the page, in the same logical pixels the rest of the
#: document is laid out in. Sized against the ~660 px A4 text column below
#: rather than against the dock.
PDF_IMAGE_PX = 520

PDF_MARGINS_MM = (16, 16, 16, 16)  # left, top, right, bottom

#: Logical width the document is laid out at before Qt scales it onto the
#: page. Roughly a screen's worth of text column, which is what the font
#: sizes in this module were chosen against.
PDF_PAGE_PX = 610


def export_pdf(markdown: str, *, base_dir: str, path: str, title: str = "") -> bool:
    """Write `markdown` to a self-contained PDF at `path`; ``True`` on success.

    **Figures are referenced, not inlined.** Every other export path that has
    to stand alone either base64s the pixels (HTML) or copies them next to the
    file (Markdown), but a PDF needs neither: ``QTextDocument`` resolves the
    ``file:`` URLs this module already emits and Qt embeds the decoded pixels
    into the PDF itself. So the one-file promise holds without ever building a
    multi-megabyte ``data:`` string.

    The whole render runs under :func:`style.temporary_theme`, which also pins
    the UI-scale multiplier to 1.0 -- a document's type size must not depend on
    how large the operator likes their widgets.
    """
    if not PDF_AVAILABLE:
        return False

    with S.temporary_theme(PRINT_THEME, scale=1.0):
        body = to_html(markdown, base_dir=base_dir, image_px=PDF_IMAGE_PX)
        # An explicit white ground and dark default: a PDF viewer paints no
        # background of its own, and any text this module did not colour
        # explicitly would otherwise inherit the widget default.
        document = QtGui.QTextDocument()
        document.setHtml(
            f"<body style='background-color:#ffffff; color:{S.TEXT};'>{body}</body>"
        )

    printer = QtPrintSupport.QPrinter(QtPrintSupport.QPrinter.HighResolution)
    printer.setOutputFormat(QtPrintSupport.QPrinter.PdfFormat)
    printer.setOutputFileName(path)
    printer.setPageSize(QtPrintSupport.QPrinter.A4)
    printer.setPageMargins(*PDF_MARGINS_MM, QtPrintSupport.QPrinter.Millimeter)
    if title:
        printer.setDocName(title)

    # Lay the document out at a screen-like width and let `print_` scale that
    # to the page. Without this the document is laid out directly in the
    # printer's device units (thousands of dots across at HighResolution) and
    # the type comes out microscopic.
    document.setPageSize(QtCore.QSizeF(PDF_PAGE_PX, PDF_PAGE_PX * 1.414))
    document.print_(printer)
    return os.path.isfile(path)
