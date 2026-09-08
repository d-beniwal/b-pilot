"""Mirror the experiment report to a remote, read-only web viewer.

Collaborators who are not sitting at the beamline cannot see the report at all
today; the only sharing mechanism is an Export that is stale the moment it is
written. This module pushes the report *outward* to a small service the user
hosts (see ``report_server/`` in this repo), where anyone holding an
unguessable link can watch it update live.

The direction of travel is the whole security model. **Nothing is ever
accepted from the network** -- this module only ever makes outbound requests,
the workstation opens no port, and the service's reader-facing routes are
GET-only and take no input. A remote viewer cannot reach the instrument even in
principle, which is a property of the shape rather than of a setting.

**Three conditions arm it, not one** (see :func:`enabled`):

* ``report_sync_enabled`` -- the profile opts in;
* ``report_sync_url`` -- the profile names a service;
* ``BPILOT_REPORT_SYNC_TOKEN`` -- *this machine's environment* carries a push
  token.

The env var is not merely authentication. ``profiles/*/active_config.json`` is
committed to git and the beamline runs on shared accounts, so a flag alone
would arm every checkout of that profile -- a colleague's workstation, a dev
laptop -- for a service they never chose. Requiring something that lives only
in the environment makes the committed flag harmless everywhere it was not
intended, and it is the same reasoning (and the same ``~/.bashrc`` line) as
``ARGO_API_KEY`` in ``.context/DEPLOY.md``.

Even fully armed, this publishes nothing until the user shares a *specific*
experiment (:func:`share`). :func:`report_views.get_view` returning ``None`` is
the resting state.

**Why this module runs its own poll instead of hooking the Report panel.**
The obvious design calls out from ``report_panel.refresh()``, which already
rebuilds the document. It is wrong three ways, all of which bite during a real
beamtime:

* ``report_panel.closeEvent`` stops the panel's timer, and the dock's title-bar
  X routes straight to it -- so *closing the dock to make room for the console*
  would silently stop the remote from updating, with nothing to say so.
* ``main_window._reset_console_ui`` stops it on every kernel shutdown/restart.
* ``refresh()`` only runs when ``report_store.source_state()``'s byte sizes
  move. Changing ``report_title`` or ``report_excluded_plans`` re-renders the
  document with *zero* byte change, so the remote would keep serving runs the
  user had just excluded. That one is data exposure, not a cosmetic lag.

:mod:`report_builder` and :mod:`report_store` are Qt-free and pure, so the
worker does the whole build on its own thread and owes the GUI nothing. That
also avoids a real aliasing bug: ``render_markdown`` mutates the entries it is
handed, so passing the panel's live list across a thread boundary was never
safe.

``collect(persist=False)`` is mandatory here. ``persist=True`` appends the runs
it reconciles, and two writers doing that in one process would double-write.
Reconciliation stays the panel's job; this module only reads.

**What goes over the wire is the rendered Markdown, not the JSONL.** The store
is append-only, so shipping deltas would be tempting and cheap -- but it would
couple the service to the entry schema, and a ``report_builder`` change would
then need a coordinated service deploy in the middle of a beamtime. A snapshot
is also self-healing: a failed push is simply retried with newer content, with
no sequence numbers, gap detection or resync path to diverge. The cost is
re-sending the whole document, which ``gzip`` and a SHA-256 short-circuit
(:meth:`_Worker._build`) reduce to almost nothing. The Markdown pushed is
byte-identical to what ``Export -> Markdown`` writes.

**The link is a bearer credential.** ``secrets.token_urlsafe(24)`` is 192 bits
and not guessable, but anyone the URL is forwarded to has access until it is
rotated, and it lands in browser history. That is the right trade for
collaborators watching a run; it is *not* appropriate for embargoed data unless
the user accepts it. :func:`stop_sharing` and :func:`rotate` are the answer,
and :func:`stop_sharing` deliberately does not claim success if the service
could not be reached.

Structurally this mirrors :mod:`qs_client`: a lazy module-level singleton, one
``threading.Thread(daemon=True)``, a ``queue.Queue`` inbox, a lock around all
shared state, and errors that are *recorded and surfaced* rather than
swallowed -- the lesson of the ``item_add`` bug where a rejected call looked
exactly like a successful one. Unlike ``qs_client`` this module imports no Qt,
so it stays unit-testable without a display and the "never even imported when
switched off" property is easy to prove.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import queue as _queue
import ssl
import threading
import time
import urllib.error
import urllib.request

from . import config
from . import report_builder
from . import report_images as ri
from . import report_store as rs
from . import report_views

SCHEMA = 1

TOKEN_ENV = "BPILOT_REPORT_SYNC_TOKEN"
CAFILE_ENV = "BPILOT_REPORT_SYNC_CAFILE"

_TICK_S = 2.0            # worker wake interval when nothing is queued
_QUIET_S = 2.0           # trailing debounce: collapse a burst of edits into one push
_DOC_TIMEOUT_S = 20.0
_FIG_TIMEOUT_S = 60.0
_BACKOFF_S = (5, 10, 20, 40, 60)
_AUTH_BACKOFF_S = 300.0  # a bad token is not transient -- do not hammer it

# Client-side caps, mirroring the service's. A runaway kernel error loop can
# append tracebacks indefinitely; better to refuse locally, visibly, than to
# fill someone's VM.
MAX_DOC_BYTES = 5_000_000
MAX_FIGURE_BYTES = 8_000_000

# Outcome classes for one HTTP attempt.
_OK = "ok"
_AUTH = "auth"
_REVOKED = "revoked"
_TRANSIENT = "transient"


# ── configuration gate ───────────────────────────────────────────────────────

def push_token() -> str:
    return (os.environ.get(TOKEN_ENV) or "").strip()


def service_url() -> str:
    return (config.get("report_sync_url") or "").strip().rstrip("/")


def enabled() -> bool:
    """Whether sync is armed on *this machine*: profile flag + URL + push token.

    All three, deliberately -- see the module docstring on why the committed
    profile flag alone would arm checkouts that never opted in.
    """
    return bool(config.get("report_sync_enabled") and service_url() and push_token())


def max_staleness_s() -> float:
    """Longest the remote is allowed to lag, from ``report_sync_interval_s``.

    One number does for both halves of the debounce: it is the ceiling that
    stops a continuously-growing ``history.jsonl`` (a long scan streaming
    output) from starving the push forever, and the quiet period is derived
    from it rather than being a second knob to get wrong.
    """
    try:
        return max(2.0, float(config.get("report_sync_interval_s") or 5))
    except (TypeError, ValueError):
        return 5.0


# ── HTTP (background thread only) ────────────────────────────────────────────

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
        return _OK
    if code in (401, 403):
        return _AUTH
    if code in (404, 410):
        return _REVOKED
    return _TRANSIENT


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
            return _OK, "", resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = (exc.read() or b"")[:200].decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            pass
        return _classify(exc.code), f"HTTP {exc.code}{': ' + detail if detail else ''}", b""
    except urllib.error.URLError as exc:
        return _TRANSIENT, f"{exc.reason}", b""
    except Exception as exc:  # noqa: BLE001 -- socket timeouts, TLS errors, bad URLs
        return _TRANSIENT, f"{type(exc).__name__}: {exc}", b""


# ── the worker ───────────────────────────────────────────────────────────────

class _Worker:
    """Owns the poll, the debounce, the build and every outbound request.

    Nothing here touches Qt or the GUI thread. The panel reads :func:`state`,
    which is a lock-guarded snapshot of plain data.
    """

    def __init__(self, *, start: bool = True) -> None:
        # `start=False` builds the worker without its thread, so a test can
        # drive `_tick()` one iteration at a time and assert on exactly what
        # was sent. Publishing the wrong experiment's record is the failure
        # this feature must never have, and a real thread makes that
        # untestable rather than merely awkward.
        self._q: _queue.Queue = _queue.Queue()
        self._lock = threading.Lock()

        # Subject: pushed in from the GUI thread, never read from config here.
        # config._cache is invalidated on a profile switch and lazily re-read,
        # so a worker calling config.get("beamline") mid-switch can see the old
        # profile for a cycle -- and push one experiment's content to another
        # experiment's view token.
        self._beamline: str | None = None
        self._experiment: str | None = None

        # Change tracking / debounce
        self._seen: tuple | None = None      # (source_state, render-affecting config)
        self._dirty_since: float | None = None
        self._last_change: float = 0.0
        self._forced = False

        # Push state
        self._digest: str | None = None
        self._uploaded: dict = {}            # (view_id, name) -> sha256
        self._indexed: set = set()           # view_ids whose figure list we've fetched
        self._pushed_at: float | None = None
        self._status = "idle"
        self._error: str | None = None
        self._error_seq = 0
        self._fail_count = 0
        self._retry_at: float = 0.0

        self._thread = threading.Thread(
            target=self._run, name="report_sync worker", daemon=True
        )
        if start:
            self._thread.start()

    # ── inbox (any thread) ───────────────────────────────────────────────────

    def post(self, kind: str, args=None) -> None:
        self._q.put((kind, args))

    def snapshot(self) -> dict:
        with self._lock:
            view = None
            if self._beamline and self._experiment:
                view = report_views.get_view(self._beamline, self._experiment)
            return {
                "active": bool(view),
                "beamline": self._beamline,
                "experiment": self._experiment,
                "url": report_views.view_url(service_url(), view),
                "view_id": (view or {}).get("view_id"),
                "status": self._status,
                "pushed_at": self._pushed_at,
                "digest": self._digest,
                "figures": len(self._uploaded),
                "error": self._error,
                "error_seq": self._error_seq,
            }

    # ── state helpers (worker thread) ────────────────────────────────────────

    def _set_status(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self._status = status
            if error and error != self._error:
                self._error_seq += 1
            self._error = error

    def _reset_subject_state(self) -> None:
        self._seen = None
        self._digest = None
        self._dirty_since = None
        self._uploaded = {}
        self._indexed = set()
        self._fail_count = 0
        self._retry_at = 0.0
        self._forced = True

    # ── control messages ─────────────────────────────────────────────────────

    def _handle(self, kind: str, args) -> None:
        if kind == "subject":
            beamline, experiment = args
            with self._lock:
                if (beamline, experiment) == (self._beamline, self._experiment):
                    return
                self._beamline, self._experiment = beamline, experiment
            self._reset_subject_state()
            self._set_status("idle", None)
        elif kind == "reset":
            # Configuration changed: the service URL or the exclusions may be
            # different, so nothing cached about the last push still holds.
            self._reset_subject_state()
        elif kind == "force":
            self._forced = True
        elif kind == "revoke":
            self._revoke(*args)
        elif kind == "rotate":
            self._rotate(*args)

    # ── sharing lifecycle (worker thread) ────────────────────────────────────

    def _revoke(self, beamline: str, experiment: str) -> None:
        view = report_views.get_view(beamline, experiment)
        if not view:
            return
        outcome, message, _ = _request(
            "POST",
            f"{service_url()}/push/{view['view_id']}/revoke",
            body=b"",
            headers=self._auth_headers(),
            timeout=_DOC_TIMEOUT_S,
        )
        # A view the service has already forgotten is a successful revoke.
        if outcome in (_OK, _REVOKED):
            report_views.forget_view(beamline, experiment)
            self._reset_subject_state()
            self._forced = False
            self._set_status("idle", None)
        else:
            # Do NOT drop the local record: the link may still be live, and
            # saying otherwise would be the one lie this feature must not tell.
            self._set_status(
                "error",
                f"could not revoke the link -- it may still work: {message}",
            )

    def _rotate(self, beamline: str, experiment: str) -> None:
        if report_views.rotate_view(beamline, experiment) is None:
            return
        self._reset_subject_state()

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {push_token()}"}

    # ── main loop ────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while True:
            try:
                kind, args = self._q.get(timeout=_TICK_S)
            except _queue.Empty:
                kind, args = "tick", None
            try:
                if kind != "tick":
                    self._handle(kind, args)
                self._tick()
            except Exception as exc:  # noqa: BLE001 -- never let the worker thread die
                self._set_status("error", f"{type(exc).__name__}: {exc}")

    def _tick(self) -> None:
        with self._lock:
            beamline, experiment = self._beamline, self._experiment
        if not (beamline and experiment and enabled()):
            return
        view = report_views.get_view(beamline, experiment)
        if not view:
            return  # not shared -- the resting state for most experiments

        now = time.time()
        if self._status == "revoked":
            return  # the service disowned this view; re-sharing is a user action
        if now < self._retry_at:
            return

        # Anything that changes the *rendered* document has to be watched, not
        # just the files: a change to the title or the exclusion list re-renders
        # with no byte change at all, and the remote would otherwise keep
        # serving runs the user just excluded.
        signature = (
            rs.source_state(beamline, experiment),
            config.get("report_title") or "",
            tuple(config.get("report_excluded_plans") or []),
        )
        if signature != self._seen:
            self._seen = signature
            self._last_change = now
            if self._dirty_since is None:
                self._dirty_since = now

        if not (self._forced or self._dirty_since is not None):
            return
        if not self._forced:
            ceiling = max_staleness_s()
            quiet = min(_QUIET_S, ceiling / 2.0)
            settled = (now - self._last_change) >= quiet
            overdue = (now - self._dirty_since) >= ceiling
            if not (settled or overdue):
                return  # still mid-burst; coalesce

        self._push(beamline, experiment, view)

    # ── build and push ───────────────────────────────────────────────────────

    def _build(self, beamline: str, experiment: str) -> tuple[str, str, list, list]:
        """``(markdown, digest, figures, manifest)`` for the current record.

        ``persist=False``: reconciling runs into ``report.jsonl`` is the Report
        panel's job, and a second writer in the same process would double-write.
        """
        entries = report_builder.collect(
            beamline,
            experiment,
            persist=False,
            exclude=config.get("report_excluded_plans") or [],
        )
        manifest = _manifest(entries)
        markdown = report_builder.render_markdown(
            entries,
            experiment=experiment,
            beamline=beamline,
            title=config.get("report_title") or "",
            show_hidden=False,
            controls=False,
        )
        digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        return markdown, digest, _referenced_figures(markdown), manifest

    def _push(self, beamline: str, experiment: str, view: dict) -> None:
        markdown, digest, figures, manifest = self._build(beamline, experiment)

        pending = [f for f in figures if (view["view_id"], f) not in self._uploaded]
        if digest == self._digest and not pending and not self._forced:
            # Nothing the reader would see has changed. This is the common case
            # while a plan streams output into history.jsonl: the bytes move,
            # the rendered document does not, and we make no request at all.
            self._dirty_since = None
            return

        if len(markdown.encode("utf-8")) > MAX_DOC_BYTES:
            self._fail(f"report is larger than the {MAX_DOC_BYTES // 1_000_000} MB push limit")
            return

        self._set_status("pushing", self._error)

        if view["view_id"] not in self._indexed:
            self._index_figures(view)
            pending = [f for f in figures if (view["view_id"], f) not in self._uploaded]

        # Figures first: a document that references an image the service cannot
        # serve would render broken for every reader until the next cycle.
        for rel in pending:
            if not self._push_figure(beamline, experiment, view, rel):
                return

        envelope = {
            "schema": SCHEMA,
            "view_id": view["view_id"],
            "secret": view["secret"],
            "beamline": beamline,
            "experiment": experiment,
            "title": config.get("report_title") or "",
            "generated_at": time.time(),
            # render_markdown bakes beamline-local times with no marker, so an
            # off-site reader would silently misread every timestamp. The page
            # labels them with this rather than the document being rewritten.
            "tz": time.strftime("%Z"),
            "tz_offset_s": -(time.altzone if time.daylight and time.localtime().tm_isdst else time.timezone),
            "sha256": digest,
            "markdown": markdown,
            "figures": figures,
            "manifest": manifest,
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
        if outcome == _OK:
            self._digest = digest
            self._dirty_since = None
            self._forced = False
            self._fail_count = 0
            self._retry_at = 0.0
            self._pushed_at = time.time()
            self._set_status("ok", None)
        else:
            self._fail(message, outcome)

    def _push_figure(self, beamline: str, experiment: str, view: dict, rel: str) -> bool:
        path = ri.resolve(beamline, experiment, rel)
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError as exc:
            # A figure the record references but disk no longer has. The
            # document already renders a "[missing figure]" placeholder for
            # this, so don't let it block the push forever -- note it and go on.
            self._uploaded[(view["view_id"], rel)] = "missing"
            self._set_status("pushing", f"skipped a missing figure ({rel}): {exc}")
            return True
        if len(blob) > MAX_FIGURE_BYTES:
            self._uploaded[(view["view_id"], rel)] = "oversize"
            self._set_status("pushing", f"skipped an oversize figure ({rel})")
            return True

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
        if outcome == _OK:
            self._uploaded[(view["view_id"], rel)] = hashlib.sha256(blob).hexdigest()
            return True
        self._fail(f"figure {name}: {message}", outcome)
        return False

    def _index_figures(self, view: dict) -> None:
        """Learn what the service already holds, so a restart re-uploads nothing."""
        outcome, _, payload = _request(
            "GET",
            f"{service_url()}/push/{view['view_id']}/figures",
            headers=self._auth_headers(),
            timeout=_DOC_TIMEOUT_S,
        )
        if outcome != _OK:
            return  # best effort; worst case we re-upload
        try:
            known = json.loads(payload.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return
        for item in known if isinstance(known, list) else []:
            name = (item or {}).get("name")
            if name:
                self._uploaded[(view["view_id"], f"figures/{name}")] = item.get("sha256", "")
        self._indexed.add(view["view_id"])

    def _fail(self, message: str, outcome: str = _TRANSIENT) -> None:
        """Record a failure and decide when to try again. Never drops the work."""
        if outcome == _AUTH:
            self._retry_at = time.time() + _AUTH_BACKOFF_S
            self._set_status("auth_error", message)
            return
        if outcome == _REVOKED:
            self._set_status("revoked", message)
            return
        delay = _BACKOFF_S[min(self._fail_count, len(_BACKOFF_S) - 1)]
        self._fail_count += 1
        self._retry_at = time.time() + delay
        self._set_status("retrying", message)


def _referenced_figures(markdown: str) -> list:
    """``figures/...`` paths the *rendered document* actually references.

    Deriving this from the document rather than from the entries or the
    figures directory is what keeps a hidden figure off the service. Hiding an
    entry drops its image line from the Markdown, so the pixels stop being
    referenced -- and the service serves a figure only while the stored
    document names it. Without this, pasting a screenshot and then hiding it
    would leave the pixels on the server indefinitely: ``show_hidden=False``
    protects the document, not the blob store.
    """
    found = []
    for line in markdown.splitlines():
        match = ri.IMAGE_MD.match(line.strip())
        if not match:
            continue
        rel = match.group(2)
        if rel.startswith("figures/") and "/" not in rel[len("figures/"):] and ".." not in rel:
            if rel not in found:
                found.append(rel)
    return found


def _manifest(entries: list) -> list:
    """A flat summary of the visible entries, for the reader page's chrome.

    Strictly best-effort and strictly non-load-bearing: the service renders
    from the Markdown alone and ignores this entirely if it doesn't recognise
    the schema. It exists so a table of contents or a run counter can be added
    later without changing the wire format mid-beamtime.
    """
    out = []
    try:
        visible = report_builder.ordered(
            report_builder.visible_entries(entries, show_hidden=False)
        )
    except Exception:  # noqa: BLE001
        return out
    for entry in visible:
        out.append(
            {
                "id": report_builder.entry_id(entry),
                "ts": entry.get("ts"),
                "kind": entry.get("kind"),
                "title": (entry.get("title") or "")[:200],
                "plan_name": entry.get("plan_name"),
                "ok": entry.get("ok"),
            }
        )
    return out


# ── module-level API ─────────────────────────────────────────────────────────

_worker: _Worker | None = None
_IDLE_STATE = {
    "active": False,
    "beamline": None,
    "experiment": None,
    "url": "",
    "view_id": None,
    "status": "off",
    "pushed_at": None,
    "digest": None,
    "figures": 0,
    "error": None,
    "error_seq": 0,
}


def _get_worker() -> _Worker:
    global _worker
    if _worker is None:
        _worker = _Worker()
    return _worker


def set_subject(beamline: str | None, experiment: str | None) -> None:
    """Point the sync at one experiment. Call whenever either could have changed.

    Deliberately a no-op -- and deliberately does not create the worker thread
    -- unless :func:`enabled`, so a machine that never opted in never starts a
    thread or opens a socket.
    """
    if not enabled():
        return
    _get_worker().post("subject", (beamline, experiment))


def state() -> dict:
    """Snapshot for the panel's status chip. Instant; safe on the GUI thread."""
    if _worker is None:
        return dict(_IDLE_STATE)
    return _worker.snapshot()


