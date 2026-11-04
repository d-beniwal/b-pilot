"""Continuous-acquisition status button + floating start/stop popup.

:class:`ContAcqButton` is a persistent 2-line status button ("Cont. Aq.:\\n…")
living in the status bar above the console. The instrument keeps no
persistent cont_acq state itself (`instrument/plans/scans_stationary.py`'s
`stop_cont_acq` checks `det.cam.acquire` live), so the button polls every
known area detector on a timer, mirroring `B_PILOT.mode_buttons.ModeButtonBar`.
Green + one line per detector while any are acquiring; gray + "—" when none
are. Clicking it opens :class:`ContAcqPopup`, a `Qt.Popup` with a "start a new
detector" row (console-only, no queue option per spec) and a red "x" per
currently-running detector to stop just that one.
"""
from __future__ import annotations

import os

from PyQt5 import QtCore
from PyQt5 import QtWidgets

from . import command_builder
from . import config
from . import det_startup_state
from . import device_source
from . import midas_bridge
from . import param_form
from . import plan_parser as P
from . import style as S

_POLL_MS = 2800


def _discover_cont_acq() -> dict[str, dict]:
    """Find `cont_acq`/`stop_cont_acq` in `scans_stationary.py`, common to
    every beamline (unlike the per-beamline `switch_to_*` shortcuts)."""
    plans_dir = config.get("plans_dir") or P.USER_DIR
    abs_path = os.path.join(plans_dir, "scans_stationary.py")
    specs = P.find_plan_specs(abs_path)
    module = P.file_to_module(abs_path, config.get("import_root"))
    found = {}
    for name in ("cont_acq", "stop_cont_acq"):
        if name in specs:
            found[name] = {**specs[name], "module": module}
    return found


class ContAcqButton(QtWidgets.QPushButton):
    """Persistent 2-line "Cont. Aq.:\\n<detectors or —>" status button."""

    def __init__(self, console, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._console = console
        self._running: list[str] = []
        self._poll_inflight = False
        self.setStyleSheet(S.status_button_qss(S.MUTED))
        self.setToolTip(
            "Click to start/stop continuous acquisition.\n"
            "Green = acquiring (one line per detector), gray = none running."
        )
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.clicked.connect(self._open_popup)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._poll)

        self.set_console_ready(False)

    # ── Console-readiness ────────────────────────────────────────────────────

    def set_console_ready(self, ready: bool) -> None:
        self.setEnabled(ready)
        if ready:
            self._timer.start()
            self._poll()
        else:
            self._timer.stop()
            self._poll_inflight = False
            self._apply_state([])

    # ── Polling ───────────────────────────────────────────────────────────────

    def _poll(self) -> None:
        if self._poll_inflight or not self._console.is_running():
            return
        names = device_source.get_catalog().names_for("area_detector")
        if not names:
            self._apply_state([])
            return
        self._poll_inflight = True
        self._console.query_values(
            {
                name: (
                    "(lambda v: getattr(v, 'readback', v))"
                    f"({name}.cam.acquire.get(as_string=True))"
                )
                for name in names
            },
            self._on_status,
        )

    def _on_status(self, result: dict) -> None:
        self._poll_inflight = False
        running = sorted(name for name, val in result.items() if val == "Acquiring")
        self._apply_state(running)

    def _apply_state(self, running: list[str]) -> None:
        self._running = running
        if running:
            self.setText("Cont. Aq.:\n" + "\n".join(running))
            self.setStyleSheet(S.status_button_qss(S.CMD_RE))
        else:
            self.setText("Cont. Aq.:\n—")
            self.setStyleSheet(S.status_button_qss(S.MUTED))

    # ── Popup ─────────────────────────────────────────────────────────────────

    def _open_popup(self) -> None:
        popup = ContAcqPopup(self._console, list(self._running), parent=self)
        popup.move(S.clamp_popup_to_window(self, popup))
        popup.show()


