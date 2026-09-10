"""Where a shared report is published to, and the contract every target keeps.

:mod:`report_sync` owns *when* to publish -- the poll, the debounce, the
SHA-256 short-circuit, the backoff, the status the panel shows. This module
owns *where*, and nothing else. The split exists because the two halves have
completely different reasons to change: the scheduling half is finished and
load-bearing, while the destination half grew a second implementation (Google
Docs) the moment it turned out that publishing the HTTP service needs a VM,
TLS and egress the beamline may not have.

**The outcome vocabulary is the seam.** Every sink call returns one of
:data:`OK`, :data:`AUTH`, :data:`REVOKED`, :data:`TRANSIENT`, and that is the
whole reason one backoff implementation can serve targets as different as a
FastAPI service and the Drive API. A sink's job is to translate its own error
language into these four; the worker's job is to decide what to do about them,
and it does not care which target it is talking to.

* :data:`OK` -- it worked.
* :data:`AUTH` -- the credential is wrong or withdrawn. Not transient: the
  worker backs off for five minutes rather than hammering it.
* :data:`REVOKED` -- the target no longer exists. Re-sharing is a *user*
  action, so the worker stops rather than silently recreating something the
  user may have deleted on purpose.
* :data:`TRANSIENT` -- anything else. Retried with escalating backoff.

**Sinks must not raise.** The worker runs on a daemon thread whose death would
be silent, so every method returns an outcome instead of propagating. The one
exception is programmer error, which the worker's own catch-all reports as a
status.

**Sinks must be cheap to construct and safe to call from the GUI thread for
:meth:`Sink.url` alone.** The panel asks for the current link while painting a
status chip; that must never become a network call. Every sink therefore
stores whatever it needs to answer ``url()`` in the view record itself.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import config
from . import report_views

SCHEMA = 1

TOKEN_ENV = "BPILOT_REPORT_SYNC_TOKEN"
CAFILE_ENV = "BPILOT_REPORT_SYNC_CAFILE"

_DOC_TIMEOUT_S = 20.0
_FIG_TIMEOUT_S = 60.0

# Outcome classes for one publish attempt. See the module docstring: this
# four-value vocabulary is the entire contract between a sink and the worker.
OK = "ok"
AUTH = "auth"
REVOKED = "revoked"
TRANSIENT = "transient"

# Client-side caps, mirroring the HTTP service's. A runaway kernel error loop
# can append tracebacks indefinitely; better to refuse locally, visibly, than
# to fill someone's VM -- or someone's Drive quota.
MAX_DOC_BYTES = 5_000_000
MAX_FIGURE_BYTES = 8_000_000


@dataclass(frozen=True)
class Document:
    """One rendered snapshot of the report, ready to publish.

    Frozen and passed whole rather than as seven arguments, because a sink that
    quietly reordered ``beamline`` and ``experiment`` would publish one
    experiment's record under another's link -- the single failure this feature
    must never have.
    """

    beamline: str
    experiment: str
    markdown: str
    digest: str
    figures: list
    manifest: list
    title: str = ""


class Sink:
    """What a publish target must implement.

    Subclasses translate their own transport's errors into the four outcome
    constants and never raise.
    """

    #: Value of ``report_sync_backend`` that selects this sink.
    name = ""

    #: Human-readable target, for status text and error messages.
    label = ""

    def enabled(self) -> bool:
        """Whether this machine is armed to publish to this target."""
        raise NotImplementedError

    def url(self, view: dict | None) -> str:
        """Reader-facing URL for `view`. Must not do I/O -- see the docstring."""
        raise NotImplementedError

    def wants_figures(self) -> bool:
        """Whether figures are published as separate uploads.

        ``False`` for a target that carries its pixels inside the document
        itself, which makes the worker skip the whole figure-upload phase
        rather than the sink having to no-op six calls.
        """
        return True

    def min_interval_s(self) -> float:
        """Floor on how often this target may be written, in seconds.

        A cheap HTTP POST can happily run at the user's configured interval. A
        target where every write costs a document conversion and a permanent
        revision-history entry cannot, and the floor belongs with the target
        that knows why -- not in a config key the user has to get right.
        """
        return 0.0

    def known_figures(self, view: dict) -> dict | None:
        """``{rel: sha256}`` the target already holds, so a restart re-uploads
        nothing.

        ``None`` means *could not tell* -- the listing failed -- and the worker
        will ask again next cycle. An empty dict is an answer: the target holds
        nothing. Collapsing the two would turn one transient network blip into
        a permanent re-upload of every figure.
        """
        return {}

    def push_doc(self, view: dict, doc: Document) -> tuple[str, str]:
        """Publish `doc`. Returns ``(outcome, message)``."""
        raise NotImplementedError

    def push_figure(self, view: dict, rel: str, blob: bytes) -> tuple[str, str]:
        """Publish one figure. Returns ``(outcome, message)``."""
        return OK, ""

    def revoke(self, view: dict) -> tuple[str, str]:
        """Retire the published copy. Returns ``(outcome, message)``.

        A target that has already forgotten this view must report :data:`OK` or
        :data:`REVOKED`; both mean "the link is dead", which is what the caller
        needs to know.
        """
        raise NotImplementedError

    def prepare_share(self, beamline: str, experiment: str, view: dict) -> dict:
        """Hook for a target that must create something before the first push.

        Returns any fields to merge into the stored view record (the Google
        sink puts its document id here). The HTTP service needs nothing: the
        first push creates the view server-side, so the default is a no-op.
        """
        return {}

    def rotate_target(self, beamline: str, experiment: str, view: dict) -> dict:
        """Hook for retiring the old link when a fresh secret is not enough.

        The HTTP service addresses a view by ``view_id`` + ``secret``, so
        ``report_views.rotate_view`` alone kills the old URL and this is a
        no-op. A Google Doc's URL *is* its file id, so the only way to retire a
        link is to publish into a new document and un-share the old one; that
        sink returns the new id here.
        """
        return {}


# ── HTTP: the report_server/ service ─────────────────────────────────────────

def push_token() -> str:
    return (os.environ.get(TOKEN_ENV) or "").strip()


def service_url() -> str:
    return (config.get("report_sync_url") or "").strip().rstrip("/")


def _ssl_context() -> ssl.SSLContext:
    """Default verification, optionally against a private CA.

    An APS-internal VM with a self-signed certificate is the likely real snag
    here. The answer is a CA file, from the environment alongside the push
    token -- deliberately *not* a "skip verification" config key, which would
    be a permanent hole added to dodge a one-time setup problem.
    """
    return ssl.create_default_context(cafile=os.environ.get(CAFILE_ENV) or None)


def _classify(code: int) -> str:
    if 200 <= code < 300:
        return OK
    if code in (401, 403):
        return AUTH
    if code in (404, 410):
        return REVOKED
    return TRANSIENT


def _request(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: dict | None = None,
    timeout: float,
) -> tuple[str, str, bytes]:
    """One HTTP attempt. Returns ``(outcome, message, payload)``; never raises.

    ``timeout`` is always passed: ``urlopen`` defaults to *no* timeout, and a
    hung socket would wedge this daemon thread silently and forever -- the same
    shape as the queueserver GUI-freeze incident, just moved off the GUI thread.
    """
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return OK, "", resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = (exc.read() or b"")[:200].decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            pass
        return _classify(exc.code), f"HTTP {exc.code}{': ' + detail if detail else ''}", b""
    except urllib.error.URLError as exc:
        return TRANSIENT, f"{exc.reason}", b""
    except Exception as exc:  # noqa: BLE001 -- socket timeouts, TLS errors, bad URLs
        return TRANSIENT, f"{type(exc).__name__}: {exc}", b""


class HttpSink(Sink):
    """The ``report_server/`` service: a snapshot POSTed to a host the user runs.

    The only target that is genuinely *live* -- readers watch a page that polls
    for a new digest -- and the only one where hiding an entry takes its pixels
    offline instantly, because the service serves a figure only while the
    stored document names it.
    """

    name = "http"
    label = "report viewer service"

    def enabled(self) -> bool:
        return bool(config.get("report_sync_enabled") and service_url() and push_token())

    def url(self, view: dict | None) -> str:
        return report_views.view_url(service_url(), view)

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {push_token()}"}

    def known_figures(self, view: dict) -> dict | None:
        outcome, _, payload = _request(
            "GET",
            f"{service_url()}/push/{view['view_id']}/figures",
            headers=self._auth_headers(),
            timeout=_DOC_TIMEOUT_S,
        )
        if outcome != OK:
            return None  # ask again next cycle rather than re-uploading
        try:
            known = json.loads(payload.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
        out = {}
        for item in known if isinstance(known, list) else []:
            name = (item or {}).get("name")
            if name:
                out[f"figures/{name}"] = item.get("sha256", "")
        return out

    def push_doc(self, view: dict, doc: Document) -> tuple[str, str]:
        envelope = {
            "schema": SCHEMA,
            "view_id": view["view_id"],
            "secret": view["secret"],
            "beamline": doc.beamline,
            "experiment": doc.experiment,
            "title": doc.title,
            "generated_at": _now(),
            # render_markdown bakes beamline-local times with no marker, so an
            # off-site reader would silently misread every timestamp. The page
            # labels them with this rather than the document being rewritten.
            "tz": _tz_name(),
            "tz_offset_s": _tz_offset_s(),
            "sha256": doc.digest,
            "markdown": doc.markdown,
            "figures": doc.figures,
            "manifest": doc.manifest,
        }
        body = gzip.compress(json.dumps(envelope).encode("utf-8"))
        outcome, message, _ = _request(
            "POST",
            f"{service_url()}/push/{view['view_id']}",
            body=body,
            headers={
                **self._auth_headers(),
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
            timeout=_DOC_TIMEOUT_S,
        )
        return outcome, message

    def push_figure(self, view: dict, rel: str, blob: bytes) -> tuple[str, str]:
        name = os.path.basename(rel)
        outcome, message, _ = _request(
            "POST",
            f"{service_url()}/push/{view['view_id']}/figures/{name}",
            body=blob,
            headers={
                **self._auth_headers(),
                "Content-Type": "application/octet-stream",
                "X-Content-Sha256": hashlib.sha256(blob).hexdigest(),
            },
            timeout=_FIG_TIMEOUT_S,
        )
        return outcome, (f"figure {name}: {message}" if message else "")

    def revoke(self, view: dict) -> tuple[str, str]:
        outcome, message, _ = _request(
            "POST",
            f"{service_url()}/push/{view['view_id']}/revoke",
            body=b"",
            headers=self._auth_headers(),
            timeout=_DOC_TIMEOUT_S,
        )
        return outcome, message


def _now() -> float:
    return time.time()


def _tz_name() -> str:
    return time.strftime("%Z")


def _tz_offset_s() -> int:
    return -(time.altzone if time.daylight and time.localtime().tm_isdst else time.timezone)


# ── selection ────────────────────────────────────────────────────────────────

_SINKS: dict = {}
_gdocs_reason = "not checked yet"


def _register_builtin() -> None:
    if HttpSink.name not in _SINKS:
        _SINKS[HttpSink.name] = HttpSink

    # The Google sink is optional: its client libraries are not part of the
    # pinned beamline environment, and B-PILOT must run everywhere they are
    # absent. A guarded import here (the `autopilot_bridge` pattern) means the
    # backend is simply not offered rather than the app failing to start.
    #
    # The module always imports; it is `available()` that reports whether the
    # libraries did. That split is what lets the Configuration page say *why*
    # the backend is missing instead of silently omitting it.
    # The outbox sink is pure stdlib, so it is always available -- which is
    # the point: it is the backend for a machine that can install nothing.
    if "outbox" not in _SINKS:
        from .report_outbox import FileSink

        _SINKS[FileSink.name] = FileSink

    global _gdocs_reason
    if "gdocs" not in _SINKS:
        try:
            from . import report_gdocs
        except Exception as exc:  # noqa: BLE001 -- a broken install
            _gdocs_reason = f"{type(exc).__name__}: {exc}"
            return
        if not report_gdocs.available():
            _gdocs_reason = report_gdocs.MISSING_REASON
            return
        _gdocs_reason = ""
        _SINKS[report_gdocs.GDocsSink.name] = report_gdocs.GDocsSink


def available() -> list:
    """Backend names this installation can actually use."""
    _register_builtin()
    return sorted(_SINKS)


def backend_name() -> str:
    """The configured backend, falling back to HTTP if it is unavailable.

    Falling back rather than erroring matters on a workstation that pulls a
    profile selecting ``gdocs`` without the client libraries installed: the
    report simply keeps publishing the way it did before, and the
    Configuration page says why.
    """
    _register_builtin()
    want = (config.get("report_sync_backend") or HttpSink.name).strip()
    return want if want in _SINKS else HttpSink.name


def get_sink(name: str | None = None) -> Sink:
    """Construct the named sink (default: the configured one)."""
    _register_builtin()
    return _SINKS[name or backend_name()]()


def unavailable_reason(name: str) -> str:
    """Why `name` is not in :func:`available`, for the Configuration page.

    An empty string means it *is* available (or that nothing is known about
    the name). Users who switch the backend and find nothing happens deserve
    the actual import error, not silence.
    """
    _register_builtin()
    if name in _SINKS:
        return ""
    return _gdocs_reason if name == "gdocs" else f"unknown backend {name!r}"
