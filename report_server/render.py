"""Render a pushed report's Markdown into HTML for the browser.

This is deliberately a *second* renderer, not a port of B-PILOT's
``B_PILOT/report_render.py``, and the reason is worth stating plainly because
"two renderers" normally means someone got lazy.

``report_render`` targets Qt's rich-text engine, which has no flexbox, no
``border-radius`` and no background on a full-width ``<div>``. Everything it
emits is bent around those gaps: code blocks and quotes are one-cell
``<table>``s, code is a wrapping ``<div>`` of ``<br>`` and ``&nbsp;`` rather
than a ``<pre>``, and every element carries an inline ``style="..."`` because
there is no stylesheet. A browser has none of those limitations, so this
renderer gets real ``<pre>``, real ``<figure>``, real CSS classes -- and
crucially, because it emits no inline styles, the page needs no
``style-src 'unsafe-inline'`` in its Content-Security-Policy. Porting the Qt
renderer would have forced exactly the CSP directive most worth keeping out.

**The security model is stricter here than in the desktop app, and it has to
be.** Qt's rich-text engine ignores ``<script>``; a browser does not. The body
of a report is genuinely untrusted text -- kernel output, notes pasted from
anywhere, prose drafted by an LLM -- and it is now being served to people over
the network. Three invariants, all tested in ``tests/test_security.py``:

1. **No ``<a href>`` is ever emitted from report content.** B-PILOT's
   ``_link_html`` makes an exception for its own ``bpilot:`` control links;
   here there is no exception at all, because those controls are meaningless
   remotely and a live scheme handler is a liability. ``[label](url)`` renders
   as inert text showing both, exactly as it does in the desktop app.
2. **An image ``src`` is only ever ``figures/<validated basename>``.** A note
   reading ``![x](figures/../../../../etc/passwd)`` is untrusted Markdown
   reaching a file path, and ``![x](https://evil.example/p.png)`` would leak
   every reader's IP and confirm the secret link is being read. Neither is
   rendered.
3. **Every text node and every attribute goes through ``html.escape(quote=True)``.**
   Written explicitly rather than relying on the default, so that nobody
   "simplifies" it later.

The construct list mirrors what ``report_builder.render_markdown`` actually
emits -- HTML comments (dropped), ATX headings, ``---`` rules, fenced code,
pipe tables, ``>`` quotes, ``![](...)`` images, and inline code/bold/italic.
Anything unrecognised falls through as a paragraph rather than being dropped:
a lab record must never silently lose a line.
"""
from __future__ import annotations

import html
import re

# Figure names as report_images.store_image writes them: fig_<stamp>[_n].<ext>.
# Anchored, no path separators, no dots beyond the extension's -- so traversal
# cannot survive it even before the filesystem check in storage.figure_path.
FIGURE_NAME = re.compile(r"^fig_[A-Za-z0-9_-]+\.(?:png|jpe?g)$")

_FENCE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|$")
_COMMENT_OPEN = re.compile(r"^\s*<!--")
_COMMENT_CLOSE = re.compile(r"-->")
_IMAGE = re.compile(r'^!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+"[^"]*")?\s*\)\s*$')

# Inline spans, applied to ALREADY-ESCAPED text. Escaping rewrites & < > and ",
# none of which appear in these markers, so running them afterwards is safe and
# avoids double-escaping. Same ordering as the desktop renderer.
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![\w*])_([^_]+)_(?![\w*])")
# `*text*` as well as `_text_`. The desktop renderer handles only the
# underscore form, so an AutoPILOT block's title -- which report_builder emits
# as `*{title}*` -- shows its asterisks there. Supporting it here renders the
# author's evident intent rather than inventing syntax; it must run after
# _BOLD, which has already consumed the doubled markers.
_ITALIC_STAR = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)")


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _inline(text: str) -> str:
    """Escape `text`, then apply the inline spans the report actually uses.

    ``_LINK`` runs last so a link's label can carry the other spans, and it
    renders the link **inert** -- label plus target as plain text. See
    invariant 1 in the module docstring: no anchor is ever produced from
    report content.
    """
    out = _esc(text)
    out = _CODE.sub(r'<code class="c">\1</code>', out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    out = _ITALIC_STAR.sub(r"<em>\1</em>", out)
    out = _LINK.sub(r'\1 <span class="url">(\2)</span>', out)
    return out


def _image_html(alt: str, target: str) -> str:
    """One ``![alt](target)`` line, or an inert placeholder if it isn't ours.

    The only acceptable target is a figure this report pushed. Anything else
    -- an absolute path, a traversal, a remote URL -- is shown as text so the
    reader can see that something was there, without the page fetching it.
    """
    prefix = "figures/"
    name = target[len(prefix):] if target.startswith(prefix) else ""
    if not name or not FIGURE_NAME.match(name):
        return f'<p class="figure-missing">[figure not available: {_esc(target)}]</p>'
    return (
        '<figure class="fig">'
        f'<img src="figures/{_esc(name)}" alt="{_esc(alt)}" loading="lazy">'
        + (f'<figcaption>{_inline(alt)}</figcaption>' if alt else "")
        + "</figure>"
    )


def _table_html(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    head, body = rows[0], rows[1:]
    out = ['<table class="t"><thead><tr>']
    out += [f"<th>{_inline(cell)}</th>" for cell in head]
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def render(markdown: str) -> str:
    """Markdown -> an HTML fragment of the report body. Never raises."""
    lines = (markdown or "").splitlines()
    parts: list[str] = []
    para: list[str] = []
    i = 0

    def flush() -> None:
        if para:
            parts.append(f'<p>{_inline(" ".join(para))}</p>')
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # HTML comments are dropped whole -- the document opens with one, and
        # passing raw HTML through is exactly what must never happen here.
        if _COMMENT_OPEN.match(line):
            flush()
            while i < len(lines) and not _COMMENT_CLOSE.search(lines[i]):
                i += 1
            i += 1
            continue

        fence = _FENCE.match(stripped)
        if fence:
            flush()
            i += 1
            block: list[str] = []
            while i < len(lines) and not _FENCE.match(lines[i].strip()):
                block.append(lines[i])
                i += 1
            i += 1  # closing fence, or EOF which is fine
            parts.append(f'<pre class="code"><code>{_esc(chr(10).join(block))}</code></pre>')
            continue

        if not stripped:
            flush()
            i += 1
            continue

        if stripped in ("---", "***", "___"):
            flush()
            parts.append('<hr class="rule">')
            i += 1
            continue

        heading = _HEADING.match(stripped)
        if heading:
            flush()
            level = len(heading.group(1))
            parts.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue

        image = _IMAGE.match(stripped)
        if image:
            flush()
            parts.append(_image_html(image.group(1), image.group(2)))
            i += 1
            continue

        if stripped.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                if not _TABLE_SEP.match(lines[i].strip()):
                    rows.append(_split_row(lines[i]))
                i += 1
            parts.append(_table_html(rows))
            continue

        if stripped.startswith(">"):
            flush()
            quoted = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quoted.append(lines[i].lstrip()[1:].lstrip())
                i += 1
            body = "<br>".join(_inline(q) for q in quoted)
            parts.append(f'<blockquote class="q">{body}</blockquote>')
            continue

        para.append(stripped)
        i += 1

    flush()
    return "\n".join(parts)
