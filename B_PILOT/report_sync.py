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
exactly like a successful one. Nothing here touches the GUI thread or any
widget, so the whole module is driveable from a test with ``start=False``.

**Where it publishes to lives in :mod:`report_sinks`.** This module owns *when*
-- the poll, the debounce, the digest short-circuit, the backoff, the status --
and delegates *where* to a sink chosen by ``report_sync_backend``. The four
outcome constants are the seam; see that module's docstring. A second target
(Google Docs) exists because publishing the HTTP service needs a host, TLS and
egress that a beamline may not have, and it trades live updates and the
instant-hide guarantee for needing no hosting at all.

One caveat on "no Qt": :mod:`report_images`, imported here to resolve figure
paths, imports PyQt5, so this module is not importable in a bare interpreter
even though nothing in it touches a widget. :mod:`report_builder` and
:mod:`report_store` *are* genuinely Qt-free, which is what lets the document be
rendered off the GUI thread.
"""
from __future__ import annotations

import hashlib
import queue as _queue
import threading
import time

from . import config
from . import report_builder
from . import report_images as ri
from . import report_sinks as sinks
from . import report_store as rs
from . import report_views
from .report_sinks import AUTH as _AUTH
from .report_sinks import MAX_DOC_BYTES
from .report_sinks import MAX_FIGURE_BYTES
from .report_sinks import OK as _OK
from .report_sinks import REVOKED as _REVOKED
from .report_sinks import TRANSIENT as _TRANSIENT

# Re-exported, not redefined: ``config_dialog`` and ``report_panel`` both name
# ``report_sync.TOKEN_ENV`` / ``push_token()`` / ``service_url()`` when telling
# the user which arming condition is missing, and those call sites should not
# have to know that the transport moved into :mod:`report_sinks`.
SCHEMA = sinks.SCHEMA
TOKEN_ENV = sinks.TOKEN_ENV
CAFILE_ENV = sinks.CAFILE_ENV
push_token = sinks.push_token
service_url = sinks.service_url

_TICK_S = 2.0            # worker wake interval when nothing is queued
_QUIET_S = 2.0           # trailing debounce: collapse a burst of edits into one push
_BACKOFF_S = (5, 10, 20, 40, 60)
_AUTH_BACKOFF_S = 300.0  # a bad token is not transient -- do not hammer it

# ── configuration gate ───────────────────────────────────────────────────────

def enabled() -> bool:
    """Whether sync is armed on *this machine*, per the configured backend.

    Delegated to the sink because the conditions differ by target: the HTTP
    service needs a URL and a push token, while Google Docs needs client
    credentials and a connected account. What does *not* differ is that at
    least one of them always lives in the environment rather than the profile
    -- see the module docstring on why the committed flag alone must never be
    enough to arm a checkout.
    """
    return sinks.get_sink().enabled()


def max_staleness_s(sink: sinks.Sink | None = None) -> float:
    """Longest the remote is allowed to lag, in seconds.

    One number does for both halves of the debounce: it is the ceiling that
    stops a continuously-growing ``history.jsonl`` (a long scan streaming
    output) from starving the push forever, and the quiet period is derived
    from it rather than being a second knob to get wrong.

    Floored by the sink. A target where every write costs a document
    conversion and a permanent revision-history entry cannot be written every
    five seconds, and that constraint belongs to the target rather than to a
    config key the user has to know to raise. `sink` is passed explicitly by
    the worker so it uses the sink it is actually publishing through, not
    whatever the config names right now.
    """
    try:
        want = max(2.0, float(config.get("report_sync_interval_s") or 5))
    except (TypeError, ValueError):
        want = 5.0
    return max(want, (sink or sinks.get_sink()).min_interval_s())


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

        # Where this publishes. Rebuilt on `reset` so a backend change in
        # Configuration takes effect without restarting the GUI.
        self._sink = sinks.get_sink()

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
                "url": self._sink.url(view),
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
            # Configuration changed: the backend, the service URL or the
            # exclusions may be different, so nothing cached about the last
            # push still holds -- and the sink itself may be a different one.
            self._sink = sinks.get_sink()
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
        outcome, message = self._sink.revoke(view)
        # A target that has already forgotten this view is a successful revoke.
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
        """Forget everything cached about the retired link.

        This deliberately does **not** rotate the view itself. :func:`rotate`
        already did that on the GUI thread and handed the resulting URL to the
        user; rotating a second time here would mint a third secret, publish
        under it, and leave the user holding a link that 404s. That was a real
        bug -- both call sites used to rotate -- so if a future change moves
        the rotation back onto the worker, make sure exactly one of them does
        it and that the caller returns the secret that actually gets pushed.
        """
        if report_views.get_view(beamline, experiment) is None:
            return
        self._reset_subject_state()

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
        if not (beamline and experiment and self._sink.enabled()):
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
            ceiling = max_staleness_s(self._sink)
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

        # A target that carries its pixels inside the document (Google Docs
        # embeds them as data: URIs) has no separate figure phase at all.
        uploads = figures if self._sink.wants_figures() else []
        pending = [f for f in uploads if (view["view_id"], f) not in self._uploaded]
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

        if uploads and view["view_id"] not in self._indexed:
            self._index_figures(view)
            pending = [f for f in uploads if (view["view_id"], f) not in self._uploaded]

        # Figures first: a document that references an image the target cannot
        # serve would render broken for every reader until the next cycle.
        for rel in pending:
            if not self._push_figure(beamline, experiment, view, rel):
                return

        doc = sinks.Document(
            beamline=beamline,
            experiment=experiment,
            markdown=markdown,
            digest=digest,
            figures=figures,
            manifest=manifest,
            title=config.get("report_title") or "",
        )
        outcome, message = self._sink.push_doc(view, doc)
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

        outcome, message = self._sink.push_figure(view, rel, blob)
        if outcome == _OK:
            self._uploaded[(view["view_id"], rel)] = hashlib.sha256(blob).hexdigest()
            return True
        self._fail(message, outcome)
        return False

    def _index_figures(self, view: dict) -> None:
        """Learn what the target already holds, so a restart re-uploads nothing."""
        known = self._sink.known_figures(view)
        if known is None:
            return  # the listing failed; ask again next cycle
        for rel, digest in known.items():
            self._uploaded[(view["view_id"], rel)] = digest
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
    sink = sinks.get_sink()
    view = report_views.ensure_view(beamline, experiment)

    # Some targets must exist before they can be written to: the HTTP service
    # creates the view on first push, but a Google Doc has to be created and
    # shared before it has a URL to hand back. Done here, on the GUI thread,
    # rather than in the worker -- the user pressed a button and is waiting for
    # a link, so a failure belongs in front of them, not in a status chip.
    extra = {}
    if enabled():
        try:
            extra = sink.prepare_share(beamline, experiment, view) or {}
        except Exception:  # noqa: BLE001 -- a sink must not, but never trust it
            extra = {}
    if extra:
        view = report_views.update_view(beamline, experiment, extra) or view

    if enabled():
        worker = _get_worker()
        worker.post("subject", (beamline, experiment))
        worker.post("force", None)
    return sink.url(view)


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
    """Retire the current link and mint a new one for the same experiment.

    What "a new link" *is* depends on the target, and the panel's wording has
    to match: the HTTP service keeps one stored document and mints a new
    secret for it, while a Google Doc's URL is its file id, so the only way to
    kill a link is to publish into a new document and un-share the old one.
    """
    sink = sinks.get_sink()
    view = report_views.rotate_view(beamline, experiment)
    if view is None:
        return ""

    # Same reasoning as :func:`share`: whatever the target needs doing happens
    # here, synchronously, so the URL returned is the one that will actually be
    # published. The worker is only told to forget its cache.
    extra = {}
    if enabled():
        try:
            extra = sink.rotate_target(beamline, experiment, view) or {}
        except Exception:  # noqa: BLE001 -- a sink must not, but never trust it
            extra = {}
    if extra:
        view = report_views.update_view(beamline, experiment, extra) or view

    if enabled():
        worker = _get_worker()
        worker.post("rotate", (beamline, experiment))
        worker.post("force", None)
    return sink.url(view)


def view_url(beamline: str, experiment: str) -> str:
    return sinks.get_sink().url(report_views.get_view(beamline, experiment))


def reset() -> None:
    """Re-read configuration on the worker thread. Non-blocking.

    Called from ``ConfigDialog.accept`` -- guarded there, as ``qs_client.reset``
    is, so that saving Configuration on a machine with sync switched off never
    brings the worker thread into existence.
    """
    if _worker is not None:
        _worker.post("reset", None)
