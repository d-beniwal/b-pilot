"""The BLADE screen: a full-window page (swapped in by ``MainWindow``) with MIDAS_GUI-style two panels.

Left: actions and selections (session, alignment routines, manual moves, stop/abort). Right: what the instrument shows (latest camera frame with BLADE's
overlay, pin-position-vs-omega plot with the fitted trajectory) and a log of every command BLADE sends to the RunEngine.

This module holds UI only. All logic is BLADE's (``blade.registry`` tools run on a worker thread); every action that touches the instrument is a plan
executed by the console kernel shared with the Runner tab, through :class:`blade_bridge.KernelExecutor`. The screen only READS the run folder
(``state.json``, ``events.jsonl``, frame JPEGs) to display progress, exactly like BLADE's own web GUI.
"""
from __future__ import annotations

import json
import os

import numpy as np
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PyQt5 import QtCore, QtGui, QtWidgets

from . import blade_bridge as bridge
from . import config
from . import kernel_session as ks
from . import style as S
from .panel_ribbon import PanelRibbon


class _Worker(QtCore.QThread):
    """Runs one BLADE tool off the GUI thread (the tool blocks while the RunEngine moves)."""
    done = QtCore.pyqtSignal(str, object)
    failed = QtCore.pyqtSignal(str, str)

    def __init__(self, tool: str, kwargs: dict, parent=None) -> None:
        super().__init__(parent)
        self.tool, self.kwargs = tool, kwargs

    def run(self) -> None:  # noqa: D401
        try:
            self.done.emit(self.tool, bridge.blade_registry.invoke(self.tool, **self.kwargs))
        except Exception as exc:  # noqa: BLE001 -- shown to the operator; BLADE never retries
            self.failed.emit(self.tool, f"{type(exc).__name__}: {exc}")


