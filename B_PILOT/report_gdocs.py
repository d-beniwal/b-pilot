"""Publish the report as a Google Doc, shared read-only by link.

The alternative to :class:`report_sinks.HttpSink` for people who have no host
to run ``report_server/`` on. It needs no VM, no TLS, no reverse proxy and no
inbound egress rule -- just a Google account -- and it gives that up in
exchange for three things the HTTP service does better, all of which the UI
has to say out loud rather than bury:

* **It is not live.** A Doc is a document. Readers refresh; the push cadence is
  floored at :data:`MIN_INTERVAL_S` because every write costs a conversion and
  a permanent revision-history entry.
* **Hiding an entry is no longer instant.** The HTTP service serves a figure
  only while the current document names it, so hiding takes the pixels offline
  immediately. Here the next push rewrites the document, but Drive keeps the
  old revision, and a reader with the link can open revision history. This is
  the sharpest difference between the two targets and the UI says so.

Figures *do* publish: the payload is a ``.docx`` built by :mod:`report_docx`,
whose images are real files inside the archive and survive Drive's conversion
as inline pictures. See :data:`UPLOAD_MIME` for why not Markdown and why not
themed HTML.
* **The link is a Google sharing link**, with everything that implies about
  who can forward it.

**Scope is ``drive.file`` and must stay that way.** That grants access only to
files this application itself created -- not the user's Drive. It is the
difference between a stolen token exposing the report documents and a stolen
token exposing everything the user owns, which matters because the beamline
runs on shared accounts. Nothing here needs a broader scope: the sink creates
its own documents and only ever touches those.

**Optional by construction.** The Google client libraries are not part of the
pinned beamline environment, and B-PILOT must run everywhere they are absent,
so the imports are guarded exactly as ``autopilot_bridge`` guards AutoPILOT's.
:func:`available` is how the rest of the app asks; a machine without them
simply is not offered the backend.

**Consent never happens on the worker thread.** :func:`connect` runs the
browser flow and is called only from the Configuration dialog, in response to
a button. The worker refreshes an existing token and, if that fails, reports
``auth_error`` telling the user to reconnect -- it never pops a browser window
in the middle of a beamtime.
"""
from __future__ import annotations

import os
import threading

from . import config
from . import experiment_history as eh
from . import report_docx
from . import report_drive as dr
from . import report_sinks as sinks
from .report_sinks import AUTH, OK, REVOKED, TRANSIENT, Document, Sink

# Everything that actually talks to Drive lives in report_drive, which takes
# its configuration as arguments so the headless relay daemon can use the same
# code with no profile, no Qt and no GUI. This module is the config-driven
# half: it decides *which* token, *which* folder, *which* title, and nothing
# else.
MISSING_REASON = dr.MISSING_REASON

CREDENTIALS_ENV = "BPILOT_GDOCS_CREDENTIALS"
SCOPES = dr.SCOPES
TOKEN_FILENAME = "gdocs_token.json"

#: Floor on the push interval. A conversion upload is orders of magnitude more
#: expensive than an HTTP POST, and one Drive revision every five seconds would
#: make the document's history unreadable and burn quota for no reader benefit.
MIN_INTERVAL_S = 30.0

#: What we upload for conversion: a ``.docx`` built by :mod:`report_docx`.
#: Drive converts it into a native Google Doc, and -- the reason for this
#: choice -- images inside a ``.docx`` are real files in the archive rather
#: than links or base64 in markup, so figures survive the conversion as inline
#: pictures. Uploading the Markdown is simpler but a relative
#: ``figures/x.png`` path means nothing to Drive, so every figure is dropped.
#:
#: Feeding it ``report_render.to_html(embed_images=True)`` instead was the
#: other candidate. It was rejected for a structural reason, not a cosmetic
#: one: its colours come from the *session's* theme, so a dark session would
#: publish near-white text, and the established fix for that
#: (``style.temporary_theme``, used by the PDF exporter) rebinds module globals
#: -- safe on the GUI thread, not safe on this worker's thread while the GUI
#: paints. ``report_docx`` carries no palette at all.
UPLOAD_MIME = dr.DOCX_MIME

#: Falls back to uploading the Markdown when python-docx is not installed.
#: Text still publishes; only the figures are lost. Degrading is better than
#: refusing to publish at all, and the Configuration page says which is in use.
FALLBACK_MIME = dr.MARKDOWN_MIME

_lock = threading.Lock()


def available() -> bool:
    """Whether the Google client libraries imported."""
    return dr.available()


def credentials_path() -> str:
    """OAuth client-secrets file, from the environment.

    In the environment rather than the profile for the same reason
    ``BPILOT_REPORT_SYNC_TOKEN`` is: profiles travel between workstations, so a
    profile that named a credentials file would arm every checkout of it.
    """
    return (os.environ.get(CREDENTIALS_ENV) or "").strip()


