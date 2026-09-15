"""Google Drive operations for publishing a report, with no B-PILOT in them.

Two callers need exactly the same six Drive operations from very different
places: :mod:`report_gdocs`, the in-app sink, which learns everything from the
active profile; and the relay daemon (``report_relay/``), which runs on a
different machine with no profile, no Qt and no GUI, and is told everything by
command line. So this module takes its configuration as *arguments* -- paths,
a folder id -- and reads no config of its own.

That parameterisation is the whole point. The alternative was a second copy of
the create/share/update/rotate/revoke logic living in the relay, drifting from
this one, and being discovered to have drifted at 3 a.m. during a beamtime.

**Scope is ``drive.file`` and must stay that way.** It grants access only to
files this application itself created -- not the user's Drive. On a shared
beamline account, or on a relay host where a copied token sits on disk, that is
the difference between an exposed set of report documents and an exposed Google
account.

**Consent is never automatic.** :func:`connect` opens a browser and is called
only in response to a person pressing a button. Everything else refreshes an
existing token and reports failure; a relay running headless simply never
calls it (copy an already-authorised token file to that machine instead).
"""
from __future__ import annotations

import io
import json
import os

MISSING_REASON = ""
try:
    from google.auth.transport.requests import Request as _GoogleRequest
    from google.oauth2.credentials import Credentials as _Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow as _InstalledAppFlow
    from googleapiclient.discovery import build as _build
    from googleapiclient.errors import HttpError as HttpError
    from googleapiclient.http import MediaIoBaseUpload as _MediaIoBaseUpload
except Exception as exc:  # noqa: BLE001 -- absent, or a broken partial install
    MISSING_REASON = f"{type(exc).__name__}: {exc}"
    HttpError = Exception  # so `except HttpError` is still legal downstream

#: Only files this app created. Never widen this -- see the module docstring.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

DOC_MIME = "application/vnd.google-apps.document"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MARKDOWN_MIME = "text/markdown"

# Outcome vocabulary, matching report_sinks so a sink can return these
# unchanged. Duplicated as plain strings rather than imported, because the
# relay must be able to use this module without pulling in the sink machinery.
OK = "ok"
AUTH = "auth"
REVOKED = "revoked"
TRANSIENT = "transient"


def available() -> bool:
    return not MISSING_REASON


def classify(exc) -> tuple[str, str]:
    """Map a Google API failure onto the outcome vocabulary."""
    status = getattr(getattr(exc, "resp", None), "status", None)
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


def doc_url(file_id: str) -> str:
    return f"https://docs.google.com/document/d/{file_id}/edit"


# ── credentials ──────────────────────────────────────────────────────────────

def save_token(creds, token_path: str) -> None:
    """Write the token 0600, atomically.

    A temp file in the same directory then ``os.replace``, so a crash mid-write
    cannot leave a truncated credential that reads as "connected" but refreshes
    into an auth error forever.
    """
    os.makedirs(os.path.dirname(token_path) or ".", exist_ok=True)
    tmp = token_path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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
    os.replace(tmp, token_path)


def load_credentials(token_path: str):
    """Valid credentials, refreshing if needed, or ``None``.

    Never starts a consent flow. ``None`` means "a person must reconnect",
    which callers turn into an :data:`AUTH` outcome rather than a browser
    window opening by itself.
    """
    if not available() or not os.path.isfile(token_path):
        return None
    try:
        creds = _Credentials.from_authorized_user_file(token_path, SCOPES)
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
            save_token(creds, token_path)
        except OSError:
            pass  # a token we could not cache still works for this session
        return creds
    return None


def connect(credentials_path: str, token_path: str) -> tuple[bool, str]:
    """Run the browser consent flow and store the token. Needs a browser.

    Returns ``(ok, message)``. A headless relay never calls this: authorise on
    a machine with a browser and copy the resulting token file across, mode
    0600. The token is not bound to the machine that minted it.
    """
    if not available():
        return False, f"the Google client libraries are not installed ({MISSING_REASON})"
    if not credentials_path:
        return False, "no OAuth client-secrets file configured"
    if not os.path.isfile(credentials_path):
        return False, f"the OAuth client-secrets file does not exist: {credentials_path}"
    try:
        flow = _InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
        creds = flow.run_local_server(port=0)
        save_token(creds, token_path)
    except Exception as exc:  # noqa: BLE001 -- cancelled, bad client file, no browser
        return False, f"{type(exc).__name__}: {exc}"
    return True, "connected"


