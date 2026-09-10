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
  old revision, and a reader with the link can open revision history.
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

import io
import json
import os
import threading

from . import config
from . import report_sinks as sinks
from .report_sinks import AUTH, OK, REVOKED, TRANSIENT, Document, Sink

MISSING_REASON = ""
try:
    from google.auth.transport.requests import Request as _GoogleRequest
    from google.oauth2.credentials import Credentials as _Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow as _InstalledAppFlow
    from googleapiclient.discovery import build as _build
    from googleapiclient.errors import HttpError as _HttpError
    from googleapiclient.http import MediaIoBaseUpload as _MediaIoBaseUpload
except Exception as exc:  # noqa: BLE001 -- absent, or a broken partial install
    MISSING_REASON = f"{type(exc).__name__}: {exc}"

CREDENTIALS_ENV = "BPILOT_GDOCS_CREDENTIALS"

#: Only files this app created. Never widen this -- see the module docstring.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

TOKEN_FILENAME = "gdocs_token.json"

#: Floor on the push interval. A conversion upload is orders of magnitude more
#: expensive than an HTTP POST, and one Drive revision every five seconds would
#: make the document's history unreadable and burn quota for no reader benefit.
MIN_INTERVAL_S = 30.0

DOC_MIME = "application/vnd.google-apps.document"

#: What we upload for conversion. Markdown, deliberately: Drive converts it to
#: a Doc directly, so the payload is the *same bytes* the HTTP service gets and
#: byte-identical to ``Export -> Markdown``. The obvious alternative -- feeding
#: it ``report_render.to_html(embed_images=True)``, which already inlines
#: figures as data: URIs -- would drag the app's *theme* into a document that
#: has nothing to do with the screen (a dark session would upload near-white
#: text), and fixing that means ``style.temporary_theme``, which mutates module
#: globals and is therefore not safe to call from the worker thread while the
#: GUI paints. See PENDING below for when that trade is worth revisiting.
UPLOAD_MIME = "text/markdown"

# PENDING (figures): Markdown upload cannot carry ``figures/*.png`` -- the
# relative paths mean nothing to Drive, so a converted Doc shows the alt text
# and no image. Two ways out, and which one is right depends on a 15-minute
# manual check nobody has run yet: export the report as HTML, upload it to
# Drive, open it as a Doc, and see whether the base64 data: URIs survive.
#   * They survive -> switch the payload to HTML and solve the theme problem
#     above properly (an explicit palette argument on ``to_html``, not a global
#     swap).
#   * They do not -> upload each figure to Drive as its own file and rewrite
#     the image links to point at it. Figures then become links rather than
#     inline pixels, and ``wants_figures()`` below must start returning True.
# Until then this sink publishes text faithfully and figures not at all, and
# `share`'s dialog says so.

_lock = threading.Lock()


def available() -> bool:
    """Whether the Google client libraries imported."""
    return not MISSING_REASON


def credentials_path() -> str:
    """OAuth client-secrets file, from the environment.

    In the environment rather than the profile for the same reason
    ``BPILOT_REPORT_SYNC_TOKEN`` is: ``active_config.json`` is committed and
    beamline accounts are shared, so a profile that named a credentials file
    would arm every checkout of it.
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


def _save_token(creds) -> None:
    """Write the token 0600, atomically.

    Same discipline as ``report_views``: a temp file in the same directory then
    ``os.replace``, so a crash mid-write cannot leave a truncated credential
    that reads as "connected" but refreshes into an auth error forever.
    """
    path = token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_credentials():
    """Valid credentials, refreshing if needed, or ``None``.

    Never starts a consent flow -- see the module docstring. ``None`` here
    means "ask the user to reconnect in Configuration", which is what the
    worker turns into an ``auth_error``.
    """
    if not available() or not connected():
        return None
    try:
        creds = _Credentials.from_authorized_user_file(token_path(), SCOPES)
    except Exception:  # noqa: BLE001 -- corrupt or hand-edited token file
        return None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(_GoogleRequest())
        except Exception:  # noqa: BLE001 -- revoked, offline, clock skew
            return None
        try:
            _save_token(creds)
        except OSError:
            pass  # a token we could not cache still works for this session
        return creds
    return None


def connect() -> tuple[bool, str]:
    """Run the browser consent flow and store the token. GUI thread only.

    Returns ``(ok, message)``. Called from the Configuration dialog's
    "Connect Google account" button, never from the worker.
    """
    if not available():
        return False, f"the Google client libraries are not installed ({MISSING_REASON})"
    path = credentials_path()
    if not path:
        return False, f"{CREDENTIALS_ENV} is not set in the environment"
    if not os.path.isfile(path):
        return False, f"{CREDENTIALS_ENV} points at a file that does not exist: {path}"
    try:
        flow = _InstalledAppFlow.from_client_secrets_file(path, SCOPES)
        creds = flow.run_local_server(port=0)
        _save_token(creds)
    except Exception as exc:  # noqa: BLE001 -- user cancelled, bad client file, no browser
        return False, f"{type(exc).__name__}: {exc}"
    return True, "connected"


def disconnect() -> None:
    """Forget the stored token. Does not revoke it Google-side."""
    try:
        os.unlink(token_path())
    except OSError:
        pass


def _classify(exc) -> tuple[str, str]:
    """Map a Google API failure onto the sink outcome vocabulary."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    detail = ""
    try:
        payload = json.loads(getattr(exc, "content", b"") or b"{}")
        detail = (payload.get("error") or {}).get("message") or ""
    except Exception:  # noqa: BLE001
        detail = str(exc)[:200]
    message = f"HTTP {status}: {detail}" if status else f"{type(exc).__name__}: {exc}"
    if status in (401, 403):
        return AUTH, message
    if status in (404, 410):
        return REVOKED, message
    return TRANSIENT, message


