"""Security invariants for the report viewer service.

These ship with the service rather than living in B-PILOT's gitignored
``scripts/`` directory, because they are the reason anyone should be willing to
put a lab record on a public-facing box: they are part of the deployable, and
they must be runnable by whoever deploys it.

The threat model is not hypothetical. A report's body is assembled from kernel
output, notes pasted from anywhere, and prose drafted by an LLM -- then served
over the network to people the beamline does not control. Everything below is
an attack that text could carry.

Run directly (``python -m report_server.tests.test_security``) or under pytest.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from report_server import render, storage  # noqa: E402

# Anything that would execute, fetch, or navigate.
_SCRIPT = re.compile(r"<\s*script", re.I)
_ANCHOR = re.compile(r"<\s*a\b[^>]*\bhref", re.I)
_IMG_SRC = re.compile(r'<img[^>]*\bsrc="([^"]*)"', re.I)

# Real emitted tags. Because every text node is escaped, a literal "<" in the
# output can only be a tag the renderer chose to emit -- so enumerating them
# and checking each against an allowlist is a stronger statement than grepping
# for known-bad substrings, and it does not trip over escaped text that merely
# *reads* like an attack (`&lt;img onerror=...&gt;` is inert, and a substring
# search cannot tell the difference).
_TAG = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)((?:\s[^>]*)?)/?>")
_ATTR = re.compile(r"([a-zA-Z-]+)\s*=")

ALLOWED_TAGS = {
    "p", "strong", "em", "code", "pre", "hr", "br", "span", "div",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td",
    "blockquote", "figure", "figcaption", "img",
}
ALLOWED_ATTRS = {"class", "src", "alt", "loading"}


def _assert_tags_allowed(out: str, source: str) -> None:
    for match in _TAG.finditer(out):
        tag, attrs = match.group(1).lower(), match.group(2)
        assert tag in ALLOWED_TAGS, f"tag <{tag}> emitted from {source!r}"
        for attr in _ATTR.findall(attrs):
            assert attr.lower() in ALLOWED_ATTRS, (
                f"attribute {attr!r} emitted from {source!r} -> {match.group(0)!r}"
            )

HOSTILE = [
    "<script>alert(1)</script>",
    '"><script>alert(1)</script>',
    "<img src=x onerror=alert(1)>",
    "<iframe src=javascript:alert(1)></iframe>",
    "[click me](javascript:alert(1))",
    "[home](file:///etc/passwd)",
    "[control](bpilot:hide/abc123)",
    "![beacon](https://evil.example/pixel.png)",
    "![esc](figures/../../../../etc/passwd)",
    '![quote](figures/fig_1.png" onerror="alert(1))',
    "![absolute](/etc/passwd)",
    "![data](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=)",
    "<!-- --><script>alert(1)</script><!-- -->",
    "| <script>alert(1)</script> | b |",
    "> <img src=x onerror=alert(1)>",
    "```\n<script>alert(1)</script>\n```",
    "# <script>alert(1)</script>",
    "**<script>alert(1)</script>**",
    "`<script>alert(1)</script>`",
]


def test_no_execution_or_navigation_from_report_text() -> None:
    """No hostile input yields a script, an event handler, or a link.

    Note what "survived" means: hostile text is *shown*, escaped and inert --
    a report must not silently swallow content. So the assertion is about the
    tags actually emitted, not about whether the attack string appears.
    """
    for source in HOSTILE:
        out = render.render(source)
        assert not _SCRIPT.search(out), f"script survived: {source!r} -> {out!r}"
        assert not _ANCHOR.search(out), f"anchor emitted: {source!r} -> {out!r}"
        _assert_tags_allowed(out, source)


def test_image_src_is_only_ever_an_own_figure() -> None:
    """The only `src` the renderer will emit is a validated relative figure."""
    for source in HOSTILE + ["![ok](figures/fig_20260908_101112.png)"]:
        for src in _IMG_SRC.findall(render.render(source)):
            assert src.startswith("figures/"), f"non-figure src: {src!r}"
            name = src[len("figures/"):]
            assert render.FIGURE_NAME.match(name), f"unvalidated figure name: {src!r}"
            assert ".." not in src and "//" not in src


def test_a_real_figure_still_renders() -> None:
    """The hardening must not have broken the feature it protects."""
    out = render.render("![Detector view](figures/fig_20260908_101112.png)")
    assert '<img src="figures/fig_20260908_101112.png"' in out
    assert "Detector view" in out


def test_control_links_are_inert_here() -> None:
    """`bpilot:` controls are clickable in the desktop panel, never remotely."""
    out = render.render("[hide](bpilot:hide/abc)")
    assert "bpilot:hide/abc" in out       # shown, so nothing is silently lost
    assert not _ANCHOR.search(out)        # but not navigable


def test_no_line_is_silently_dropped() -> None:
    """A lab record must not lose content to an unrecognised construct."""
    out = render.render("::: some future syntax :::")
    assert "some future syntax" in out


def test_document_structure_round_trips() -> None:
    """Every construct report_builder.render_markdown actually emits."""
    out = render.render(
        "<!-- header comment -->\n"
        "# Title\n\n"
        "**Experiment:** X\n\n"
        "---\n\n"
        "## Monday\n\n"
        "#### Note - 10:11\n\n"
        "> a quoted note\n\n"
        "| Reading | Value | Units |\n|---|---|---|\n| I0 | 1.2 | mA |\n\n"
        "```\nRE(scan(...))\n```\n"
    )
    assert "<!--" not in out                    # comment dropped whole
    assert "<h1>Title</h1>" in out
    assert '<hr class="rule">' in out
    assert "<h2>Monday</h2>" in out
    assert "<blockquote" in out and "a quoted note" in out
    assert "<th>Reading</th>" in out and "<td>I0</td>" in out
    assert "<pre" in out and "RE(scan(...))" in out


def test_storage_rejects_traversal_and_bad_ids() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BPILOT_REPORT_DATA"] = tmp
        for bad in ("../etc", "a/b", "", ".", "..", "x" * 40):
            assert storage.figure_path(bad, "fig_1.png") is None
            assert storage.save_push(bad, {"markdown": "x"}) is False
        assert storage.save_push("good1", {"markdown": "hi", "secret": "s"}) is True
        for bad in ("../../etc/passwd", "fig_1.png/../../x", "notafig.png", "fig_1.exe"):
            assert storage.figure_path("good1", bad) is None
            assert storage.save_figure("good1", bad, b"x") is False


def test_wrong_secret_and_unknown_view_are_indistinguishable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BPILOT_REPORT_DATA"] = tmp
        storage.save_push("v1", {"markdown": "hi", "secret": "correct-horse"})
        assert storage.check_secret("v1", "correct-horse") is True
        assert storage.check_secret("v1", "wrong") is False
        assert storage.check_secret("nosuchview", "anything") is False


def test_figure_is_served_only_while_the_document_references_it() -> None:
    """Hiding an entry in B-PILOT must take its pixels offline immediately."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BPILOT_REPORT_DATA"] = tmp
        name = "fig_20260908_101112.png"
        storage.save_push("v2", {"markdown": f"![x](figures/{name})", "secret": "s"})
        storage.save_figure("v2", name, b"\x89PNG fake")
        assert storage.references_figure("v2", name) is True
        assert storage.figure_path("v2", name) is not None

        # The user hides that entry: the next push simply omits the image line.
        storage.save_push("v2", {"markdown": "# nothing here", "secret": "s"})
        assert storage.references_figure("v2", name) is False


def test_constant_time_comparison_is_used() -> None:
    """A plain `==` on a 192-bit credential is a timing oracle."""
    source = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "storage.py"), encoding="utf-8").read()
    assert "hmac.compare_digest" in source


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
