"""Run controls shown below the console: Stop run + recovery actions.

Maps the Bluesky RunEngine interrupt/recovery model onto buttons:

* **Stop run** — *click* = deferred pause (one Ctrl+C, stops at the next
  checkpoint); *press-and-hold >1 s* = immediate pause (double Ctrl+C).
  Console-kernel target: delivered as SIGINT(s) via
  :func:`kernel_session.interrupt`.
* After a pause, the RunEngine's **four** recovery options appear as temporary
  buttons — ``RE.resume()`` / ``RE.stop()`` / ``RE.abort()`` / ``RE.halt()`` —
  sent to the console.  They hide again once one is chosen.  Stop/Abort/Halt
  (never Resume) are always followed by ``RE(abort_cleanup())``, the active
  profile's cleanup shortcut, if one is configured.

When ``config.get("queue_backend") == "qs"``, a plan dispatched through the
queue server (:mod:`qs_client`) is a second, independent RunEngine that can
be "the thing currently running" — every action above then picks its
**target** per click (:meth:`RunControlBar._active_target`) instead of
assuming the console kernel: Stop maps to :func:`qs_client.re_pause`, the
four recovery buttons to :func:`qs_client.re_resume`/`re_stop`/`re_abort`/
`re_halt`. ``abort_cleanup()`` (Python source sent to the kernel) has no QS
equivalent — a documented, deliberate gap for QS-dispatched plans; console-
dispatched (interactive Run) plans are unaffected. For the native backend
(the default), none of this QS polling ever runs — see :meth:`__init__`.

Shut down kernel now lives in the top toolbar (``main_window.py``), set apart
from the other controls there — :meth:`RunControlBar.hide_recovery` is called
from that button's handler so an open recovery bar doesn't linger.
"""
from __future__ import annotations

import os

from PyQt5 import QtCore
from PyQt5 import QtWidgets

from . import config
from . import kernel_session as ks
from . import paths as _paths
from . import plan_parser as P
from . import qs_client
from . import style as S

_HOLD_MS = 1000  # press-and-hold threshold for a hard (immediate) halt

# Recovery commands that end the run (Resume is excluded -- it isn't a
# cleanup point) -- each is always followed by RE(abort_cleanup()), if the
# active profile defines one. Console-kernel target only (see _recover).
_CLEANUP_COMMANDS = {"RE.stop()", "RE.abort()", "RE.halt()"}

# QS manager_state values (bluesky_queueserver.manager.manager.MState) that
# mean a queued plan is actively running or paused mid-plan. Only consulted
# when the QS backend is active (see __init__).
_QS_ACTIVE_STATES = {"executing_queue", "executing_task", "starting_queue", "paused"}

# Same four recovery actions, QS-API target -- keyed by the same command
# strings the recovery buttons were built with (see _build_ui). Only ever
# invoked when the QS backend is active (see _recover).
_QS_RECOVERY = {
    "RE.resume()": qs_client.re_resume,
    "RE.stop()": qs_client.re_stop,
    "RE.abort()": qs_client.re_abort,
    "RE.halt()": qs_client.re_halt,
}


def _abort_cleanup_command() -> str | None:
    """`from <module> import abort_cleanup` + `RE(abort_cleanup())`,
    resolved against the active profile's `switch_to_search_paths` (the same
    per-beamline shortcuts file that already holds its `switch_to_*` plans).
    `abort_cleanup` is deliberately excluded from that module's `__all__` (so
    it never shows up as a switch-to shortcut), which is why this looks it up
    with `plan_parser.file_defines_function` instead of `find_plan_specs`.
    Every beamline's `abort_cleanup` is a plan (contains `yield from`), so a
    bare `abort_cleanup()` call only builds a generator and runs nothing --
    it must go through `RE(...)` like any other plan.
    Returns None if no configured search path defines it.
    """
    import_root = config.get("import_root")
    for rel_path in config.get("switch_to_search_paths") or []:
        abs_path = rel_path if os.path.isabs(rel_path) else os.path.join(_paths.BLUESKY_ROOT, rel_path)
        if P.file_defines_function(abs_path, "abort_cleanup"):
            module = P.file_to_module(abs_path, import_root)
            return f"from {module} import abort_cleanup\nRE(abort_cleanup())"
    return None