class GDocsSink(Sink):
    """Publishes into one Google Doc per shared experiment."""

    name = "gdocs"
    label = "Google Docs"

    # Figures ride inside the document for the HTTP sink and not at all for
    # this one yet -- see the PENDING note at the top of the module. Either
    # way there is no separate upload phase today.
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

    # ── Drive plumbing ───────────────────────────────────────────────────────

    def _service(self):
        creds = load_credentials()
        if creds is None:
            return None
        # cache_discovery=False: the default file cache warns loudly under a
        # non-writable home and buys nothing for two endpoints.
        return _build("drive", "v3", credentials=creds, cache_discovery=False)

    @staticmethod
    def _doc_url(file_id: str) -> str:
        return f"https://docs.google.com/document/d/{file_id}/edit"

    def _create_doc(self, service, title: str) -> str:
        body = {"name": title, "mimeType": DOC_MIME}
        folder = (config.get("report_gdocs_folder_id") or "").strip()
        if folder:
            body["parents"] = [folder]
        created = service.files().create(body=body, fields="id").execute()
        return created["id"]

    def _share_anyone(self, service, file_id: str) -> None:
        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
        ).execute()

    def _unshare_anyone(self, service, file_id: str) -> None:
        """Remove every anyone-with-link grant. Idempotent."""
        listed = service.permissions().list(fileId=file_id, fields="permissions(id,type)").execute()
        for perm in listed.get("permissions", []):
            if perm.get("type") == "anyone":
                service.permissions().delete(fileId=file_id, permissionId=perm["id"]).execute()

    # ── sink protocol ────────────────────────────────────────────────────────

    def prepare_share(self, beamline: str, experiment: str, view: dict) -> dict:
        """Create the document and make it readable by link."""
        with _lock:
            service = self._service()
            if service is None:
                return {}
            title = (config.get("report_title") or "").strip() or experiment
            file_id = self._create_doc(service, f"{title} ({beamline})")
            self._share_anyone(service, file_id)
            return {"gdoc_id": file_id, "gdoc_url": self._doc_url(file_id)}

    def rotate_target(self, beamline: str, experiment: str, view: dict) -> dict:
        """Publish into a *new* document and retire the old one.

        A Doc's URL is its file id, so unlike the HTTP service there is no way
        to keep one document and invalidate its link. The old document is
        un-shared rather than deleted: it stays in the user's Drive as a record
        of what was published, which is the same promise ``stop_sharing``
        makes.
        """
        with _lock:
            service = self._service()
            if service is None:
                return {}
            old = (view or {}).get("gdoc_id")
            if old:
                try:
                    self._unshare_anyone(service, old)
                except Exception:  # noqa: BLE001 -- gone already, or no longer ours
                    pass
            title = (config.get("report_title") or "").strip() or experiment
            file_id = self._create_doc(service, f"{title} ({beamline})")
            self._share_anyone(service, file_id)
            return {"gdoc_id": file_id, "gdoc_url": self._doc_url(file_id)}

    def push_doc(self, view: dict, doc: Document) -> tuple[str, str]:
        """Replace the document's contents in place.

        ``files.update`` keeps the file id, so the URL every reader holds stays
        valid for the life of the share -- the same snapshot-not-deltas
        property the HTTP service has, for the same reason: a failed push is
        simply retried with newer content.
        """
        file_id = (view or {}).get("gdoc_id")
        if not file_id:
            return TRANSIENT, "this experiment has no Google Doc yet"
        with _lock:
            service = self._service()
            if service is None:
                return AUTH, "not connected to Google -- reconnect in Configuration"
            media = _MediaIoBaseUpload(
                io.BytesIO(doc.markdown.encode("utf-8")),
                mimetype=UPLOAD_MIME,
                resumable=False,
            )
            try:
                service.files().update(fileId=file_id, media_body=media).execute()
            except _HttpError as exc:
                return _classify(exc)
            except Exception as exc:  # noqa: BLE001 -- socket, DNS, TLS
                return TRANSIENT, f"{type(exc).__name__}: {exc}"
        return OK, ""

    def revoke(self, view: dict) -> tuple[str, str]:
        """Stop sharing: remove the link grant, keep the document.

        Deliberately not a delete. The document is the user's record of a
        beamtime and deleting it on "stop sharing" would destroy data to
        achieve access control. Removing the grant is what the button promises
        and all it should do.
        """
        file_id = (view or {}).get("gdoc_id")
        if not file_id:
            return OK, ""
        with _lock:
            service = self._service()
            if service is None:
                return AUTH, "not connected to Google -- reconnect in Configuration"
            try:
                self._unshare_anyone(service, file_id)
            except _HttpError as exc:
                outcome, message = _classify(exc)
                # A document that is already gone is a successful revoke.
                return (OK, "") if outcome == REVOKED else (outcome, message)
            except Exception as exc:  # noqa: BLE001
                return TRANSIENT, f"{type(exc).__name__}: {exc}"
        return OK, ""
