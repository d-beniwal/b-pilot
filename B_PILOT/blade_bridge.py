"""Optional bridge to BLADE (``../blade``), the agent-driven beamline-automation library.

Guarded import, same contract as :mod:`autopilot_bridge`: B-PILOT must keep working with BLADE absent. The dependency arrow points B-PILOT -> BLADE only
(BLADE never imports B_PILOT). Everything BLADE does to the instrument is a Bluesky plan (``blade/plans.py``) that this module's :class:`KernelExecutor`
sends to the SAME console kernel the Runner tab uses, so there is one RunEngine and one IPython console.

BLADE is located via the ``BLADE_ROOT`` environment variable, else the sibling directory ``<workspace>/blade`` next to this checkout.
"""
from __future__ import annotations

import os
import sys
import threading

from PyQt5 import QtCore

from . import config, paths

_CANDIDATES = [os.environ.get("BLADE_ROOT"), os.path.join(os.path.dirname(paths.BUNDLE_DIR), "blade")]
BLADE_ROOT = next((p for p in _CANDIDATES if p and os.path.isfile(os.path.join(p, "blade", "__init__.py"))), None)
if BLADE_ROOT and BLADE_ROOT not in sys.path:
    sys.path.insert(0, BLADE_ROOT)

IMPORT_ERROR: str | None = None
try:
    if BLADE_ROOT is None:
        raise ImportError("no BLADE checkout found (set BLADE_ROOT, or place it next to b-pilot as ../blade)")
    from blade import config as blade_config  # noqa: F401
    from blade import executor as blade_executor  # noqa: F401
    from blade import registry as blade_registry  # noqa: F401
    AVAILABLE = True
except Exception as exc:  # noqa: BLE001 -- any failure just means "no BLADE tab content"
    blade_config = blade_executor = blade_registry = None  # type: ignore[assignment]
    AVAILABLE = False
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def diagnose() -> list[str]:
    """Why BLADE isn't available (empty once it is), for a click-triggered popup."""
    if AVAILABLE:
        return []
    return [
        "BLADE could not be loaded.\n"
        f"Looked for it at: {BLADE_ROOT or ' or '.join(p for p in _CANDIDATES if p) or '(no candidates)'}\n"
        "Clone it next to this checkout (../blade) or set BLADE_ROOT.",
        f"Import error: {IMPORT_ERROR}",
    ]


class _Job:
    def __init__(self, code: str, expressions: dict | None) -> None:
        self.code, self.expressions = code, dict(expressions or {})
        self.done = threading.Event()
        self.result: dict = {}
        self.error: str | None = None
        self.matched = False


class KernelExecutor(QtCore.QObject):
    """Runs BLADE's code in the console kernel shared with the Runner tab.

    ``run`` is called from BLADE's worker thread and blocks; the console is only ever touched on the GUI thread (a queued signal hands each job over).
    Each job is echoed as an ordinary visible console cell, so the operator sees every ``RE(blade_...)`` BLADE issues and the console history/transcript
    stay complete. Refuses to start while the console is busy with someone else's cell or while the persistent queue is running (so a queued plan can
    never run between two BLADE steps); an error or abort in the kernel fails the job and BLADE stops, it never retries.
    """

    _submit = QtCore.pyqtSignal(object)
    logged = QtCore.pyqtSignal(str, str)         # (kind, text) for the BLADE screen's command log: "cmd" | "ok" | "error"

    def __init__(self, console, parent=None) -> None:
        super().__init__(parent)
        self.console = console
        self._job: _Job | None = None
        self._cancelled = False
        self._submit.connect(self._start, QtCore.Qt.QueuedConnection)
        console.executing.connect(self._on_executing)
        console.executed.connect(self._on_executed)

    def cancel(self) -> None:
        """Stop issuing requests until the next routine starts (the Stop button); a request already in the kernel is ended by the kernel interrupt."""
        self._cancelled = True

    def reset_cancel(self) -> None:
        self._cancelled = False

    # -- worker-thread side ------------------------------------------------------------------------------------------
    def run(self, code, expressions=None, timeout=None):
        if self._cancelled and code.strip():
            raise blade_executor.ExecutorError("cancelled by the operator")
        job = _Job(code, expressions)
        self._submit.emit(job)
        if not job.done.wait(timeout or 120.0):
            self.logged.emit("error", "timed out waiting for the console")
            raise blade_executor.ExecutorError(f"timed out after {timeout or 120.0:g} s waiting for the console kernel")
        if job.error:
            raise blade_executor.ExecutorError(job.error)
        return job.result

    # -- GUI-thread side ---------------------------------------------------------------------------------------------
    def _fail(self, job: _Job, msg: str) -> None:
        job.error = msg
        self.logged.emit("error", msg)
        job.done.set()

    def _queue_running(self) -> bool:
        if config.get("queue_backend") != "native":
            return False
        try:
            from . import queue_store
            return queue_store.load(config.get("beamline")).get("state") == queue_store.S_RUNNING
        except Exception:  # noqa: BLE001 -- unreadable queue file: don't block, the console-busy check still applies
            return False

    def _start(self, job: _Job) -> None:
        if self._job is not None:
            return self._fail(job, "another BLADE request is still running")
        if not self.console.is_ready():
            return self._fail(job, "the console is not ready (Launch IPython / Attach first)")
        if self._queue_running():
            return self._fail(job, "the plan queue is running; pause it before using BLADE")
        if self.console.is_busy():
            return self._fail(job, "the console is busy with another command")
        self._job = job
        if job.code.strip():
            self.logged.emit("cmd", job.code.strip())
            self.console.run_code(job.code)
        else:
            self._query(job)

    def _on_executing(self, source) -> None:
        job = self._job
        if job is not None and str(source).strip() == job.code.strip():
            job.matched = True

    def _on_executed(self, msg) -> None:
        job = self._job
        if job is None or not job.matched:
            return
        content = msg.get("content", {})
        if content.get("status") != "ok":
            self._job = None
            return self._fail(job, f"{content.get('ename', 'error')}: {content.get('evalue', '')}".strip())
        self._query(job)

    def _query(self, job: _Job) -> None:
        if not job.expressions:
            return self._finish(job, {})

        def cb(values: dict) -> None:
            missing = [k for k, v in values.items() if v is None]
            if missing:
                self._job = None
                return self._fail(job, f"could not read back {missing} from the kernel")
            self._finish(job, values)
        self.console.query_values(job.expressions, cb)

    def _finish(self, job: _Job, values: dict) -> None:
        self._job = None
        job.result = values
        self.logged.emit("ok", "done" if not values else f"-> {values}")
        job.done.set()