# ── the client ───────────────────────────────────────────────────────────────

class DriveClient:
    """The six Drive operations publishing a report needs.

    Every method raises :data:`HttpError` on a Google-side failure; callers
    translate with :func:`classify`. Construction is cheap and does no I/O --
    the service is built lazily so an unauthorised host can still import and
    instantiate this without exploding.
    """

    def __init__(self, *, token_path: str, folder_id: str = "") -> None:
        self.token_path = token_path
        self.folder_id = (folder_id or "").strip()
        self._service = None

    def connected(self) -> bool:
        return os.path.isfile(self.token_path)

    def service(self):
        """The Drive service, or ``None`` when there is no usable credential."""
        if self._service is not None:
            return self._service
        creds = load_credentials(self.token_path)
        if creds is None:
            return None
        # cache_discovery=False: the default file cache warns loudly under a
        # non-writable home and buys nothing for two endpoints.
        self._service = _build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def invalidate(self) -> None:
        """Drop the cached service, so the next call re-reads the token."""
        self._service = None

    def create_doc(self, service, title: str) -> str:
        body = {"name": title, "mimeType": DOC_MIME}
        if self.folder_id:
            body["parents"] = [self.folder_id]
        return service.files().create(body=body, fields="id").execute()["id"]

    def share_anyone(self, service, file_id: str) -> None:
        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
        ).execute()

    def unshare_anyone(self, service, file_id: str) -> None:
        """Remove every anyone-with-link grant. Idempotent."""
        listed = service.permissions().list(
            fileId=file_id, fields="permissions(id,type)"
        ).execute()
        for perm in listed.get("permissions", []):
            if perm.get("type") == "anyone":
                service.permissions().delete(
                    fileId=file_id, permissionId=perm["id"]
                ).execute()

    def update_doc(self, service, file_id: str, payload: bytes, mimetype: str) -> None:
        """Replace the document's contents, keeping its file id and URL."""
        media = _MediaIoBaseUpload(io.BytesIO(payload), mimetype=mimetype, resumable=False)
        service.files().update(fileId=file_id, media_body=media).execute()

    # ── composed operations, used identically by the sink and the relay ──────

    def publish(self, file_id: str, payload: bytes, mimetype: str) -> tuple[str, str]:
        """Update one document. Returns ``(outcome, message)``; never raises."""
        service = self.service()
        if service is None:
            return AUTH, "not connected to Google"
        try:
            self.update_doc(service, file_id, payload, mimetype)
        except HttpError as exc:
            outcome, message = classify(exc)
            if outcome == AUTH:
                self.invalidate()
            return outcome, message
        except Exception as exc:  # noqa: BLE001 -- socket, DNS, TLS
            return TRANSIENT, f"{type(exc).__name__}: {exc}"
        return OK, ""

    def create_shared(self, title: str) -> tuple[str, str, str]:
        """Create a document shared by link. ``(file_id, url, error)``."""
        service = self.service()
        if service is None:
            return "", "", "not connected to Google"
        try:
            file_id = self.create_doc(service, title)
            self.share_anyone(service, file_id)
        except HttpError as exc:
            return "", "", classify(exc)[1]
        except Exception as exc:  # noqa: BLE001
            return "", "", f"{type(exc).__name__}: {exc}"
        return file_id, doc_url(file_id), ""

    def retire(self, file_id: str) -> tuple[str, str]:
        """Un-share a document, keeping it. ``(outcome, message)``.

        Deliberately not a delete: the document is the user's record of a
        beamtime, and destroying data to achieve access control is the wrong
        trade. A document that is already gone counts as retired.
        """
        if not file_id:
            return OK, ""
        service = self.service()
        if service is None:
            return AUTH, "not connected to Google"
        try:
            self.unshare_anyone(service, file_id)
        except HttpError as exc:
            outcome, message = classify(exc)
            return (OK, "") if outcome == REVOKED else (outcome, message)
        except Exception as exc:  # noqa: BLE001
            return TRANSIENT, f"{type(exc).__name__}: {exc}"
        return OK, ""