class ContAcqPopup(QtWidgets.QFrame):
    """Floating start/stop panel. Console-only (no queue option): both actions
    are one-off beamline toggles, not part of a scan plan being composed."""

    def __init__(
        self,
        console,
        running_detectors: list[str],
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent, QtCore.Qt.Popup)
        self._console = console
        self._specs = _discover_cont_acq()
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setStyleSheet(
            f"QFrame{{background:{S.PANEL};border:1px solid {S.BORDER};"
            f"border-radius:4px;}}"
        )
        self._param_widgets: dict = {}
        self._build_ui(running_detectors)

    def _build_ui(self, running_detectors: list[str]) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        start_spec = self._specs.get("cont_acq")
        if start_spec is None:
            outer.addWidget(QtWidgets.QLabel("cont_acq plan not found."))
        else:
            start_row = QtWidgets.QHBoxLayout()
            start_row.setSpacing(8)
            self._params = start_spec["params"]
            self._param_widgets = param_form.build_row(
                start_row, self._params, self._live_validate
            )
            start_row.addStretch(1)
            self._status_lbl = QtWidgets.QLabel("")
            self._status_lbl.setStyleSheet(f"color: {S.MUTED};")
            start_row.addWidget(self._status_lbl)
            self._start_btn = S.primary_btn("▶  Start")
            self._start_btn.clicked.connect(self._start)
            start_row.addWidget(self._start_btn)
            outer.addLayout(start_row)
            self._live_validate()

        if running_detectors:
            outer.addWidget(QtWidgets.QLabel("Running:"))
            for name in running_detectors:
                row = QtWidgets.QHBoxLayout()
                row.addWidget(QtWidgets.QLabel(name))
                row.addStretch(1)
                stop_btn = QtWidgets.QPushButton("STOP")
                stop_btn.setToolTip(f"Stop continuous acquisition on {name}.")
                stop_btn.setStyleSheet(
                    f"QPushButton{{background:{S.ERROR};color:white;"
                    f"font-weight:bold;border-radius:{S.px(4)}px;"
                    f"padding:{S.px(3)}px {S.px(14)}px;}}"
                )
                stop_btn.clicked.connect(lambda _c=False, n=name: self._stop(n))
                row.addWidget(stop_btn)
                outer.addLayout(row)

    # ── Validation ────────────────────────────────────────────────────────────

    def _live_validate(self) -> None:
        errors = []
        for spec in self._params:
            widget = self._param_widgets[spec.name][1]
            err = param_form.field_error(spec, widget)
            if spec.dtype not in ("bool", "device_list"):
                S.mark_invalid(widget, err is not None)
            if err:
                errors.append(err)
        self._start_btn.setEnabled(self._console.is_running() and not errors)
        if errors:
            self._status_lbl.setText(f"⚠ {len(errors)} field(s) to fix")
            self._status_lbl.setToolTip("\n".join(errors))
        else:
            self._status_lbl.setText("")
            self._status_lbl.setToolTip("")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _start(self) -> None:
        spec = self._specs["cont_acq"]
        values, errors = param_form.parse_values(self._params, self._param_widgets)
        if errors:
            return
        import_line = command_builder.make_import_line("cont_acq", spec["module"])
        re_line = command_builder.make_re_line("cont_acq", self._params, values)
        detectors = midas_bridge.area_detector_devices(self._params, values)
        startup = det_startup_state.build_startup_commands(
            config.get("beamline"), detectors
        )
        # Sent as its own execute() call (not string-concatenated with the
        # cont_acq lines) so it lands as its own history/plan-history entry --
        # det_startup and cont_acq are separate plan invocations. Uses
        # run_code_sequence (waits for det_startup's result) rather than two
        # bare run_code() calls -- see its docstring for why firing both
        # immediately can silently drop cont_acq if det_startup errors.
        # for_console() drops the import line unless this profile's
        # `send_import_line` is on -- see command_builder.for_console.
        main_cmd = command_builder.for_console(f"{import_line}\n{re_line}")
        self._console.run_code_sequence([startup, main_cmd] if startup else [main_cmd])
        midas_bridge.notify_interactive(
            self._console, detectors, config.get("midas_bridge_enabled")
        )
        self.close()

    def _stop(self, det_name: str) -> None:
        spec = self._specs.get("stop_cont_acq")
        if spec is None:
            self.close()
            return
        from .plan_parser import RawCode

        import_line = command_builder.make_import_line("stop_cont_acq", spec["module"])
        re_line = command_builder.make_re_line(
            "stop_cont_acq", spec["params"], {"det": RawCode(det_name)}
        )
        self._console.run_code(command_builder.for_console(f"{import_line}\n{re_line}"))
        self.close()