def token_path() -> str:
    """Where the refresh token lives: one per user, not one per beamline.

    A Google account is a property of the person, not of the instrument, so
    this sits at the session root rather than under a beamline directory the
    way ``report_views.json`` does.
    """
    root = os.path.expanduser(config.get("session_dir") or "~/.bluesky_pilot")
    return os.path.join(root, TOKEN_FILENAME)


def connected() -> bool:
    return os.path.isfile(token_path())


def connect() -> tuple[bool, str]:
    """Run the browser consent flow. GUI thread only, user-initiated."""
    path = credentials_path()
    if not path:
        return False, f"{CREDENTIALS_ENV} is not set in the environment"
    return dr.connect(path, token_path())


def disconnect() -> None:
    """Forget the stored token. Does not revoke it Google-side."""
    try:
        os.unlink(token_path())
    except OSError:
        pass


def _client() -> "dr.DriveClient":
    return dr.DriveClient(
        token_path=token_path(),
        folder_id=config.get("report_gdocs_folder_id") or "",
    )


def _payload(doc: Document) -> tuple[bytes, str]:
    """``(bytes, mimetype)`` to upload for conversion.

    A ``.docx`` when :mod:`report_docx` is usable, so figures come through;
    the raw Markdown otherwise, which still publishes the text.
    """
    if report_docx.available():
        base = eh.experiment_dir(doc.beamline, doc.experiment)
        title = doc.title or doc.experiment
        return report_docx.build(doc.markdown, base_dir=base, title=title), UPLOAD_MIME
    return doc.markdown.encode("utf-8"), FALLBACK_MIME


def figures_supported() -> bool:
    """Whether a published document will carry its figures.

    The Report panel and the Configuration page both word themselves from this
    rather than promising something the installation cannot deliver.
    """
    return report_docx.available()


class GDocsSink(Sink):
    """Publishes into one Google Doc per shared experiment."""

    name = "gdocs"
    label = "Google Docs"

    # Figures ride *inside* the uploaded .docx as real archive members, so
    # there is no separate upload phase -- unlike the HTTP service, which
    # posts each figure alongside the document.
    def wants_figures(self) -> bool:
        return False

    def min_interval_s(self) -> float:
        return MIN_INTERVAL_S

    def enabled(self) -> bool:
        return bool(
            available()
            and config.get("report_sync_enabled")
            and credentials_path()
            and connected()
        )

    def url(self, view: dict | None) -> str:
        return (view or {}).get("gdoc_url") or ""

    # ── sink protocol ────────────────────────────────────────────────────────

    def _title(self, beamline: str, experiment: str) -> str:
        configured = (config.get("report_title") or "").strip()
        return f"{configured or experiment} ({beamline})"

    def prepare_share(self, beamline: str, experiment: str, view: dict) -> dict:
        """Create the document and make it readable by link."""
        with _lock:
            file_id, url, error = _client().create_shared(self._title(beamline, experiment))
            return {"gdoc_id": file_id, "gdoc_url": url} if not error else {}

    def rotate_target(self, beamline: str, experiment: str, view: dict) -> dict:
        """Publish into a *new* document and retire the old one.

        A Doc's URL is its file id, so unlike the HTTP service there is no way
        to keep one document and invalidate its link. The old document is
        un-shared rather than deleted: it stays in the user's Drive as a record
        of what was published, which is the same promise ``revoke`` makes.
        """
        with _lock:
            client = _client()
            old = (view or {}).get("gdoc_id")
            if old:
                client.retire(old)  # best effort; a failure must not block the new link
            file_id, url, error = client.create_shared(self._title(beamline, experiment))
            return {"gdoc_id": file_id, "gdoc_url": url} if not error else {}

    def push_doc(self, view: dict, doc: Document) -> tuple[str, str]:
        """Replace the document's contents in place.

        ``files.update`` keeps the file id, so the URL every reader holds stays
        valid for the life of the share -- the same snapshot-not-deltas
        property the HTTP service has, and for the same reason: a failed push
        is simply retried with newer content.
        """
        file_id = (view or {}).get("gdoc_id")
        if not file_id:
            return TRANSIENT, "this experiment has no Google Doc yet"
        try:
            payload, mimetype = _payload(doc)
        except Exception as exc:  # noqa: BLE001 -- a malformed record
            return TRANSIENT, f"could not build the document: {type(exc).__name__}: {exc}"
        with _lock:
            return _client().publish(file_id, payload, mimetype)

    def revoke(self, view: dict) -> tuple[str, str]:
        """Stop sharing: remove the link grant, keep the document."""
        with _lock:
            return _client().retire((view or {}).get("gdoc_id") or "")