class _FrameView(QtWidgets.QLabel):
    def __init__(self) -> None:
        super().__init__("No frame yet. Start a run, then Measure.")
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setMinimumSize(S.px(320), S.px(240))
        self.setStyleSheet(f"background:#101010; color:{S.MUTED};")
        self._pix: QtGui.QPixmap | None = None

    def show_file(self, path: str) -> None:
        pix = QtGui.QPixmap(path)
        if not pix.isNull():
            self._pix = pix
            self._rescale()

    def _rescale(self) -> None:
        if self._pix is not None:
            self.setPixmap(self._pix.scaled(self.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._rescale()


class _TrajectoryPlot(FigureCanvasQTAgg):
    """Pin column vs omega with the fitted trajectory c + a cos(w) + b sin(w) and the beam centre."""

    def __init__(self) -> None:
        self.fig = Figure(figsize=(4, 3), tight_layout=True)
        super().__init__(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.draw_data([], [], None)

    def draw_data(self, omegas, cols, fit) -> None:
        ax = self.ax
        ax.clear()
        ax.set_xlabel("omega (deg)")
        ax.set_ylabel("pin column (px)")
        if len(omegas):
            ax.plot(omegas, cols, "o", ms=4, color=S.ACCENT, label="measured")
        if fit and all(k in fit for k in ("a", "b", "c")):
            w = np.linspace(-90, 90, 181)
            r = np.radians(w)
            ax.plot(w, fit["c"] + fit["a"] * np.cos(r) + fit["b"] * np.sin(r), "-", color="#4a7", lw=1.2, label="fit")
            ax.axhline(fit["c"], color="#69f", lw=0.8, ls="--", label="axis c")
            if fit.get("beam_cx") is not None:
                ax.axhline(fit["beam_cx"], color="#4a7", lw=0.8, ls=":", label="beam centre")
        if len(omegas) or fit:
            ax.legend(fontsize=7, loc="best")
        ax.grid(alpha=0.25)
        self.draw_idle()


class BladeScreen(QtWidgets.QWidget):
    """Full-window BLADE page. ``backRequested`` returns to the Runner screen."""

    backRequested = QtCore.pyqtSignal()

    def __init__(self, console, parent=None) -> None:
        super().__init__(parent)
        self.console = console
        self.executor = bridge.KernelExecutor(console, self) if bridge.AVAILABLE else None
        if self.executor is not None:
            bridge.blade_executor.set_default(self.executor)
            self.executor.logged.connect(self._on_cmd_logged)
        self._worker: _Worker | None = None
        self._run_dir: str | None = None
        self._events_pos = 0
        self._meas: list[tuple[float, float]] = []
        self._profiles: dict[str, dict] = {}
        self._build_ui()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        self._refresh_state()

    # ── layout ────────────────────────────────────────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.ribbon = PanelRibbon()
        self.ribbon.register_tab("runner", "B-PILOT", self.backRequested.emit)
        self.ribbon.register_tab("blade", "BLADE", lambda: None)
        self.ribbon.set_active("blade", True)
        root.addWidget(self.ribbon)
        split = S.Splitter(QtCore.Qt.Horizontal)
        S.configure_splitter(split)
        split.addWidget(self._build_left())
        split.addWidget(self._build_right())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([S.px(340), S.px(1100)])
        root.addWidget(split, 1)

    def _build_left(self) -> QtWidgets.QWidget:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(S.px(300))
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        host = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(host)
        col.setContentsMargins(8, 8, 8, 8)

        # session
        card = S.make_card("Session")
        self.lbl_console = QtWidgets.QLabel()
        self.lbl_console.setWordWrap(True)
        card.body.addWidget(self.lbl_console)
        form = QtWidgets.QFormLayout()
        self.cb_profile, self.cb_station, self.cb_camera = (S.NoScrollComboBox() for _ in range(3))
        form.addRow("Profile", self.cb_profile)
        form.addRow("Station (hutch)", self.cb_station)
        form.addRow("Camera", self.cb_camera)
        card.body.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        self.btn_start = S.primary_btn("Start run")
        self.btn_finish = QtWidgets.QPushButton("Finish run")
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_finish)
        card.body.addLayout(row)
        self.lbl_run = QtWidgets.QLabel("No run.")
        self.lbl_run.setStyleSheet(f"color:{S.MUTED};")
        self.lbl_run.setWordWrap(True)
        card.body.addWidget(self.lbl_run)
        col.addWidget(card)

        # alignment
        card = S.make_card("Rotation-axis alignment")
        self.btn_adaptive = S.primary_btn("Adaptive align")
        self.btn_adaptive.setToolTip("Rotate until the pin leaves the view on each side, fit, correct z / x / base, repeat. Moves motors.")
        card.body.addWidget(self.btn_adaptive)
        form = QtWidgets.QFormLayout()
        self.sp_step = QtWidgets.QDoubleSpinBox(); self.sp_step.setRange(1, 45); self.sp_step.setValue(15); self.sp_step.setSuffix(" deg")
        self.sp_passes = QtWidgets.QSpinBox(); self.sp_passes.setRange(1, 12); self.sp_passes.setValue(6)
        form.addRow("Step", self.sp_step)
        form.addRow("Max passes", self.sp_passes)
        card.body.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        self.btn_measure = QtWidgets.QPushButton("Measure frame")
        self.btn_sweep = QtWidgets.QPushButton("Sweep")
        self.btn_propose = QtWidgets.QPushButton("Propose")
        for b in (self.btn_measure, self.btn_sweep, self.btn_propose):
            row.addWidget(b)
        card.body.addLayout(row)
        col.addWidget(card)

        # manual
        card = S.make_card("Manual moves (through the RunEngine)")
        row = QtWidgets.QHBoxLayout()
        self.sp_omega = QtWidgets.QDoubleSpinBox(); self.sp_omega.setRange(-90, 90); self.sp_omega.setDecimals(2); self.sp_omega.setSuffix(" deg")
        self.btn_goto = QtWidgets.QPushButton("Go to omega")
        row.addWidget(self.sp_omega); row.addWidget(self.btn_goto)
        card.body.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        self.cb_motor = S.NoScrollComboBox(); self.cb_motor.addItems(["x", "y", "z", "base"])
        self.sp_mm = QtWidgets.QDoubleSpinBox(); self.sp_mm.setRange(-1, 1); self.sp_mm.setDecimals(4); self.sp_mm.setSingleStep(0.01); self.sp_mm.setSuffix(" mm")
        self.btn_move = QtWidgets.QPushButton("Move")
        row.addWidget(self.cb_motor); row.addWidget(self.sp_mm); row.addWidget(self.btn_move)
        card.body.addLayout(row)
        col.addWidget(card)

        # safety
        card = S.make_card("Stop")
        row = QtWidgets.QHBoxLayout()
        self.btn_stop = QtWidgets.QPushButton("Stop (pause RE)")
        self.btn_stop.setStyleSheet(S.status_button_qss(S.ERROR))
        self.btn_abort = QtWidgets.QPushButton("RE.abort()")
        self.btn_halt = QtWidgets.QPushButton("RE.halt()")
        for b in (self.btn_stop, self.btn_abort, self.btn_halt):
            row.addWidget(b)
        card.body.addLayout(row)
        self.lbl_stop = QtWidgets.QLabel("Stop pauses the shared RunEngine and ends the BLADE routine; recover with abort/halt here or in the B-PILOT tab.")
        self.lbl_stop.setWordWrap(True)
        self.lbl_stop.setStyleSheet(f"color:{S.MUTED};")
        card.body.addWidget(self.lbl_stop)
        col.addWidget(card)

        # note
        card = S.make_card("Note")
        self.ed_note = QtWidgets.QLineEdit()
        self.btn_note = QtWidgets.QPushButton("Add note to run log")
        card.body.addWidget(self.ed_note); card.body.addWidget(self.btn_note)
        col.addWidget(card)
        col.addStretch(1)
        scroll.setWidget(host)

        self._populate_profiles()
        self.cb_profile.currentTextChanged.connect(self._on_profile_changed)
        self.btn_start.clicked.connect(self._start_run)
        self.btn_finish.clicked.connect(lambda: self._call("finish_run", status="ok", summary="finished from the BLADE screen"))
        self.btn_adaptive.clicked.connect(lambda: self._call("align_adaptive", step=self.sp_step.value(), max_passes=self.sp_passes.value()))
        self.btn_measure.clicked.connect(lambda: self._call("align_measure"))
        self.btn_sweep.clicked.connect(lambda: self._call("align_sweep"))
        self.btn_propose.clicked.connect(lambda: self._call("align_propose"))
        self.btn_goto.clicked.connect(lambda: self._call("align_goto_omega", deg=self.sp_omega.value()))
        self.btn_move.clicked.connect(lambda: self._call("align_move", motor=self.cb_motor.currentText(), mm=self.sp_mm.value(), reason="manual (BLADE screen)"))
        self.btn_note.clicked.connect(self._add_note)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_abort.clicked.connect(lambda: self._recover("RE.abort()"))
        self.btn_halt.clicked.connect(lambda: self._recover("RE.halt()"))
        return scroll

    def _build_right(self) -> QtWidgets.QWidget:
        outer = S.Splitter(QtCore.Qt.Vertical)
        S.configure_splitter(outer)
        top = S.Splitter(QtCore.Qt.Horizontal)
        S.configure_splitter(top)
        self.frame_view = _FrameView()
        self.plot = _TrajectoryPlot()
        top.addWidget(self.frame_view)
        top.addWidget(self.plot)
        top.setStretchFactor(0, 3)
        top.setStretchFactor(1, 2)
        outer.addWidget(top)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setStyleSheet(f"font-family:{S.MONO_CSS};")
        self.log.setPlaceholderText("Commands BLADE sends to the RunEngine (shared console) and run events appear here.")
        outer.addWidget(self.log)
        outer.setStretchFactor(0, 4)
        outer.setStretchFactor(1, 1)
        outer.setSizes([S.px(620), S.px(200)])
        return outer

    # ── profile selection ────────────────────────────────────────────────────────────────────────────────────────
    def _populate_profiles(self) -> None:
        if not bridge.AVAILABLE:
            self.lbl_console.setText("BLADE is not available:\n" + "\n".join(bridge.diagnose()))
            return
        cfgm = bridge.blade_config
        for name in cfgm.profiles():
            try:
                self._profiles[name] = json.loads((cfgm.profile_dir(name) / "default_config.json").read_text())
            except Exception:  # noqa: BLE001
                continue
        usable = [n for n, p in self._profiles.items() if "bluesky" in p.get("backends", [])]
        self.cb_profile.addItems(usable)
        want = config.get("beamline")
        if want in usable:
            self.cb_profile.setCurrentText(want)
        self._on_profile_changed(self.cb_profile.currentText())

    def _on_profile_changed(self, name: str) -> None:
        p = self._profiles.get(name, {})
        self.cb_station.clear()
        self.cb_station.addItems(sorted(p.get("stations", {})))
        if p.get("default_station"):
            self.cb_station.setCurrentText(p["default_station"])
        self.cb_camera.clear()
        usable = [c for c, i in p.get("cameras", {}).items() if i.get("pv_prefix") or i.get("simulated")]
        self.cb_camera.addItems(usable)
        if p.get("default_camera") in usable:
            self.cb_camera.setCurrentText(p["default_camera"])

    # ── actions ───────────────────────────────────────────────────────────────────────────────────────────────────
    def _busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _start_run(self) -> None:
        self._call("new_run", profile=self.cb_profile.currentText(), station=self.cb_station.currentText(), backend="bluesky",
                   camera=self.cb_camera.currentText() or None)

    def _call(self, tool: str, **kwargs) -> None:
        if not bridge.AVAILABLE or self._busy():
            return
        self._log(f"[{tool}] started")
        if self.executor is not None:
            self.executor.reset_cancel()
        self._worker = _Worker(tool, kwargs, self)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._refresh_state)
        self._worker.start()
        self._refresh_state()

    def _on_done(self, tool: str, result) -> None:
        if isinstance(result, dict) and result.get("ok") is False:     # BLADE's own pre-checks refuse in-band, without raising
            self._log(f"[{tool}] REFUSED: {result.get('reason', result)}")
            self.lbl_stop.setText(f"{tool} refused: {result.get('reason', 'see log')}. Nothing was moved.")
        else:
            self._log(f"[{tool}] finished")
        if tool == "new_run" and isinstance(result, dict):
            self._attach_run(result.get("dir"))

    def _on_failed(self, tool: str, msg: str) -> None:
        self._log(f"[{tool}] FAILED: {msg}")
        self.lbl_stop.setText(f"{tool} failed: {msg}. BLADE stopped; nothing was retried.")

    def _add_note(self) -> None:
        text = self.ed_note.text().strip()
        if text and self._run_dir:
            self._call("add_note", text=text)
            self.ed_note.clear()

    def _stop(self) -> None:
        if self.executor is not None:
            self.executor.cancel()
        ok = ks.interrupt(config.get("beamline"), hard=True)
        self.lbl_stop.setText("RunEngine pause signalled; BLADE routine ends. Choose abort or halt to recover."
                              if ok else "Could not signal the kernel (is it a console kernel started from B-PILOT?).")

    def _recover(self, code: str) -> None:
        if self.console.is_ready() and not self.console.is_busy():
            self.console.run_code(code)

    # ── run display ───────────────────────────────────────────────────────────────────────────────────────────────
    def _attach_run(self, run_dir: str | None) -> None:
        self._run_dir, self._events_pos, self._meas = run_dir, 0, []
        self.plot.draw_data([], [], None)
        self.lbl_run.setText(f"Run: {os.path.basename(run_dir)}" if run_dir else "No run.")

    def _poll(self) -> None:
        self._refresh_state()
        if not self._run_dir:
            return
        ev_path = os.path.join(self._run_dir, "events.jsonl")
        try:
            with open(ev_path) as f:
                f.seek(self._events_pos)
                chunk = f.read()
                self._events_pos = f.tell()
        except OSError:
            return
        changed = False
        for line in chunk.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            self._log(f"  {ev.get('msg', '')}")
            if ev.get("image"):
                self.frame_view.show_file(os.path.join(self._run_dir, ev["image"]))
            d = ev.get("data") or {}
            if ev.get("kind") == "measure" and d.get("pin_col") is not None and d.get("omega") is not None:
                self._meas.append((float(d["omega"]), float(d["pin_col"])))
                changed = True
        if changed or chunk:
            fit = None
            try:
                fit = json.load(open(os.path.join(self._run_dir, "state.json"))).get("last_fit")
            except (OSError, ValueError):
                pass
            self.plot.draw_data([m[0] for m in self._meas], [m[1] for m in self._meas], fit)

    # ── state / log ───────────────────────────────────────────────────────────────────────────────────────────────
    def _log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _on_cmd_logged(self, kind: str, text: str) -> None:
        self._log({"cmd": ">>> ", "ok": "    ", "error": "!!! "}.get(kind, "") + text.replace("\n", "\n    "))

    def _refresh_state(self) -> None:
        if not bridge.AVAILABLE:
            for w in self.findChildren(QtWidgets.QPushButton):
                w.setEnabled(False)
            return
        ready, cbusy = self.console.is_ready(), self.console.is_busy()
        self.lbl_console.setText(
            "Console: not ready - Launch IPython / Attach in the B-PILOT tab first." if not ready
            else "Console: busy (a command is running)." if cbusy else "Console: ready. BLADE moves run in this RunEngine.")
        have_run = self._run_dir is not None
        idle = ready and not self._busy()
        self.btn_start.setEnabled(idle and bool(self.cb_profile.currentText()))
        for b in (self.btn_finish, self.btn_adaptive, self.btn_measure, self.btn_sweep, self.btn_propose, self.btn_goto, self.btn_move, self.btn_note):
            b.setEnabled(idle and have_run)
        self.btn_stop.setEnabled(self._busy())
        self.btn_abort.setEnabled(ready and not cbusy)
        self.btn_halt.setEnabled(ready and not cbusy)