def _stop_qss() -> str:
    """Build the Stop-run button's QSS from the live theme's error color."""
    border = S.darken(S.ERROR, 130)
    return (
        f"QPushButton{{background:{S.ERROR};color:white;font-weight:bold;"
        f"border:1px solid {border};border-radius:{S.px(4)}px;padding:{S.px(5)}px {S.px(12)}px;}}"
        f"QPushButton:hover{{background:{S.darken(S.ERROR, 110)};}}"
        f"QPushButton:pressed{{background:{border};}}"
        f"QPushButton:disabled{{background:{S.BUTTON_DISABLED_BG};color:{S.DISABLED_TEXT};"
        f"border-color:{S.BUTTON_DISABLED_BORDER};}}"
    )


class RunControlBar(QtWidgets.QWidget):
    """Stop-run (soft/hard), RunEngine recovery actions, and Shutdown kernel."""

    def __init__(self, console, parent=None) -> None:
        """`console` drives the RE interrupt/recovery commands."""
        super().__init__(parent)
        self._console = console
        self._held = False
        self._target: str | None = None  # "kernel" or "qs", set when a pause starts
        # Only ever True for the QS backend (config.get("queue_backend") ==
        # "qs") -- gates every qs_client call and the QS poll timer below,
        # so the native backend (the default) never creates qs_client's
        # background worker thread or makes a single connection attempt.
        self._qs_enabled = config.get("queue_backend") == "qs"
        self._qs_active = False
        self._seen_qs_error_seq = 0

        self._hold_timer = QtCore.QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setInterval(_HOLD_MS)
        self._hold_timer.timeout.connect(self._on_hold_elapsed)

        self._build_ui()
        self.set_console_ready(False)

        if self._qs_enabled:
            # Independent light poll of the QS queue's RE state, so Stop/
            # recovery work for a queued plan even though it runs in QS's
            # own environment, not this console's kernel.
            self._qs_timer = QtCore.QTimer(self)
            self._qs_timer.setInterval(750)
            self._qs_timer.timeout.connect(self._poll_qs)
            self._qs_timer.start()

    # ── UI ──────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(4)

        self._row = row = QtWidgets.QHBoxLayout()
        self._stop_btn = QtWidgets.QPushButton("■  Stop run")
        self._stop_btn.setStyleSheet(_stop_qss())
        self._stop_btn.setMinimumHeight(S.px(30))
        self._stop_btn.setToolTip(
            "Click = pause at the next checkpoint (Ctrl+C once).\n"
            "Press and hold >1 s = pause immediately (Ctrl+C twice)."
        )
        self._stop_btn.pressed.connect(self._on_pressed)
        self._stop_btn.released.connect(self._on_released)
        row.addWidget(self._stop_btn)
        row.addStretch(1)
        outer.addLayout(row)

        # Own row below the buttons so a long status message never widens the
        # button row (which would otherwise shove the IPython console splitter).
        self._hint = QtWidgets.QLabel("")
        self._hint.setStyleSheet(f"color: {S.MUTED};")
        outer.addWidget(self._hint)

        # Temporary recovery actions (hidden until a pause is requested).
        self._recovery = QtWidgets.QFrame()
        self._recovery.setObjectName("toolbar")
        rlay = QtWidgets.QHBoxLayout(self._recovery)
        rlay.setContentsMargins(8, 4, 8, 4)
        rlay.setSpacing(6)
        rlay.addWidget(QtWidgets.QLabel("Run paused →"))
        self._resume_btn = self._recovery_btn(
            "Resume", "RE.resume()", "Continue the plan from where it paused."
        )
        self._stop_re_btn = self._recovery_btn(
            "Stop", "RE.stop()",
            "End now, run cleanup, mark the run SUCCESSFUL."
        )
        self._abort_btn = self._recovery_btn(
            "Abort", "RE.abort()",
            "End now, run cleanup, mark the run ABORTED."
        )
        self._halt_btn = self._recovery_btn(
            "Halt", "RE.halt()",
            "End now WITHOUT running cleanup handlers."
        )
        for b in (self._resume_btn, self._stop_re_btn, self._abort_btn, self._halt_btn):
            rlay.addWidget(b)
        rlay.addStretch(1)
        self._recovery.setVisible(False)
        outer.addWidget(self._recovery)

    def add_trailing_widget(self, widget: QtWidgets.QWidget) -> None:
        """Append `widget` to the far end of the top button row.

        Used by the main window to place the BEAMMODE/SHUTTERMODE toggle bar in
        the same row as Stop run, rather than as its own row underneath.
        """
        self._row.addWidget(widget)

    def _recovery_btn(self, label: str, command: str, tip: str) -> QtWidgets.QPushButton:
        btn = QtWidgets.QPushButton(label)
        btn.setToolTip(f"{tip}\n\nRuns: {command}")
        btn.clicked.connect(lambda: self._recover(command))
        return btn

    # ── Enable/disable with the console / QS ─────────────────────────────────────

    def set_console_ready(self, ready: bool) -> None:
        """Enable Stop when a kernel is connected (or, for the QS backend,
        when a queued plan is actively running -- see :meth:`_active_target`)."""
        self._console_ready = ready
        self._update_stop_enabled()
        if not ready and self._target != "qs":
            self._hide_recovery()

    def _poll_qs(self) -> None:
        status = qs_client.status() or {}
        active = status.get("manager_state") in _QS_ACTIVE_STATES
        if active != self._qs_active:
            self._qs_active = active
            self._update_stop_enabled()
        # Surface a failed QS action (re_pause/re_resume/re_stop/re_abort/
        # re_halt) -- these are fire-and-forget over the network same as
        # queue_panel's item_add, so a rejected/undeliverable call used to
        # look identical to one that actually reached the RE Manager.
        err = qs_client.last_action_error()
        if err is not None and err[0] != self._seen_qs_error_seq:
            self._seen_qs_error_seq = err[0]
            self._hint.setText(f"Queue server error: {err[2]}")

    def _update_stop_enabled(self) -> None:
        self._stop_btn.setEnabled(self._console_ready or self._qs_active)

    # ── Target selection (QS backend only; console-only otherwise) ──────────────

    def _active_target(self) -> str | None:
        """Which backend a Stop/recovery click should act on right now: the
        console kernel (if it's running something) takes priority since it
        reflects an action this GUI itself just dispatched interactively;
        otherwise QS, if the QS backend is active and it's actively
        executing/paused a queued plan; else `None` (nothing to stop)."""
        if self._console.is_running():
            return "kernel"
        if self._qs_enabled and self._qs_active:
            return "qs"
        return None

    # ── Stop run: click = soft, hold = hard ─────────────────────────────────────

    def _on_pressed(self) -> None:
        if self._active_target() is None:
            return
        self._held = False
        self._hold_timer.start()

    def _on_hold_elapsed(self) -> None:
        # Held long enough → immediate (hard) pause.
        self._held = True
        self._interrupt(hard=True)

    def _on_released(self) -> None:
        self._hold_timer.stop()
        if self._held:
            return  # hard halt already fired on hold
        if self._active_target() is not None:
            self._interrupt(hard=False)

    def _interrupt(self, hard: bool) -> None:
        target = self._active_target()
        if target == "kernel":
            # ks.interrupt() is a local signal, not a network round trip --
            # still synchronous/instant, so its success/failure is checked
            # directly (unchanged from before the QS target existed).
            if not ks.interrupt(config.get("beamline"), hard=hard):
                self._hint.setText("Could not signal the kernel.")
                return
        elif target == "qs":
            # qs_client's action calls are fire-and-forget (see its module
            # docstring -- they run on a background thread so a slow/
            # unreachable queue server never blocks the GUI); there is no
            # synchronous success/failure to check here.
            qs_client.re_pause(option="immediate" if hard else "deferred")
        else:
            return
        self._target = target
        self._show_recovery(immediate=hard)

    # ── Recovery ────────────────────────────────────────────────────────────────

    def _show_recovery(self, immediate: bool) -> None:
        self._hint.setText(
            "Pausing immediately… choose an action once paused."
            if immediate else
            "Pausing at next checkpoint… choose an action once paused."
        )
        self._recovery.setVisible(True)

    def _hide_recovery(self) -> None:
        self._recovery.setVisible(False)
        self._hint.setText("")
        self._target = None

    def hide_recovery(self) -> None:
        """Hide the pause-recovery bar. Called by the main window's toolbar
        Shutdown button before it tears down the kernel, so a lingering
        Resume/Stop/Abort/Halt bar doesn't survive a shutdown."""
        self._hide_recovery()

    def _recover(self, command: str) -> None:
        # `command` is the console-kernel form ("RE.resume()" etc.) chosen at
        # button-build time -- translated to the QS API call when that's the
        # active target (QS has no abort_cleanup() equivalent, so that step
        # is skipped for the QS target -- see module docstring).
        target = self._target
        if target == "qs" and self._qs_enabled:
            _QS_RECOVERY.get(command, lambda: None)()
        elif self._console.is_running():
            if command in _CLEANUP_COMMANDS:
                cleanup = _abort_cleanup_command()
                if cleanup:
                    command = f"{command}\n{cleanup}"
            self._console.run_code(command)
        self._hide_recovery()
