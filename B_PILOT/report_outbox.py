"""Publish the report into a shared folder, for a relay to pick up.

For the case the other two backends cannot serve: a beamline workstation with
**no route to the internet**. It writes the rendered report and its figures to
a directory on shared storage; a daemon on a machine that *does* have internet
(``report_relay/``) watches that directory and publishes to Google Docs.

**Why an outbox rather than letting the relay read ``report.jsonl`` directly.**
The raw record contains hidden entries and excluded plans; they are filtered
out at *render* time by ``report_builder``, using this profile's
``report_excluded_plans`` and ``show_hidden=False``. A relay that rendered the
record itself would have to reproduce that filtering exactly, on another
machine, with its own copy of the config -- and any drift publishes something
the user deliberately hid. So what crosses is an **already-filtered rendered
document**, and the relay ships only what it is handed. This is the same
argument that made the wire format a rendered document rather than the JSONL,
applied to a second transport.

**The layout is deliberately identical to what ``report_server`` stores**
(``report.md``, ``figures/``, ``meta.json`` per view). One relay implementation
therefore serves both topologies -- fed by this sink over shared storage, or by
the HTTP service over a network -- and moving between them later costs nothing.

**Nothing here needs a dependency.** Pure stdlib, so a beamline workstation
running this backend needs no Google client libraries and no ``python-docx``:
the relay does all of that. On a host whose environment is pinned and hard to
change, that is the whole point.

**The relay writes back.** When it has published, it drops ``published.json``
into the view directory with the reader URL, which :meth:`FileSink.url` reads.
So the link still appears in B-PILOT's Report panel, even though B-PILOT never
spoke to Google and could not have minted it.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from . import config
from .report_sinks import OK, TRANSIENT, Document, Sink

#: The outbox root, from the environment. Same discipline as the other
#: backends' credentials: a profile travels between workstations, and one
#: naming an outbox would start feeding the relay from a machine that never
#: chose to publish. The relay publishes whatever appears here, so this is a
#: publishing decision, not merely a path.
OUTBOX_ENV = "BPILOT_REPORT_OUTBOX"

PUBLISHED_FILENAME = "published.json"
REVOKED_FILENAME = "revoked.json"
META_FILENAME = "meta.json"
DOC_FILENAME = "report.md"
SCHEMA = 1


def outbox_root() -> str:
    return (os.environ.get(OUTBOX_ENV) or "").strip()


def _view_dir(view: dict | None) -> str:
    root = outbox_root()
    view_id = (view or {}).get("view_id") or ""
    if not (root and view_id):
        return ""
    return os.path.join(root, view_id)


def _write_atomic(path: str, payload: bytes) -> None:
    """Write via a temp file in the same directory, then ``os.replace``.

    Not a nicety here: the reader is a *different process on another machine*
    polling this directory, and a partially written ``report.md`` would be
    published as a truncated document. ``os.replace`` is atomic within a
    filesystem, which is exactly the guarantee needed.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class FileSink(Sink):
    """Writes the report to a shared folder; a relay does the publishing."""

    name = "outbox"
    label = "shared folder (relay publishes)"

    def enabled(self) -> bool:
        root = outbox_root()
        return bool(config.get("report_sync_enabled") and root and os.path.isdir(root))

    def url(self, view: dict | None) -> str:
        """The reader URL the relay reported, or ``""`` until it has published.

        A small local read rather than a network call, so it stays safe on the
        GUI thread -- and it fails quiet, because a hung mount must not take
        the Report panel's status chip down with it.
        """
        directory = _view_dir(view)
        if not directory:
            return ""
        try:
            with open(os.path.join(directory, PUBLISHED_FILENAME), encoding="utf-8") as fh:
                return (json.load(fh) or {}).get("url") or ""
        except Exception:  # noqa: BLE001 -- not published yet, or the mount is away
            return ""

    def known_figures(self, view: dict) -> dict | None:
        """What the outbox already holds, so a restart re-copies nothing."""
        directory = _view_dir(view)
        if not directory:
            return None
        figures = os.path.join(directory, "figures")
        if not os.path.isdir(figures):
            return {}
        out = {}
        try:
            for name in sorted(os.listdir(figures)):
                if name.endswith(".tmp"):
                    continue
                with open(os.path.join(figures, name), "rb") as fh:
                    out[f"figures/{name}"] = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return None  # the mount is away; ask again rather than re-copying
        return out

    def prepare_share(self, beamline: str, experiment: str, view: dict) -> dict:
        """Create the view directory, and clear any stale revoke marker."""
        directory = _view_dir(view)
        if not directory:
            return {}
        try:
            os.makedirs(os.path.join(directory, "figures"), exist_ok=True)
            _remove(os.path.join(directory, REVOKED_FILENAME))
        except OSError:
            pass
        return {}

    def rotate_target(self, beamline: str, experiment: str, view: dict) -> dict:
        """Ask the relay for a new document by bumping the generation.

        The relay compares the generation it last published against the one in
        ``meta.json``; a higher one means "retire the old document and publish
        into a fresh one". Doing it through the file rather than through a
        second marker keeps one source of truth for the view's state.
        """
        directory = _view_dir(view)
        if not directory:
            return {}
        # The relay has not seen the new generation yet, so the old URL is
        # stale from this instant. Drop it rather than hand it back.
        _remove(os.path.join(directory, PUBLISHED_FILENAME))
        return {"outbox_generation": int((view or {}).get("outbox_generation") or 0) + 1}

    def push_doc(self, view: dict, doc: Document) -> tuple[str, str]:
        directory = _view_dir(view)
        if not directory:
            return TRANSIENT, f"{OUTBOX_ENV} is not set, or this experiment has no view"
        meta = {
            "schema": SCHEMA,
            "view_id": view.get("view_id"),
            "generation": int(view.get("outbox_generation") or 0),
            "beamline": doc.beamline,
            "experiment": doc.experiment,
            "title": doc.title,
            "sha256": doc.digest,
            "generated_at": time.time(),
            # render_markdown bakes beamline-local times with no marker, so a
            # reader elsewhere would silently misread every timestamp.
            "tz": time.strftime("%Z"),
            "tz_offset_s": -(
                time.altzone if time.daylight and time.localtime().tm_isdst else time.timezone
            ),
            "figures": list(doc.figures),
        }
        try:
            # Document first, then meta: the relay triggers on meta.json's
            # digest, so writing it last means it never points at a report.md
            # that has not landed yet.
            _write_atomic(os.path.join(directory, DOC_FILENAME), doc.markdown.encode("utf-8"))
            _write_atomic(
                os.path.join(directory, META_FILENAME),
                json.dumps(meta, indent=2).encode("utf-8"),
            )
        except OSError as exc:
            return TRANSIENT, f"could not write to the outbox: {exc}"
        return OK, ""

    def push_figure(self, view: dict, rel: str, blob: bytes) -> tuple[str, str]:
        directory = _view_dir(view)
        if not directory:
            return TRANSIENT, f"{OUTBOX_ENV} is not set"
        name = os.path.basename(rel)
        try:
            _write_atomic(os.path.join(directory, "figures", name), blob)
        except OSError as exc:
            return TRANSIENT, f"figure {name}: {exc}"
        return OK, ""

    def revoke(self, view: dict) -> tuple[str, str]:
        """Tell the relay to un-share, and take the content out of the outbox.

        The document and its figures are removed here immediately -- there is
        no reason for a copy the user has stopped sharing to sit on shared
        storage -- while the marker stays until the relay has acted on it.
        """
        directory = _view_dir(view)
        if not directory or not os.path.isdir(directory):
            return OK, ""
        try:
            _write_atomic(
                os.path.join(directory, REVOKED_FILENAME),
                json.dumps({"at": time.time()}, indent=2).encode("utf-8"),
            )
            _remove(os.path.join(directory, DOC_FILENAME))
            _remove(os.path.join(directory, PUBLISHED_FILENAME))
            figures = os.path.join(directory, "figures")
            if os.path.isdir(figures):
                for name in os.listdir(figures):
                    _remove(os.path.join(figures, name))
        except OSError as exc:
            # Do NOT report success: the relay has not necessarily seen this,
            # so the Doc may still be shared, and saying otherwise would be the
            # one lie this feature must not tell.
            return TRANSIENT, f"could not write the revoke marker: {exc}"
        return OK, ""


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
