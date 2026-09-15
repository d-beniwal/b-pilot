"""Where a shared report is published to, and the contract every target keeps.

:mod:`report_sync` owns *when* to publish -- the poll, the debounce, the
SHA-256 short-circuit, the backoff, the status the panel shows. This module
owns *where*, and nothing else. The split exists because the two halves have
completely different reasons to change: the scheduling half is generic and
load-bearing across every target, while the destination half has two
independent shapes -- a Google Doc for a workstation with internet, and a
shared-folder + relay for one without.

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

from dataclasses import dataclass

from . import config

SCHEMA = 1

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


# ── selection ────────────────────────────────────────────────────────────────

_SINKS: dict = {}
_gdocs_reason = "not checked yet"


def _register_builtin() -> None:
    # The outbox sink is pure stdlib, so it is always available -- which is
    # the point: it is the universal fallback (a machine that can install
    # nothing still publishes) and the backend for a beamline with no route
    # to the internet at all.
    if "outbox" not in _SINKS:
        from .report_outbox import FileSink

        _SINKS[FileSink.name] = FileSink

    # The Google sink is optional: its client libraries are not part of the
    # pinned beamline environment, and B-PILOT must run everywhere they are
    # absent. A guarded import here (the `autopilot_bridge` pattern) means the
    # backend is simply not offered rather than the app failing to start.
    #
    # The module always imports; it is `available()` that reports whether the
    # libraries did. That split is what lets the Configuration page say *why*
    # the backend is missing instead of silently omitting it.
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
    """The configured backend, falling back to the outbox sink if unavailable.

    Falling back rather than erroring matters on a workstation that pulls a
    profile selecting ``gdocs`` without the client libraries installed: the
    report keeps publishing (to the shared outbox) instead of silently going
    dark, and the Configuration page says why. ``outbox`` is the fallback
    target rather than ``gdocs`` itself precisely because it is the one sink
    with no optional dependency to be missing.
    """
    _register_builtin()
    from .report_outbox import FileSink

    want = (config.get("report_sync_backend") or FileSink.name).strip()
    return want if want in _SINKS else FileSink.name


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
