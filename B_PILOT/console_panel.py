"""Embedded live IPython console (out-of-process kernel) as a Qt panel.

Wraps qtconsole's :class:`RichJupyterWidget` on a kernel started in a *separate*
process (via :class:`QtKernelManager`), so:

* the GUI survives a plan crash / kernel death,
* the RunEngine (when the user loads it) lives in its own process,
* the kernel uses the SAME interpreter as the GUI, so ``import instrument``
  resolves.

**Persistence & reattach.** The kernel is a real separate process, so it can
outlive the GUI.  :meth:`start` records the kernel's *connection file* (and
saves it to config); on GUI close the session is **detached, not killed** (see
:meth:`close_session`, gated by the ``keep_kernel_on_exit`` config).  A later
GUI instance calls :meth:`attach` with that connection file to reconnect to the
same running kernel — including one with a plan still running in it.

The kernel is NOT started until :meth:`start` (or :meth:`attach`) is called.
On a fresh :meth:`start`, the configured startup command(s) (see
:meth:`run_startup_commands`) run automatically once the kernel connects —
this DOES touch hardware/EPICS if configured to.  Since 2026-09-03 the shipped
profiles configure none: the instrument import moved into the starter scripts
(``starter_scripts/*.sh``, ``--IPKernelApp.exec_lines``), so it happens as the
kernel initializes rather than as a console cell.  :meth:`attach` never runs
them either way: a reattached kernel already went through this once, under its
own original launch.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys

# qtconsole imports Qt through qtpy; pin the binding to PyQt5 before that happens.
os.environ.setdefault("QT_API", "pyqt5")

from PyQt5 import QtCore  # noqa: E402
from PyQt5 import QtGui  # noqa: E402
from PyQt5 import QtWidgets  # noqa: E402
from qtconsole.ansi_code_processor import AnsiCodeProcessor  # noqa: E402
from qtconsole.manager import QtKernelManager  # noqa: E402
from qtconsole.rich_jupyter_widget import RichJupyterWidget  # noqa: E402

from . import config  # noqa: E402
from . import det_startup_state  # noqa: E402
from . import experiment_history  # noqa: E402
from . import kernel_session as ks  # noqa: E402
from . import paths  # noqa: E402
from . import style as S  # noqa: E402

# ── Output line-banding (separates a command's output from the command itself) ──
# qtconsole's JupyterWidget has no public per-line-background hook (its
# `style_sheet` trait only themes the In[]/Out[] prompt HTML spans and
# pygments syntax colors -- the actual stdout/result/traceback text is
# inserted as bare plain text). The only way to get a real full-width
# highlighted band behind output lines is a QTextBlockFormat background on
# the widget's own internal QTextEdit (`_control`) -- see
# _rescan_output_bands below. Fixed (not theme-derived): the console is
# hardcoded to qtconsole's "lightbg" style regardless of the app's
# Light/Dark/Slate theme (see _wire_widget's set_default_style call).
_OUTPUT_BAND_BG = QtGui.QColor("#eef2f8")
_IN_PROMPT_RE = re.compile(r"^In \[\s*\d+\]:")
_CONT_RE = re.compile(r"^\s*\.\.\.:")


def _rescan_output_bands(control) -> None:
    """Shade every "output" line of `control` (a qtconsole internal QTextEdit)
    with :data:`_OUTPUT_BAND_BG`; leave "In [n]:"/continuation input lines and
    the pre-first-input banner unstyled.

    Reaches into qtconsole's private `_control`/document internals, so the
    whole pass is guarded: a qtconsole version mismatch just silently
    disables the highlighting rather than crashing the console.
    """
    try:
        doc = control.document()
        was_modified = doc.isModified()
        undo_was_enabled = doc.isUndoRedoEnabled()
        doc.blockSignals(True)          # formatting-only edits must not recurse
        doc.setUndoRedoEnabled(False)   # ...and must not pollute the undo stack
        try:
            state = "banner"   # "banner" (unstyled) -> "input" -> "output"
            block = doc.begin()
            while block.isValid():
                text = block.text()
                if _IN_PROMPT_RE.match(text):
                    state = "input"
                elif state == "input" and _CONT_RE.match(text):
                    pass   # continuation line of the same input cell
                elif state != "banner":
                    state = "output"
                want_band = state == "output"
                fmt = block.blockFormat()
                has_band = fmt.background().style() != QtCore.Qt.NoBrush
                if want_band != has_band:
                    cursor = QtGui.QTextCursor(block)
                    fmt.setBackground(QtGui.QBrush(_OUTPUT_BAND_BG) if want_band else QtGui.QBrush())
                    cursor.setBlockFormat(fmt)
                block = block.next()
        finally:
            doc.setUndoRedoEnabled(undo_was_enabled)
            doc.blockSignals(False)
            doc.setModified(was_modified)
    except Exception:  # noqa: BLE001
        pass

# Fallback startup command(s) if config is unavailable -- mirrors
# config.DEFAULTS["bluesky_startup"], which is now EMPTY: since 2026-09-03 the
# instrument import lives in the profile's starter script (starter_scripts/*.sh,
# via --IPKernelApp.exec_lines) instead of being sent as a console cell, so
# there is nothing left to run by default. Kept as a named constant, not
# inlined, so a deployment that still wants a console-side startup command has
# one obvious place to look. Anything put here CONNECTS TO HARDWARE/EPICS when
# it is an instrument import, and only ever on an explicit fresh Launch
# IPython, never on attach.
BLUESKY_STARTUP = ""

_PLACEHOLDER = (
    "IPython session not started.\n\n"
    "Set a working directory above and click  ▶ Launch IPython."
)


class ConsolePanel(QtWidgets.QWidget):
    """A live IPython console backed by an out-of-process ipykernel."""

    started = QtCore.pyqtSignal()        # emitted once the kernel + widget are up
    ready = QtCore.pyqtSignal()          # kernel handshake done (safe to execute)
    executing = QtCore.pyqtSignal(object)  # a cell started (source)
    executed = QtCore.pyqtSignal(object)   # a cell finished (execute_reply msg)
    attach_failed = QtCore.pyqtSignal(str)  # attach() could not connect (reason)
    launch_blocked = QtCore.pyqtSignal(object)  # start() refused: kernel already running
    # A command actually finished executing SUCCESSFULLY in the kernel --
    # from ANY client (this GUI's own console, another attached GUI, or the
    # detached queue runner), unlike `executing`/`executed` which only fire
    # for this widget's own execute() calls. Emitted with the raw source
    # text once the cell goes idle on iopub WITHOUT an intervening `error`
    # message (sent to every attached client) -- a command that raises never
    # fires this.
    code_executed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None, font_size: int = 11) -> None:
        """Build the placeholder view; the kernel starts later via :meth:`start`."""
        super().__init__(parent)
        self.kernel_manager: QtKernelManager | None = None
        self.kernel_client = None
        self.jupyter_widget: RichJupyterWidget | None = None
        self._font_size = font_size
        self._down = False
        self._busy = False
        self._ready = False
        self._attached = False               # True if reconnected to an existing kernel
        self._connection_file: str | None = None
        self._proc = None                    # Popen of a kernel we started (else None)
        self._experiment: str | None = None  # experiment this session's history is filed under
        # msg_id -> (exprs, callback) for in-flight silent status queries (see
        # query_values); dispatched from _on_shell_msg alongside the existing
        # kernel_info_reply handshake handling.
        self._pending_queries: dict[str, tuple[dict, object]] = {}
        # msg_id -> source, for visible executions awaiting completion (see
        # _on_iopub_msg / code_executed) -- and the subset of those msg_ids
        # that raised, so a failed command is never reported as having run.
        self._pending_execs: dict[str, str] = {}
        self._errored_execs: set[str] = set()
        # parent msg_id -> {"cursor": QTextCursor, "processor": AnsiCodeProcessor,
        # "lines": [...], "row": int, "col": int} for an in-progress FOREIGN
        # progress-bar redraw (queue_runner.py's dispatch) currently being
        # collapsed into a single overwriting block -- see
        # _handle_foreign_progress_stream.
        self._progress_bars: dict[str, dict] = {}

        self._stack = QtWidgets.QStackedWidget()
        self._placeholder = QtWidgets.QLabel(_PLACEHOLDER)
        self._placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet(f"color: {S.MUTED}; padding: 24px;")
        self._stack.addWidget(self._placeholder)   # index 0

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._stack)

    # ── Lifecycle ───────────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        """True once a kernel (started OR attached) is connected and not torn down.

        Does not depend on ``kernel_manager.has_kernel`` — for an *attached*
        kernel this manager did not spawn the process, so ``has_kernel`` is False
        even though the session is live.
        """
        return (
            self.jupyter_widget is not None
            and not self._down
            and self.kernel_client is not None
        )

    def is_attached(self) -> bool:
        """True if this panel is reconnected to a pre-existing kernel."""
        return self._attached

    @property
    def connection_file(self) -> str | None:
        """Path to the current kernel's connection file (use it to reattach)."""
        return self._connection_file

    def start(self, cwd: str | None = None) -> None:
        """Ensure the beamline's ONE kernel is running, then connect to it.

        Process lifecycle + single-instance are delegated to
        :mod:`kernel_session` (which hosts the kernel in a named ``screen``
        session at a fixed per-beamline connection file, so it survives the GUI
        and is reattachable).  If a kernel is already running this emits
        :attr:`launch_blocked` (with its details) instead of starting a second
        one — the caller should offer to *attach*.
        """
        if self.jupyter_widget is not None:
            return  # this GUI already has a connection

        beamline = config.get("beamline")
        status, info = ks.launch(beamline, cwd or None)
        if status == "already_running":
            self.launch_blocked.emit(info or {})
            return
        if status != "started":
            self.attach_failed.emit(
                "Could not start kernel: " + str((info or {}).get("error", "unknown"))
            )
            return

        cf = info["connection_file"]
        self._attached = False
        # Fresh kernel: nothing has been through det_startup yet.
        det_startup_state.clear(beamline)
        # The experiment this launch was for -- frozen into the sidecar by
        # kernel_session.launch() at the moment it actually started the
        # kernel (see its docstring for why that's more reliable than a live
        # config read here).
        self._experiment = info.get("experiment") or ""
        experiment_history.append_entry(beamline, self._experiment, "marker", "Kernel launched")
        # Start the detached recorder so the full session is captured into
        # this experiment's persistent history from the first line (survives
        # GUI restarts; readable while busy).
        self._start_recorder(cf, beamline, self._experiment)
        self._start_queue_runner()
        self._connect(cf)

    def default_connection_file(self) -> str:
        """The fixed per-beamline connection file (the default attach target)."""
        return ks.connection_file(config.get("beamline"))

    def attach(self, connection_file: str | None = None) -> bool:
        """Reconnect to the ALREADY-RUNNING kernel (default: this beamline's).

        The kernel keeps all of its state — including a plan still running in it;
        if it is mid-plan the readiness handshake completes once that plan
        finishes.  Returns True if the connection was set up, else emits
        :attr:`attach_failed` and returns False.
        """
        if self.jupyter_widget is not None:
            return False  # already connected

        cf = connection_file or self.default_connection_file()
        if not cf or not os.path.exists(cf):
            self.attach_failed.emit(
                f"No running kernel found — connection file missing:\n"
                f"{cf or '(none)'}"
            )
            return False
        try:  # a valid connection file is JSON with ports + key
            with open(cf, encoding="utf-8") as fh:
                json.load(fh)
        except Exception as exc:  # noqa: BLE001
            self.attach_failed.emit(f"Invalid connection file:\n{cf}\n\n{exc}")
            return False

        self._proc = None       # we did not spawn this one
        self._attached = True
        beamline = config.get("beamline")
        # Reattaching to a kernel B-PILOT didn't just launch: we have no
        # reliable record of what's already been started up in it, so treat
        # every detector as unstarted (worst case: one harmless redundant
        # det_startup call, since it's idempotent).
        det_startup_state.clear(beamline)
        self._experiment = self._resolve_attach_experiment(beamline, cf)
        history_file = experiment_history.history_path(beamline, self._experiment)
        # A recorder is still running for this kernel iff its history file
        # already exists -- the kernel is a per-beamline singleton, so
        # "recorder alive" and "kernel alive" coincide (the recorder only
        # exits once the kernel's heartbeat dies). Only spawn a new one if
        # there's no evidence one is already appending (e.g. a kernel not
        # started by our GUI at all).
        recorder_already_running = os.path.exists(history_file)
        experiment_history.append_entry(beamline, self._experiment, "marker", "Kernel attached")
        if not recorder_already_running:
            self._start_recorder(cf, beamline, self._experiment)
        self._start_queue_runner()
        return self._connect(cf)

    @staticmethod
    def _resolve_attach_experiment(beamline: str, cf: str) -> str:
        """Best-effort experiment name for an attach, resolved synchronously.

        Trusts the beamline's kernel-launch sidecar (see kernel_session.launch)
        only if it actually describes THIS connection file -- guarding against
        a stale/foreign sidecar (e.g. a hand-picked connection file from
        elsewhere). Falls back to :data:`experiment_history.UNKNOWN_EXPERIMENT`
        rather than the slower async DM_EXP kernel-probe main_window.py uses
        for the experiment *banner* -- that probe stays the authoritative
        source for the banner, unchanged; this is only about picking which
        history file to append into, and covers the common case (reattaching
        to this beamline's own kernel after a GUI restart) synchronously and
        correctly.
        """
        info = ks.read_info(beamline) or {}
        if info.get("connection_file") == cf:
            experiment = info.get("experiment")
            if experiment:
                return experiment
        return experiment_history.UNKNOWN_EXPERIMENT

    # ── shared setup (start + attach) ────────────────────────────────────────────

    def _connect(self, cf: str) -> bool:
        """Wire a QtKernelClient to the connection file `cf` and show the widget."""
        km = QtKernelManager(connection_file=cf)
        try:
            km.load_connection_file()
            kc = km.client()
        except Exception as exc:  # noqa: BLE001
            self.attach_failed.emit(f"Could not connect to kernel:\n{exc}")
            return False

        # QtKernelClient channels need their ioloop, which start_channels() sets
        # up, before the widget can bind to them — so start channels first.
        try:
            kc.start_channels()
        except Exception as exc:  # noqa: BLE001
            self.attach_failed.emit(f"Could not connect to kernel:\n{exc}")
            return False

        self.kernel_manager = km
        self.kernel_client = kc
        self._connection_file = cf
        self._remember_connection_file(cf)

        kc.iopub_channel.message_received.connect(self._on_iopub_msg)
        self._begin_handshake()
        self._wire_widget(km, kc)
        self._down = False
        if self._attached:
            # qtconsole paints its banner/prompt only after a shell round-trip
            # (kernel_info + a silent execute for the prompt number).  A reattached
            # kernel that is BUSY (mid-plan) has a blocked shell channel, so it
            # would show a BLANK panel until it goes idle.  Write an explanatory
            # notice now; the real banner/prompt replaces it once the kernel frees.
            self._show_attach_notice()
        self.started.emit()
        return True

    def _show_attach_notice(self) -> None:
        """Write a 'reattached' notice into the widget so it is never blank."""
        jw = self.jupyter_widget
        if jw is None:
            return
        try:
            jw._append_plain_text(
                "[Reattached to a running kernel.\n"
                " If this panel looks idle, the kernel is BUSY running something;\n"
                " its output and the prompt will appear once it is free.]\n\n"
            )
        except Exception:  # noqa: BLE001
            pass

    def is_alive(self) -> bool:
        """Best-effort kernel liveness via the heartbeat (True even when BUSY).

        Lets callers tell an alive-but-busy reattached kernel (heartbeat beats
        while a plan runs) from one that has actually shut down.
        """
        kc = self.kernel_client
        if kc is None:
            return False
        try:
            return bool(kc.is_alive())
        except Exception:  # noqa: BLE001
            return False

    # ── Persistent per-experiment history recorder ───────────────────────────────

    @property
    def experiment(self) -> str | None:
        """Experiment name this session's history is filed under (see
        :mod:`experiment_history`), or ``None`` before a session starts."""
        return self._experiment

    @staticmethod
    def _start_recorder(cf: str, beamline: str, experiment: str) -> None:
        """Spawn the detached IOPub->history recorder for this kernel (best effort).

        Run as a module (not a bare script) since it needs its package for
        ``from . import experiment_history`` -- same pattern as
        :meth:`_start_queue_runner` below.
        """
        import subprocess

        try:
            subprocess.Popen(
                [sys.executable, "-m", "B_PILOT.session_recorder", cf, beamline, experiment],
                cwd=paths.PKG_PARENT,
                start_new_session=True,   # independent of the GUI, like the kernel
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _start_queue_runner() -> None:
        """Spawn the detached plan-queue runner for this beamline (best effort).

        The runner is a singleton (flock) — extra launches self-exit — so it is
        safe to call on every start/attach.  Run as a module so its relative
        imports resolve (cwd = the package parent).
        """
        import subprocess

        pkg_parent = paths.PKG_PARENT
        try:
            subprocess.Popen(
                [sys.executable, "-m", "B_PILOT.queue_runner", config.get("beamline")],
                cwd=pkg_parent,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            pass

    def _begin_handshake(self) -> None:
        """Send kernel_info; mark ready when its reply arrives (dispatch gate).

        On a reattached *busy* kernel the reply only returns once the running
        cell finishes — which is precisely when dispatching is safe again — so
        the absence of a reply is never treated as a failure.
        """
        self._ready = False
        self.kernel_client.shell_channel.message_received.connect(self._on_shell_msg)
        self.kernel_client.kernel_info()

    def _wire_widget(self, km, kc) -> None:
        """Build the RichJupyterWidget on (km, kc) and show it."""
        # Drop any previous band-highlight timer first -- if left running it
        # would eventually fire against this-about-to-be-replaced jw's
        # (possibly already deleted) _control, a real crash risk, not just
        # cleanup hygiene.
        if getattr(self, "_band_timer", None) is not None:
            self._band_timer.stop()

        jw = RichJupyterWidget()
        jw.kernel_manager = km
        jw.kernel_client = kc
        jw.confirm_restart = False

        # Show input/output from OTHER clients too (the detached queue runner,
        # or another attached GUI) so queued plans appear with their In [N]:
        # prompt + echoed command, exactly like manually-typed cells.
        jw.include_other_output = True
        try:
            jw.other_output_prefix = ""   # no "[remote] " prefix -> looks native
        except Exception:  # noqa: BLE001  (older qtconsole without the trait)
            pass

        # include_other_output above means a queue_runner.py-dispatched plan's
        # progress bar (bluesky's TerminalProgressBar, which redraws with
        # \r + \n + an ANSI cursor-up escape, exactly like a real terminal)
        # gets displayed via qtconsole's "foreign client" append path, which
        # resets its insertion cursor to just-before-our-idle-prompt on EVERY
        # incoming chunk and disables \r's overwrite logic whenever that reset
        # doesn't land on a bare newline -- see _handle_foreign_progress_stream
        # for the full explanation. Net effect without this override: every
        # progress-bar tick from a queued plan appends as a NEW line instead
        # of overwriting the previous one. This bound-method override collapses
        # exactly that case (foreign + contains "\r") ourselves; every other
        # message shape (this widget's own execution, or foreign output with
        # no "\r") falls straight through to qtconsole's original handling,
        # unchanged.
        self._progress_bars.clear()  # drop any cursors into the old jw's document
        orig_handle_stream = jw._handle_stream

        def _handle_stream_override(msg):
            try:
                content = msg.get("content", {}) or {}
                text = content.get("text", "")
                parent_id = msg.get("parent_header", {}).get("msg_id")
                if "\r" not in text or jw.from_here(msg) or not parent_id:
                    orig_handle_stream(msg)
                    return
                self._handle_foreign_progress_stream(jw, parent_id, msg, text)
            except Exception:  # noqa: BLE001 -- never let a malformed message break the console
                try:
                    orig_handle_stream(msg)
                except Exception:  # noqa: BLE001
                    pass

        jw._handle_stream = _handle_stream_override

        # Track execution lifecycle so the scheduler can chain queued plans and
        # so we know when the kernel is busy.
        jw.executing.connect(self._on_jw_executing)
        jw.executed.connect(self._on_jw_executed)

        # Light styling to match the light Qt theme.
        jw.gui_completion = "droplist"
        jw.set_default_style("lightbg")
        jw.syntax_style = "default"
        jw.font_family = S.MONO_FAMILIES[0]
        jw.font_size = self._font_size
        jw.reset_font()

        # Shade output lines (see _rescan_output_bands) so they read as
        # visually distinct from the commands that produced them. Debounced
        # (0 ms single-shot) so the several `contentsChange` signals fired
        # per message coalesce into one rescan per event-loop tick.
        self._band_timer = QtCore.QTimer(self)
        self._band_timer.setSingleShot(True)
        self._band_timer.setInterval(0)
        self._band_timer.timeout.connect(lambda: _rescan_output_bands(jw._control))
        try:
            jw._control.document().contentsChange.connect(lambda *a: self._band_timer.start())
        except Exception:  # noqa: BLE001  (older/different qtconsole internals)
            pass

        self.jupyter_widget = jw
        self._stack.addWidget(jw)
        self._stack.setCurrentWidget(jw)

    # ── Foreign progress-bar collapsing (see _wire_widget's override) ───────────

    def _handle_foreign_progress_stream(self, jw, parent_id: str, msg, text: str) -> None:
        """Collapse one \\r-bearing stream chunk from a FOREIGN client (a
        queue_runner.py-dispatched plan's progress bar) into a single,
        in-place-overwriting block, instead of qtconsole's default foreign-
        output handling which would append it as a new line every time.
        """
        if not jw.include_output(msg):
            return
        jw.flush_clearoutput()
        state = self._progress_bars.get(parent_id)
        if state is None:
            state = self._new_progress_region(jw)
            self._progress_bars[parent_id] = state
        self._feed_progress_text(state, text)
        self._render_progress_region(jw, state)

    @staticmethod
    def _new_progress_region(jw) -> dict:
        """Start a fresh collapsible block, anchored just before jw's idle
        prompt, on its own clean line so the region-replace below never bleeds
        into the prompt or into unrelated preceding output."""
        doc = jw._control.document()
        pos = jw._append_before_prompt_pos
        if pos > 0 and doc.characterAt(pos - 1) != "\n":
            nl_cursor = QtGui.QTextCursor(doc)
            nl_cursor.setPosition(pos)
            nl_cursor.insertText("\n")
            pos = nl_cursor.position()
        region_cursor = QtGui.QTextCursor(doc)
        region_cursor.setPosition(pos)
        region_cursor.setKeepPositionOnInsert(True)  # anchor stays put across replaces
        return {
            "cursor": region_cursor,
            "processor": AnsiCodeProcessor(),
            "lines": [""],
            "row": 0,
            "col": 0,
        }

    @staticmethod
    def _feed_progress_text(state: dict, text: str) -> None:
        """Replay bluesky TerminalProgressBar's escape subset (\\r, \\n,
        ANSI cursor-up) against an in-memory screen buffer -- this is NOT a
        general ANSI terminal emulator, just enough to track what a progress
        bar's meter line(s) currently look like."""
        processor, lines = state["processor"], state["lines"]
        row, col = state["row"], state["col"]
        for substring in processor.split_string(text):
            for act in processor.actions:
                if act.action == "carriage-return":
                    col = 0
                elif act.action == "newline":
                    row += 1
                    col = 0
                    while len(lines) <= row:
                        lines.append("")
                elif act.action == "move" and act.unit == "line":
                    if act.dir == "up":
                        row = max(0, row - act.count)
                    elif act.dir == "down":
                        row += act.count
                    while len(lines) <= row:
                        lines.append("")
                # erase/scroll/beep/backspace/cursor-visibility: bluesky's
                # TerminalProgressBar never emits these -- ignored on purpose.
            if substring:
                while len(lines) <= row:
                    lines.append("")
                line = lines[row]
                if col > len(line):
                    line = line + " " * (col - len(line))
                lines[row] = line[:col] + substring + line[col + len(substring):]
                col += len(substring)
        state["row"], state["col"] = row, col

    @staticmethod
    def _render_progress_region(jw, state: dict) -> None:
        """Replace the document range this block owns (from its pinned start
        cursor to the current before-prompt boundary) with the collapsed
        screen state -- so the visible result is always the CURRENT state,
        never an accumulation of every past tick."""
        doc = jw._control.document()
        start = state["cursor"].position()
        end = jw._append_before_prompt_pos
        if end < start:
            return  # defensive: document rewound unexpectedly; skip this tick
        work = QtGui.QTextCursor(doc)
        work.setPosition(start)
        work.setPosition(end, QtGui.QTextCursor.KeepAnchor)
        work.beginEditBlock()
        work.removeSelectedText()
        work.insertText("\n".join(state["lines"]) + "\n")
        work.endEditBlock()

    @staticmethod
    def _remember_connection_file(cf: str | None) -> None:
        """Persist the current connection file so a later GUI can reattach."""
        try:
            config.update({"last_kernel_connection_file": cf or ""})
        except Exception:  # noqa: BLE001
            pass

    # ── Public API ────────────────────────────────────────────────────────────

    def run_code(self, src: str) -> None:
        """Run `src` in the console as if typed: echoes the input, shows output."""
        if not src or not src.strip() or self.jupyter_widget is None:
            return
        # RichJupyterWidget.execute() echoes the source AND sends it to the
        # kernel; kernel_client.execute() would not echo.
        self.jupyter_widget.execute(source=src, hidden=False)

    def run_code_sequence(self, blocks: list[str], stop_on_error: bool = True) -> None:
        """Run `blocks` one at a time -- e.g. an auto-injected `det_startup`
        ahead of the real plan, or the configured startup commands -- each as
        its OWN kernel execution AND its own visible "In [n]:" prompt, only
        sending the next block once the previous one's reply has been fully
        processed by this widget.

        ``stop_on_error`` controls what happens when a block's reply comes
        back with an error status: True (the default) aborts the rest of the
        sequence -- the right call for det_startup-then-real-plan, where the
        plan must never run if its prerequisite failed. False keeps going
        regardless -- the right call for independent startup commands (e.g.
        on the BITS profile, a missing `nest_asyncio` shouldn't also skip the
        collection import), see :meth:`run_startup_commands`.

        Two things this avoids:

        * String-concatenating the blocks into one `run_code()` call would
          run them as a single kernel cell, collapsing them into one history
          entry (see `experiment_history.py`) even though they're separate
          plan invocations.
        * Firing them as separate `run_code()` calls back-to-back WITHOUT
          waiting is also unsafe -- for two independent reasons:

          1. If the first errors, ipykernel aborts any execute_request
             already sitting in its queue within `stop_on_error_timeout` of
             the error (default is effectively immediate, but a second
             request sent with no round-trip in between reliably lands
             inside that window) -- and an aborted request never even
             publishes `execute_input`, so it silently vanishes from the
             history record instead of erroring visibly.
          2. Even when nothing errors, `self.jupyter_widget.execute()`
             replaces the widget's *current input buffer* (the editable
             region after its most-recently-shown prompt) with the next
             block's source. If that call lands before the widget has
             redrawn a fresh "In [n]:" prompt for the *previous* block's
             reply, the new source gets stuffed into the stale prompt's
             buffer instead of a new one -- visually merging the two
             commands (or making the second look like the first's output).

        We therefore wait on this widget's own `executed` signal (mirroring
        qtconsole's `jw.executed`), not an IOPub-`status: idle`-derived signal
        like `code_executed` (used elsewhere for cross-client dispatch, e.g.
        the queue runner) -- IOPub and the shell-channel reply that redraws
        the prompt arrive on separate sockets with no ordering guarantee
        between them. qtconsole only emits `executed` AFTER it has called
        `_show_interpreter_prompt_for_reply()` for that request, so by the
        time we're notified, the next prompt is already on screen and safe
        to fill -- fixing both hazards above and
        restoring the original single-cell semantics (the real plan never
        runs if `det_startup` failed).
        """
        blocks = [b for b in (blocks or []) if b and b.strip()]
        if not blocks:
            return
        head, tail = blocks[0], blocks[1:]
        if not tail:
            self.run_code(head)
            return

        def _on_executed(msg) -> None:
            self.executed.disconnect(_on_executed)
            ok = msg.get("content", {}).get("status") == "ok"
            if ok or not stop_on_error:
                self.run_code_sequence(tail, stop_on_error=stop_on_error)

        self.executed.connect(_on_executed)
        self.run_code(head)

    def query_values(self, exprs: dict[str, str], callback) -> None:
        """Silently evaluate `{label: python_expr}`; `callback(dict[label, bool|None])`.

        Uses ``user_expressions`` on a silent, no-history execute — per the
        Jupyter messaging spec this suppresses IOPub broadcast entirely (no
        visible output in the console, no execution-count bump, no history),
        while still returning each expression's repr on the shell channel's
        ``execute_reply``. An expression that errors (e.g. a NameError before
        the relevant name is imported into the kernel) resolves to None rather
        than raising. Mirrors qtconsole's own internal (private)
        ``FrontendWidget._silent_exec_callback`` pattern, batched into one
        round trip instead of one call per expression.
        """
        if not self.is_running():
            callback({k: None for k in exprs})
            return
        msg_id = self.kernel_client.execute(
            "", silent=True, store_history=False, user_expressions=exprs
        )
        self._pending_queries[msg_id] = (exprs, callback)

    def is_busy(self) -> bool:
        """True while the kernel is executing a cell."""
        return self._busy

    def is_ready(self) -> bool:
        """True once the kernel handshake completed (safe to dispatch)."""
        return self._ready

    def _on_shell_msg(self, msg) -> None:
        mtype = msg.get("msg_type") or msg.get("header", {}).get("msg_type")
        msg_id = msg.get("parent_header", {}).get("msg_id")
        if msg_id in self._pending_queries and mtype == "execute_reply":
            self._dispatch_query_reply(msg_id, msg)
            return
        if self._ready:
            return
        if mtype == "kernel_info_reply":
            self._ready = True
            self.ready.emit()

    def _on_iopub_msg(self, msg) -> None:
        """Detect a command that ran to a SUCCESSFUL completion (any client).

        The kernel broadcasts `execute_input`, `error`, and `status` on iopub
        to every attached client for every execution it runs, regardless of
        which client requested it -- including the detached queue runner's
        own `BlockingKernelClient.execute()` calls, which never touch this
        widget's own `executing`/`executed` signals. A cell's messages always
        arrive in order (`execute_input`, ... , an `error` message if it
        raised, then `status: idle` last), so `code_executed` only fires on
        `idle` and only if no `error` was seen for that same execution.
        """
        mtype = msg.get("msg_type") or msg.get("header", {}).get("msg_type")
        parent_id = msg.get("parent_header", {}).get("msg_id")
        if mtype == "execute_input":
            code = msg.get("content", {}).get("code", "")
            if code.strip() and parent_id:
                self._pending_execs[parent_id] = code
            return
        if mtype == "status":
            # Clean up _handle_foreign_progress_stream's per-execution state
            # (see _wire_widget) -- checked BEFORE the _pending_execs guard
            # below since a bare kernel starting/restarting status has no
            # matching parent_id and would otherwise never reach this.
            state = msg.get("content", {}).get("execution_state")
            if state in ("starting", "restarting"):
                self._progress_bars.clear()
            elif state == "idle":
                self._progress_bars.pop(parent_id, None)
        if parent_id not in self._pending_execs:
            return
        if mtype == "error":
            self._errored_execs.add(parent_id)
        elif (
            mtype == "status"
            and msg.get("content", {}).get("execution_state") == "idle"
        ):
            code = self._pending_execs.pop(parent_id)
            failed = parent_id in self._errored_execs
            self._errored_execs.discard(parent_id)
            if not failed:
                self.code_executed.emit(code)

    def _dispatch_query_reply(self, msg_id: str, msg) -> None:
        """Resolve a pending `query_values` call from its execute_reply."""
        exprs, callback = self._pending_queries.pop(msg_id)
        user_exprs = msg.get("content", {}).get("user_expressions", {})
        result: dict[str, object] = {}
        for key in exprs:
            entry = user_exprs.get(key, {})
            if entry.get("status") == "ok":
                try:
                    result[key] = ast.literal_eval(entry["data"]["text/plain"])
                except Exception:  # noqa: BLE001
                    result[key] = None
            else:
                result[key] = None
        callback(result)

    def _on_jw_executing(self, source) -> None:
        self._busy = True
        self.executing.emit(source)

    def _on_jw_executed(self, msg) -> None:
        self._busy = False
        self.executed.emit(msg)

    def run_startup_commands(self) -> None:
        """Run the configured startup command(s), one console cell per line,
        each waiting for the previous one to finish before the next runs.

        Uses the ``bluesky_startup`` config value (Python → Configuration →
        Launch Session), falling back to :data:`BLUESKY_STARTUP` if unset
        (CONNECTS TO HARDWARE if configured to).  Called automatically once
        the kernel handshake completes on a fresh :meth:`start` (see
        ``MainWindow._on_console_ready``) — never after :meth:`attach`.

        **Normally a no-op since 2026-09-03**: every shipped profile now leaves
        ``bluesky_startup`` blank because its starter script performs the
        instrument import itself, inside the kernel, via
        ``--IPKernelApp.exec_lines``. Blank means *run nothing* — it must not
        fall through to some other instrument's import, which is what a
        non-empty default would do to a BITS kernel.

        Any commands that *are* configured go through
        :meth:`run_code_sequence` with ``stop_on_error=False``: they are
        independent of each other, unlike the det_startup-then-plan chain that
        method's default is built around, so one line's failure must never
        swallow the rest — see that method's docstring for why firing them
        back-to-back without waiting at all is unsafe regardless.
        """
        cmd = config.get("bluesky_startup")
        if not cmd or not cmd.strip():
            cmd = BLUESKY_STARTUP
        lines = [line.strip() for line in cmd.splitlines() if line.strip()]
        if not lines:
            return
        self.run_code_sequence(lines, stop_on_error=False)

    def detach(self) -> None:
        """Disconnect the GUI but LEAVE the kernel running (so it can be reattached).

        Stops the client channels only; does not shut the kernel down and does
        not remove the connection file.  Idempotent.
        """
        if self._down:
            return
        self._down = True
        if self.kernel_client is not None:
            try:
                self.kernel_client.stop_channels()
            except Exception:  # noqa: BLE001
                pass

    def shutdown(self) -> None:
        """Stop channels AND terminate the kernel (ends the session).  Idempotent.

        Detaches our client, then ends the kernel process.  If we're connected to
        this beamline's managed session, :func:`kernel_session.stop` also quits
        the hosting ``screen`` session and clears the fixed connection file;
        otherwise we just request the specific kernel to exit.
        """
        cf = self._connection_file
        beamline = config.get("beamline")
        if self._experiment is not None:
            experiment_history.append_entry(beamline, self._experiment, "marker", "Kernel shut down")
        if self.kernel_client is not None:
            try:
                self.kernel_client.stop_channels()
            except Exception:  # noqa: BLE001
                pass
        try:
            if cf and cf == ks.connection_file(beamline):
                ks.stop(beamline)           # shutdown + quit screen + clean files
            elif cf:
                ks.shutdown_kernel(cf)      # arbitrary kernel: just request exit
        except Exception:  # noqa: BLE001
            pass
        if self._proc is not None:          # fallback path (screen unavailable)
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self._down = True
        self._remember_connection_file("")   # session over — nothing to reattach

    def close_session(self) -> None:
        """On GUI close: keep the kernel alive (detach) or kill it, per config.

        Default (``keep_kernel_on_exit`` True) detaches, so the session survives
        and can be reattached next launch.
        """
        if config.get("keep_kernel_on_exit"):
            self.detach()
        else:
            self.shutdown()

    def reset_view(self) -> None:
        """Return to the placeholder so a new kernel can be started/attached.

        Use after :meth:`shutdown` when the GUI stays open (e.g. the user chose
        *Shutdown kernel* and wants to launch a fresh one without restarting).
        """
        if getattr(self, "_band_timer", None) is not None:
            self._band_timer.stop()   # must not fire against jw's _control after deleteLater
        jw = self.jupyter_widget
        if jw is not None:
            self._stack.removeWidget(jw)
            jw.deleteLater()
        self.jupyter_widget = None
        self.kernel_client = None
        self.kernel_manager = None
        self._proc = None
        self._attached = False
        self._ready = False
        self._busy = False
        self._down = False
        self._connection_file = None
        self._experiment = None
        self._pending_queries.clear()
        self._stack.setCurrentWidget(self._placeholder)

    # ── Qt ────────────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # noqa: N802
        """On panel close, keep or kill the kernel per config (see close_session)."""
        self.close_session()
        super().closeEvent(event)
