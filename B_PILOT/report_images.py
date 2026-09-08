"""Figures in the experiment report -- ingest, storage, and export packaging.

**Images are sidecar files, never inline in ``report.jsonl``.** That store's
whole durability argument (see :mod:`report_store`) is that one entry is one
``write()`` of one JSON line well under ``PIPE_BUF``, so concurrent writers
cannot interleave and a crash costs one entry rather than the file. A
base64 PNG is 100 kB-2 MB and breaks that outright. It would also make the
Report panel's 1 s poll re-parse megabytes on every rebuild. So the entry
stays tiny::

    {"ts": ..., "kind": "image", "file": "figures/fig_20260907_142530.png",
     "title": "Detector alignment", "w": 1440, "h": 900}

and the pixels live beside it::

    <session_dir>/<beamline>/experiments/<name>/figures/fig_....png

**Cost is paid once, at ingest.** An image is downscaled to
:data:`MAX_EDGE_PX` on its long edge and encoded as PNG, which is what
screenshots of plots and detector views want -- lossless, sharp text, and
typically 40-150 kB. Only if the PNG comes out over :data:`PNG_MAX_BYTES`
(a photograph rather than a plot) is JPEG tried instead, and even then only
if it actually wins. Nothing is re-encoded at render or export time.

This is the one report module that needs Qt: the clipboard is a Qt object and
``QImage`` is already the decoder every ingest path produces.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import time

from PyQt5 import QtCore
from PyQt5 import QtGui
from PyQt5 import QtWidgets

from . import experiment_history as eh

#: Subdirectory of the experiment folder that holds the figures. Also the
#: prefix stored in each entry's ``file``, so the stored path is relative to
#: the experiment folder and the whole record stays movable as one directory.
FIGURES_DIRNAME = "figures"

#: Long-edge cap applied on ingest. 1600 px keeps a full-screen grab readable
#: when opened at full size while cutting a 3.5 MB retina PNG to a few hundred
#: kB.
MAX_EDGE_PX = 1600

#: Above this, retry as JPEG. A plot screenshot never reaches it; a photo does.
PNG_MAX_BYTES = 400_000

JPEG_QUALITY = 85

#: What we are willing to read from an "Attach file..." dialog or a clipboard
#: file URL. Deliberately narrow -- this is a lab record, not a file manager.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp")

IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp)"

# `![alt](path)` / `![alt](path "title")`, which is what report_builder emits
# for an image entry and the only image syntax anything here has to handle.
IMAGE_MD = re.compile(r'^!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+"[^"]*")?\s*\)\s*$')


# ------------------------------------------------------------------ paths --

def base_dir(beamline: str, experiment: str) -> str:
    """The experiment folder stored ``file`` values are relative to.

    Renderers and exporters need this to turn ``figures/fig_....png`` back into
    a real path; keeping the join in one place is what stops the two of them
    from drifting apart.
    """
    return eh.experiment_dir(beamline, experiment)


def figures_dir(beamline: str, experiment: str) -> str:
    """The experiment's figure folder (not created)."""
    return os.path.join(eh.experiment_dir(beamline, experiment), FIGURES_DIRNAME)


def resolve(beamline: str, experiment: str, rel: str) -> str:
    """Absolute path for a ``file`` value stored in a report entry."""
    return os.path.join(eh.experiment_dir(beamline, experiment), rel)


def _unique_path(directory: str, stem: str, suffix: str) -> str:
    """``<stem><suffix>`` in `directory`, with ``_2``, ``_3``... on collision.

    Two figures captured inside the same second are entirely ordinary when
    pasting a series, and silently overwriting the first one would lose a
    record the user believed they had filed.
    """
    candidate = os.path.join(directory, stem + suffix)
    index = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}_{index}{suffix}")
        index += 1
    return candidate


# ----------------------------------------------------------------- ingest --

def from_clipboard() -> QtGui.QImage | None:
    """The clipboard's image, or ``None``.

    Falls back to a copied *file* whose URL points at an image, which is what
    the clipboard actually holds when the user copies a PNG in Finder or a
    file manager rather than copying pixels.
    """
    clipboard = QtWidgets.QApplication.clipboard()
    if clipboard is None:
        return None
    image = clipboard.image()
    if image is not None and not image.isNull():
        return image
    data = clipboard.mimeData()
    if data is not None and data.hasUrls():
        for url in data.urls():
            path = url.toLocalFile()
            if path and path.lower().endswith(IMAGE_SUFFIXES):
                loaded = load_file(path)
                if loaded is not None:
                    return loaded
    return None


