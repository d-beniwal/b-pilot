"""On-disk store for pushed reports. One directory per view, no database.

```
<data>/<view_id>/
    meta.json     view secret, subject, timezone, digest, receive time, manifest
    report.md     the Markdown exactly as B-PILOT rendered it
    figures/      fig_*.png|jpg, as pushed
```

A report is text and a handful of screenshots, and there is never more than one
current version of it -- a database would buy nothing and add an operational
dependency to something whose whole appeal is that it is easy to stand up on a
spare VM. Retention is "keep the last state indefinitely", so the link keeps
working as a record after the beamtime ends.

Two rules here carry real weight:

* **Secrets are compared with :func:`hmac.compare_digest`.** A plain ``==``
  short-circuits on the first differing byte, which is a timing oracle against
  a 192-bit credential. It costs one import to get right.
* **A figure is served only while the current ``report.md`` references it**
  (:func:`references_figure`). B-PILOT derives the figures it uploads from the
  rendered document, so hiding an entry removes its image line -- and this
  check is what makes that hiding take effect on the pixels too, instantly,
  with no delete protocol and no garbage collection to get wrong. Without it,
  pasting a screenshot and then hiding it would leave it readable forever.

Every identifier that reaches the filesystem is validated by regex *and*
re-checked with ``realpath`` against the directory it must stay inside. Either
alone would probably do; both is cheap.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import time

VIEW_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
FIGURE_NAME = re.compile(r"^fig_[A-Za-z0-9_-]+\.(?:png|jpe?g)$")

MAX_DOC_BYTES = 5_000_000
MAX_FIGURE_BYTES = 8_000_000


def data_root() -> str:
    return os.path.abspath(os.environ.get("BPILOT_REPORT_DATA") or "./data")


def _view_dir(view_id: str) -> str | None:
    if not VIEW_ID.match(view_id or ""):
        return None
    root = data_root()
    path = os.path.join(root, view_id)
    # Belt and braces: the regex already forbids separators and dots, but the
    # filesystem gets the last word.
    if os.path.realpath(path) != os.path.join(os.path.realpath(root), view_id):
        return None
    return path


def _meta_path(view_id: str) -> str | None:
    directory = _view_dir(view_id)
    return os.path.join(directory, "meta.json") if directory else None


def load_meta(view_id: str) -> dict | None:
    path = _meta_path(view_id)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 -- absent or corrupt means "no such view"
        return None


def check_secret(view_id: str, secret: str) -> bool:
    """Constant-time secret check. False for an unknown view, too.

    Callers must answer an unknown view and a wrong secret **identically**
    (404 for both), so that the response does not confirm which view ids exist.
    """
    meta = load_meta(view_id)
    if not meta:
        return False
    return hmac.compare_digest(str(meta.get("secret") or ""), str(secret or ""))


def save_push(view_id: str, envelope: dict) -> bool:
    """Store one pushed report. Returns False if the view id is unusable."""
    directory = _view_dir(view_id)
    if not directory:
        return False
    os.makedirs(os.path.join(directory, "figures"), exist_ok=True)

    markdown = envelope.get("markdown") or ""
    tmp = os.path.join(directory, "report.md.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    os.replace(tmp, os.path.join(directory, "report.md"))

    meta = {
        "schema": envelope.get("schema"),
        "view_id": view_id,
        "secret": envelope.get("secret"),
        "beamline": envelope.get("beamline"),
        "experiment": envelope.get("experiment"),
        "title": envelope.get("title"),
        "tz": envelope.get("tz"),
        "tz_offset_s": envelope.get("tz_offset_s"),
        "sha256": envelope.get("sha256"),
        "generated_at": envelope.get("generated_at"),
        "received_at": time.time(),
        # Best effort and never rendered from -- see PROTOCOL.md. Kept so a
        # table of contents can be added later without a new wire format.
        "manifest": envelope.get("manifest") if isinstance(envelope.get("manifest"), list) else [],
    }
    tmp = os.path.join(directory, "meta.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    os.replace(tmp, os.path.join(directory, "meta.json"))
    return True


def read_markdown(view_id: str) -> str:
    directory = _view_dir(view_id)
    if not directory:
        return ""
    try:
        with open(os.path.join(directory, "report.md"), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def revoke(view_id: str) -> bool:
    """Delete a view and everything under it. Idempotent."""
    directory = _view_dir(view_id)
    if not directory or not os.path.isdir(directory):
        return False
    figures = os.path.join(directory, "figures")
    if os.path.isdir(figures):
        for name in os.listdir(figures):
            try:
                os.remove(os.path.join(figures, name))
            except OSError:
                pass
        try:
            os.rmdir(figures)
        except OSError:
            pass
    for name in ("report.md", "meta.json"):
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass
    try:
        os.rmdir(directory)
    except OSError:
        pass
    return True


def figure_path(view_id: str, name: str) -> str | None:
    """Absolute path of a stored figure, or ``None`` if it isn't a legal one."""
    directory = _view_dir(view_id)
    if not directory or not FIGURE_NAME.match(name or ""):
        return None
    figures = os.path.join(directory, "figures")
    path = os.path.join(figures, name)
    if os.path.realpath(path) != os.path.join(os.path.realpath(figures), name):
        return None
    return path if os.path.isfile(path) else None


def references_figure(view_id: str, name: str) -> bool:
    """Whether the *current* document names this figure.

    This is what makes hiding an entry take the pixels offline as well -- see
    the module docstring.
    """
    if not FIGURE_NAME.match(name or ""):
        return False
    return f"figures/{name}" in read_markdown(view_id)


def save_figure(view_id: str, name: str, blob: bytes) -> bool:
    directory = _view_dir(view_id)
    if not directory or not FIGURE_NAME.match(name or ""):
        return False
    if len(blob) > MAX_FIGURE_BYTES:
        return False
    figures = os.path.join(directory, "figures")
    os.makedirs(figures, exist_ok=True)
    tmp = os.path.join(figures, name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, os.path.join(figures, name))
    return True


def list_figures(view_id: str) -> list:
    """``[{"name", "sha256"}]`` for what is stored, so a client can skip re-uploads."""
    import hashlib

    directory = _view_dir(view_id)
    if not directory:
        return []
    figures = os.path.join(directory, "figures")
    if not os.path.isdir(figures):
        return []
    out = []
    for name in sorted(os.listdir(figures)):
        if not FIGURE_NAME.match(name):
            continue
        try:
            with open(os.path.join(figures, name), "rb") as fh:
                out.append({"name": name, "sha256": hashlib.sha256(fh.read()).hexdigest()})
        except OSError:
            continue
    return out