def is_shared(beamline: str, experiment: str) -> bool:
    return report_views.get_view(beamline, experiment) is not None


def share(beamline: str, experiment: str) -> str:
    """Start sharing this experiment; returns the reader URL.

    Mints the view if there isn't one and forces an immediate push, so the link
    is live rather than blank by the time the user has pasted it somewhere.
    """
    view = report_views.ensure_view(beamline, experiment)
    if enabled():
        worker = _get_worker()
        worker.post("subject", (beamline, experiment))
        worker.post("force", None)
    return report_views.view_url(service_url(), view)


def stop_sharing(beamline: str, experiment: str) -> None:
    """Ask the service to drop this view, then forget it locally.

    Order matters and so does the failure case: the local record is kept if the
    revoke did not get through, and :func:`state` reports that the old link may
    still work. Claiming otherwise would be worse than not offering the button.
    """
    if enabled():
        _get_worker().post("revoke", (beamline, experiment))
    else:
        report_views.forget_view(beamline, experiment)


def rotate(beamline: str, experiment: str) -> str:
    """Retire the current link and mint a new one for the same experiment."""
    view = report_views.rotate_view(beamline, experiment)
    if enabled():
        worker = _get_worker()
        worker.post("rotate", (beamline, experiment))
        worker.post("force", None)
    return report_views.view_url(service_url(), view)


def view_url(beamline: str, experiment: str) -> str:
    return report_views.view_url(service_url(), report_views.get_view(beamline, experiment))


def reset() -> None:
    """Re-read configuration on the worker thread. Non-blocking.

    Called from ``ConfigDialog.accept`` -- guarded there, as ``qs_client.reset``
    is, so that saving Configuration on a machine with sync switched off never
    brings the worker thread into existence.
    """
    if _worker is not None:
        _worker.post("reset", None)