def load_file(path: str) -> QtGui.QImage | None:
    """Read an image file, or ``None`` if it is missing or not an image."""
    image = QtGui.QImage()
    if not image.load(path) or image.isNull():
        return None
    return image


def _flatten(image: QtGui.QImage) -> QtGui.QImage:
    """Composite onto white -- JPEG has no alpha, and drops it to black."""
    if not image.hasAlphaChannel():
        return image
    flat = QtGui.QImage(image.size(), QtGui.QImage.Format_RGB32)
    flat.fill(QtGui.QColor("white"))
    painter = QtGui.QPainter(flat)
    painter.drawImage(0, 0, image)
    painter.end()
    return flat


def _encode(image: QtGui.QImage, fmt: str, quality: int = -1) -> bytes | None:
    """Encode in memory, so the size test costs no disk write."""
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.WriteOnly)
    ok = image.save(buffer, fmt, quality)
    data = bytes(buffer.data())
    buffer.close()
    return data if ok and data else None


def store_image(beamline: str, experiment: str, image: QtGui.QImage) -> dict | None:
    """Downscale, encode, and write `image` into the experiment's figures.

    Returns the fields to put in the report entry -- ``file`` (relative to the
    experiment folder), ``w``, ``h`` -- or ``None`` if there was nothing to
    store or it could not be written. Best-effort like every other report
    write: a failed figure must never take down a run in progress.
    """
    if image is None or image.isNull():
        return None

    if max(image.width(), image.height()) > MAX_EDGE_PX:
        image = image.scaled(
            MAX_EDGE_PX,
            MAX_EDGE_PX,
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation,
        )

    payload, suffix = _encode(image, "PNG"), ".png"
    if payload is None:
        return None
    if len(payload) > PNG_MAX_BYTES:
        jpeg = _encode(_flatten(image), "JPEG", JPEG_QUALITY)
        if jpeg is not None and len(jpeg) < len(payload):
            payload, suffix = jpeg, ".jpg"

    directory = figures_dir(beamline, experiment)
    try:
        os.makedirs(directory, exist_ok=True)
        path = _unique_path(directory, f"fig_{time.strftime('%Y%m%d_%H%M%S')}", suffix)
        with open(path, "wb") as fh:
            fh.write(payload)
    except OSError:
        return None

    return {
        "file": f"{FIGURES_DIRNAME}/{os.path.basename(path)}",
        "w": image.width(),
        "h": image.height(),
    }


# ----------------------------------------------------------------- export --

def data_uri(path: str) -> str:
    """``data:image/...;base64,...`` for `path`, or ``""`` if unreadable.

    Used only by HTML export, whose whole promise is a single self-contained
    file. The live panel never goes through this -- it points ``QTextBrowser``
    at the real file instead, so a report with fifty figures does not build a
    fifty-megabyte string on every one-second poll.
    """
    try:
        with open(path, "rb") as fh:
            payload = fh.read()
    except OSError:
        return ""
    suffix = os.path.splitext(path)[1].lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".webp": "image/webp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }.get(suffix, "application/octet-stream")
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")


def package_markdown(markdown: str, base_dir: str, md_path: str) -> str:
    """Copy the figures an exported Markdown file references, next to it.

    A Markdown export that points back into the live experiment folder is not
    a standalone copy -- it breaks the moment the report is mailed to someone
    or the session directory is cleaned. So the figures are copied to
    ``<name>_figures/`` beside the ``.md`` and the links rewritten to match.

    A figure that cannot be copied keeps its original link rather than being
    dropped: a broken image link is recoverable, a silently missing figure is
    not.
    """
    lines = markdown.splitlines()
    if not any(IMAGE_MD.match(line.strip()) for line in lines):
        return markdown

    stem = os.path.splitext(os.path.basename(md_path))[0]
    dest_name = f"{stem}_figures"
    dest_dir = os.path.join(os.path.dirname(os.path.abspath(md_path)), dest_name)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError:
        return markdown

    out = []
    for line in lines:
        match = IMAGE_MD.match(line.strip())
        if not match:
            out.append(line)
            continue
        alt, rel = match.group(1), match.group(2)
        source = os.path.join(base_dir, rel)
        try:
            shutil.copyfile(source, os.path.join(dest_dir, os.path.basename(rel)))
        except OSError:
            out.append(line)
            continue
        out.append(f"![{alt}]({dest_name}/{os.path.basename(rel)})")
    return "\n".join(out)
