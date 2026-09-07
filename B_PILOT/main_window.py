"""Main window: toolbar + plan-runner (left) + console / notes (right)."""
from __future__ import annotations

import os
import sys

from PyQt5 import QtCore
from PyQt5 import QtWidgets

from . import autopilot_bridge
from . import command_builder
from . import config
from . import det_startup_state
from . import device_source
from . import kernel_session as ks
from . import midas_bridge
from . import paths
from . import queue_panel
from . import style as S
from .console_panel import ConsolePanel
from .contacq_popup import ContAcqButton
from .mode_buttons import ModeButtonBar
from .panel_ribbon import CollapsibleDockPanel
from .panel_ribbon import PanelRibbon
from .plan_runner import PlanRunnerPanel
from .report_panel import ReportDockWidget
from .run_controls import RunControlBar
from .session_log import SessionLogView
from .switchto_popup import SwitchToButton

# Default work dir (kernel cwd): the Bluesky root, so a launched kernel's
# ``from instrument.collection import *`` resolves regardless of where the GUI
# was started from.  Editable in the toolbar.
_DEFAULT_LAUNCH_DIR = paths.BLUESKY_ROOT


def _launch_dir() -> str:
    """Directory the embedded kernel is started in, for the active profile.

    The profile's ``kernel_work_dir`` when set, else the Bluesky root (the
    original, unconditional behavior). Expanded, so a stored ``~/...`` is a
    real home-relative path rather than a literal ``~`` directory -- this is
    the value that gets ``os.makedirs``'d at launch, so an unexpanded tilde
    would silently create a folder named ``~`` under the GUI's own cwd.

    It matters which directory this is: a plan's CWD-relative outputs land
    here (3-ID-C's ``flyscan`` writes its master files beside the running
    console), and apsbits resolves ``iconfig.yml``'s relative ``MD_PATH``
    against it too -- so ``RE.md``, ``scan_id`` included, is per-directory.
    """
    configured = (config.get("kernel_work_dir") or "").strip()
    if not configured:
        return _DEFAULT_LAUNCH_DIR
    return os.path.abspath(os.path.expanduser(configured))


def _shutdown_btn_qss() -> str:
    """QSS for the toolbar Shutdown-kernel button: the normal button fill,
    but a bold red border marking it as a dangerous, hard-to-reverse action
    -- set apart from the other borders in the same style as Stop run's
    solid-red fill (run_controls.py's ``_stop_qss``), just less shouty since
    this one sits right next to Profile/Launch controls, not below the
    console where a run is actually in flight."""
    border = S.darken(S.ERROR, 130)
    return (
        f"QPushButton{{border:{S.px(2)}px solid {S.ERROR};}}"
        f"QPushButton:hover{{border-color:{border};}}"
        f"QPushButton:disabled{{border-color:{S.BUTTON_DISABLED_BORDER};}}"
    )


class MainWindow(QtWidgets.QMainWindow):
    """QMainWindow hosting the plan runner, the embedded console, and run notes."""

    _ATTACH_TOOLTIP_IDLE = (
        "Reconnect to a kernel left running by a previous GUI session "
        "(Console → Attach to running kernel…)."
    )
    _ATTACH_TOOLTIP_FOUND = (
        "A Bluesky kernel is already running for this beamline — click to attach."
    )

    def __init__(self) -> None:
        """Build the toolbar + split layout and wire the console lifecycle."""
        super().__init__()
        self.setWindowTitle("B-PILOT: Bluesky - Plan Interface for Launch, Operation & Tracking")
        self.resize(S.px(1500), S.px(900))
        self.setMinimumSize(S.px(980), S.px(600))
        # AutoPILOT is the only dock widget, but nesting still widens Qt's own
        # redock hit-testing along an edge (see the AutoPILOT dock setup below).
        self.setDockNestingEnabled(True)

        self._last_launch_experiment: str | None = None
        self._exp_probe_inflight = False
        self._viewer_pid: int | None = None
        self._viewer_poll_timer: QtCore.QTimer | None = None
        self._main_split_saved_sizes: list[int] | None = None
        # Read once at startup (restart required to change, same pattern as
        # bluesky_root/ui_scale) -- "native" makes zero connection attempts
        # toward a queue server; only "qs" ever touches qs_client.
        self._queue_backend = config.get("queue_backend")

        self.ribbon = PanelRibbon()
        self.runner = PlanRunnerPanel(ribbon=self.ribbon)
        self.console = ConsolePanel()
        self.session_log = SessionLogView()
        self.report = ReportDockWidget(self)
        self.report.set_console(self.console)
        self.run_controls = RunControlBar(self.console)
        self.mode_buttons = ModeButtonBar(self.console)
        self.queue = queue_panel.create_queue_panel(self.console)
        self._mode_btn = SwitchToButton(self.console)
        self._contacq_btn = ContAcqButton(self.console)
        self.runner.runRequested.connect(self._on_run)
        self.runner.queueRequested.connect(self._on_queue)
        self._mode_btn.runRequested.connect(self._on_run)
        self._mode_btn.queueRequested.connect(self._on_queue)
        self.queue.copyToFormRequested.connect(self.runner.load_from_command)
        self.console.started.connect(self._on_console_started)
        self.console.ready.connect(self._on_console_ready)
        self.console.attach_failed.connect(self._on_attach_failed)
        self.console.launch_blocked.connect(self._on_launch_blocked)
        self.console.code_executed.connect(self._mode_btn.note_code_ran)

        central = QtWidgets.QWidget()
        clay = QtWidgets.QVBoxLayout(central)
        clay.setContentsMargins(0, 0, 0, 0)
        clay.setSpacing(0)
        clay.addWidget(self._build_toolbar())

        self._main_split = S.Splitter(QtCore.Qt.Horizontal)
        S.configure_splitter(self._main_split)
        self._main_split.addWidget(self.runner)
        self._main_split.addWidget(self._build_right_panel())
        self._main_split.setStretchFactor(0, 1)
        self._main_split.setStretchFactor(1, 1)
        self._main_split.setSizes([840, 620])
        self.runner.bothPanelsMinimizedChanged.connect(self._on_runner_both_minimized)

        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.ribbon)
        row.addWidget(self._main_split, 1)
        clay.addLayout(row, 1)

        self.setCentralWidget(central)

        # Data Viewer: always has a ribbon tab, launched as a detached,
        # independent process (see _toggle_viewer) -- best-effort highlight
        # only (pid-liveness polling), since it isn't a panel of this window.
        self.ribbon.register_tab("viewer", "Data Viewer", self._toggle_viewer)

        # AutoPILOT chat dock: created once, right here, whenever AutoPILOT/
        # is present and importable (see B_PILOT/autopilot_bridge.py) -- its
        # ribbon tab and menu checkbox thereafter only toggle *visibility*,
        # never existence, so the tab is always there per the ribbon's
        # permanent-tab design (see panel_ribbon.py). When AutoPILOT isn't
        # importable, the tab still stays put (never just missing) but wires
        # to a diagnostic popup instead, so the failure is visible and
        # actionable rather than a tab that silently isn't there.
        self.autopilot_chat = None
        self._autopilot_collapsible = None
        if autopilot_bridge.AVAILABLE:
            self.autopilot_chat = autopilot_bridge.ChatDockWidget(self.runner, self)
            # Left/right only (never top/bottom) -- a chat panel is unusable as
            # a thin horizontal strip, and narrowing the allowed areas also
            # stops the top/bottom corners from competing with the right edge
            # for Qt's redock-target hit-testing.
            self.autopilot_chat.setAllowedAreas(
                QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea
            )
            self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.autopilot_chat)
            self.autopilot_chat.setVisible(bool(config.get("autopilot_enabled")))
            self.autopilot_chat.visibilityChanged.connect(
                self._on_autopilot_visibility_changed
            )
            self._autopilot_collapsible = CollapsibleDockPanel(
                self.autopilot_chat, self.ribbon, "autopilot", "AutoPILOT"
            )
        else:
            self.ribbon.register_tab(
                "autopilot", "AutoPILOT", self._show_autopilot_diagnostics
            )

        # The experiment report, docked on the same edge as AutoPILOT.
        # `setDockNestingEnabled(True)` above means Qt tabs or splits the two
        # against each other on its own -- no explicit tabifyDockWidget needed.
        # Left/right only, for the same reason the chat dock is: a document is
        # unreadable as a thin horizontal strip.
        self.report.setAllowedAreas(
            QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea
        )
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.report)
        self.report.setVisible(bool(config.get("report_enabled")))
        self.report.visibilityChanged.connect(self._on_report_visibility_changed)
        self._report_collapsible = CollapsibleDockPanel(
            self.report, self.ribbon, "report", "Report"
        )

        self._build_menu()
        self.statusBar().showMessage(
            "Pick a Launch mode → Launch IPython, then Run plans."
        )
        QtCore.QTimer.singleShot(0, self._refresh_attach_availability)
        QtCore.QTimer.singleShot(0, self._check_bluesky_root_override)

    def _check_bluesky_root_override(self) -> None:
        error = paths.BLUESKY_ROOT_OVERRIDE_ERROR
        if not error:
            return
        QtWidgets.QMessageBox.warning(
            self,
            "Bluesky root not applied",
            f"{error}\n\n"
            f"Falling back to the auto-detected location:\n{paths.BLUESKY_ROOT}\n\n"
            "Fix the \"Bluesky root\" path in Configuration → Paths, or "
            "clear it to use auto-detection.",
        )

    # ── Toolbar ───────────────────────────────────────────────────────────────

    def _build_toolbar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QFrame()
        bar.setObjectName("toolbar")
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(6)

        lay.addWidget(QtWidgets.QLabel("Profile:"))
        self._profile_combo = S.NoScrollComboBox()
        self._profile_combo.setObjectName("profileSwitcher")
        self._profile_combo.setToolTip(
            "Active beamline profile -- drives device/plan discovery, paths, "
            "and launch settings. Locked while a console/kernel is running; "
            "shut it down first to switch."
        )
        # Qt's item-view painting derives non-selected rows' text color from
        # a tint of the current QPalette.Highlight color, which a
        # QAbstractItemView stylesheet selector cannot override (it's
        # resolved from the palette at paint time). A delegate that forces
        # the palette per-row is the fix -- see ContrastItemDelegate.
        self._profile_combo_delegate = S.ContrastItemDelegate(
            S.INPUT_BG, S.INPUT_FG, S.PROFILE_SWITCHER_BG, "white", parent=self._profile_combo,
        )
        self._profile_combo.view().setItemDelegate(self._profile_combo_delegate)
        self._refresh_profile_switcher()
        self._profile_combo.currentTextChanged.connect(self._on_profile_switcher_changed)
        lay.addWidget(self._profile_combo)

        lay.addWidget(QtWidgets.QLabel("Bluesky dir:"))
        self._workdir = QtWidgets.QLineEdit(_launch_dir())
        self._workdir.setMinimumWidth(S.px(160))
        self._workdir.setMaximumWidth(S.px(230))
        self._workdir.setToolTip(
            "Directory the Bluesky session runs in — where a plan's "
            "CWD-relative output files land (and where the RunEngine's "
            "metadata/scan_id file is kept).\nRemembered per profile; "
            "blank resets it to the Bluesky root."
        )
        lay.addWidget(self._workdir)

        browse_btn = QtWidgets.QToolButton()
        browse_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DirOpenIcon))
        browse_btn.setToolTip("Select the Bluesky directory…")
        browse_btn.clicked.connect(self._browse_dir)
        lay.addWidget(browse_btn)

        self._launch_btn = S.primary_btn("▶  Launch IPython")
        self._launch_btn.clicked.connect(self._launch_console)
        lay.addWidget(self._launch_btn)

        self._attach_btn = QtWidgets.QPushButton("Attach")
        self._attach_btn.setToolTip(self._ATTACH_TOOLTIP_IDLE)
        self._attach_btn.clicked.connect(self._attach_console)
        lay.addWidget(self._attach_btn)

        self._midas_bridge_checkbox = QtWidgets.QCheckBox("Bridge Live-View")
        self._midas_bridge_checkbox.setChecked(bool(config.get("midas_bridge_enabled")))
        self._midas_bridge_checkbox.setToolTip(
            "When on, a Run/Queue dispatch that includes an area-detector "
            "device auto-starts MIDAS_GUI's Live Data view on that "
            "detector's PVA channel (no-op if MIDAS_GUI isn't running). "
            "Turn off if MIDAS_GUI is being used for something else and "
            "auto-starting Live Data would disrupt it."
        )
        self._midas_bridge_checkbox.toggled.connect(self._on_midas_bridge_toggled)
        lay.addWidget(self._midas_bridge_checkbox)

        lay.addStretch(1)
        self._toolbar_status = QtWidgets.QLabel("")
        self._toolbar_status.setStyleSheet(f"color: {S.MUTED};")
        lay.addWidget(self._toolbar_status)

        # A second stretch keeps Shutdown pinned to the far right edge, well
        # clear of the status label and the Profile/Launch cluster -- a
        # dangerous, irreversible action shouldn't sit shoulder-to-shoulder
        # with everyday controls.
        lay.addStretch(1)
        self._shutdown_btn = QtWidgets.QPushButton("Shut down kernel")
        self._shutdown_btn.setToolTip(
            "Terminate the kernel. A killed kernel cannot be resumed later."
        )
        self._shutdown_btn.setStyleSheet(_shutdown_btn_qss())
        self._shutdown_btn.setEnabled(False)
        self._shutdown_btn.clicked.connect(self._shutdown_kernel)
        lay.addWidget(self._shutdown_btn)
        return bar

    def _browse_dir(self) -> None:
        start = self._workdir.text().strip() or os.path.expanduser("~")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Bluesky directory", start
        )
        if chosen:
            self._workdir.setText(chosen)

    def _on_midas_bridge_toggled(self, checked: bool) -> None:
        config.update({"midas_bridge_enabled": checked})

    def _refresh_profile_switcher(self) -> None:
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        self._profile_combo.addItems(config.list_profiles())
        idx = self._profile_combo.findText(config.active_profile())
        self._profile_combo.setCurrentIndex(max(0, idx))
        self._profile_combo.blockSignals(False)

    def _apply_profile_change(self, notify: str) -> None:
        """Refresh everything derived from the active profile.

        Shared by the Configuration dialog's Save and the toolbar profile
        switcher -- both just flip the active profile and need the same
        follow-up: the device catalog is cached per beamline, and the plan
        browser scope depends on the profile's configured plan files.
        """
        device_source.set_beamline(config.get("beamline"))
        self._workdir.setText(_launch_dir())
        self.runner.apply_config()
        self._sync_autopilot_visibility()
        self._act_autopilot.blockSignals(True)
        self._act_autopilot.setChecked(bool(config.get("autopilot_enabled")))
        self._act_autopilot.blockSignals(False)
        self._midas_bridge_checkbox.blockSignals(True)
        self._midas_bridge_checkbox.setChecked(bool(config.get("midas_bridge_enabled")))
        self._midas_bridge_checkbox.blockSignals(False)
        # The report is filed per beamline, so a profile switch re-points it;
        # its title and visibility are per-profile settings too.
        self.report.setVisible(bool(config.get("report_enabled")))
        self._act_report.blockSignals(True)
        self._act_report.setChecked(bool(config.get("report_enabled")))
        self._act_report.blockSignals(False)
        self.report.load(config.get("beamline"), self.console.experiment)
        self._set_toolbar_status(notify)
        QtCore.QTimer.singleShot(0, self._refresh_attach_availability)

    def _on_profile_switcher_changed(self, name: str) -> None:
        if not name or name == config.active_profile():
            return
        old_scale = config.get("ui_scale")
        old_theme = config.get("theme")
        old_font = config.get("font_family")
        old_bluesky_root = config.get("bluesky_root")
        config.set_active_profile(name)
        self._apply_profile_change(f"Switched to profile '{name}'.")
        root_changed = config.get("bluesky_root") != old_bluesky_root
        appearance_changed = (
            config.get("ui_scale") != old_scale
            or config.get("theme") != old_theme
            or config.get("font_family") != old_font
        )
        if root_changed:
            # paths.py resolves the Bluesky root (and, from it, the plan
            # directory and import root) once at import time, so the panels
            # are still pointed at the PREVIOUS profile's checkout until
            # B-PILOT is restarted. Worth spelling out rather than filing
            # under "appearance": until then the plan list reads the old
            # tree with the new profile's file whitelist, which usually
            # means it simply comes up empty -- a confusing state to hit
            # without being told why. Switching between instruments of
            # different layouts (mpe_bluesky vs. a BITS package such as
            # 3-ID-C's id3c) always lands here.
            QtWidgets.QMessageBox.warning(
                self,
                "Restart required",
                f"Profile '{name}' uses a different Bluesky root:\n"
                f"{config.get('bluesky_root') or '(auto-detected)'}\n\n"
                "That is resolved once at startup, so the plan list, import "
                "lines and device catalog still refer to the previous "
                "profile's checkout.\n\nRestart B-PILOT before running "
                "anything from this profile.",
            )
        elif appearance_changed:
            QtWidgets.QMessageBox.information(
                self,
                "Restart required",
                "Restart B-PILOT for the new profile's appearance settings "
                "to take effect.",
            )

    # ── Right panel: console + notes ────────────────────────────────────────────

    def _build_right_panel(self) -> QtWidgets.QWidget:
        vsplit = S.Splitter(QtCore.Qt.Vertical)
        S.configure_splitter(vsplit)

        console_card = S.make_card("IPython console")
        console_card.body.addWidget(self._build_status_bar())
        self._console_tabs = QtWidgets.QTabWidget()
        self._console_tabs.addTab(self.console, "Console")
        self._console_tabs.addTab(self.session_log, "Session log")
        self._console_tabs.setTabToolTip(
            1,
            "Full kernel transcript (input + output). Keeps recording even while "
            "the GUI is closed or the kernel is busy — so nothing is lost and you "
            "can watch a running plan live without waiting for the prompt.",
        )
        if self._queue_backend == "qs":
            # Only built/added for the QS backend -- its own poll timer is
            # what actually triggers qs_client's first connection attempt,
            # so a Native-only user never gets one.
            from .qs_console_panel import QSConsolePanel

            self.qs_console = QSConsolePanel()
            self._console_tabs.addTab(self.qs_console, "QS Console")
            self._console_tabs.setTabToolTip(
                2,
                "RunEngine console output for plans dispatched through the queue "
                "server (print statements, scan progress, errors) — a separate "
                "RunEngine process from the embedded-kernel Console/Session log tabs.",
            )
        console_card.body.addWidget(self._console_tabs)
        # BEAMMODE/SHUTTERMODE toggles share run_controls' top row with Stop run
        # / Shut down kernel, rather than sitting in a row of their own.
        self.run_controls.add_trailing_widget(self.mode_buttons)
        console_card.body.addWidget(self.run_controls)
        vsplit.addWidget(console_card)

        queue_card = S.make_card("Plan queue (scheduler)")
        queue_card.body.addWidget(self.queue)
        vsplit.addWidget(queue_card)

        vsplit.setStretchFactor(0, 1)
        vsplit.setStretchFactor(1, 0)
        vsplit.setSizes([570, 260])
        return vsplit

    def _build_status_bar(self) -> QtWidgets.QWidget:
        """3-part strip above the console: Experiment / Mode / Cont. Aq.

        The experiment banner gets half the strip's width; the two action
        buttons split the other half between them (they use an Expanding
        size policy — see SwitchToButton/ContAcqButton — so the stretch
        factors below actually grow them instead of leaving dead space).
        """
        bar = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 6)
        lay.setSpacing(6)

        self._exp_label = QtWidgets.QLabel("")
        self._exp_label.setAlignment(QtCore.Qt.AlignCenter)
        self._exp_label.setVisible(False)
        lay.addWidget(self._exp_label, 2)
        lay.addWidget(self._mode_btn, 1)
        lay.addWidget(self._contacq_btn, 1)
        return bar

    def _sender_area_detectors(self) -> list:
        """area_detector device name(s) for whichever panel (form or
        SwitchTo) just emitted runRequested/queueRequested -- both expose
        the same `last_dispatch_area_detector_devices()` accessor."""
        sender = self.sender()
        if sender in (self.runner, self._mode_btn):
            return sender.last_dispatch_area_detector_devices()
        return []

    def _on_run(self, command: str, notes: str) -> None:
        """Run the command in the console.

        `notes` is already baked into `command` by `plan_runner` as
        ``md={'notes': ...}`` on the generated ``RE(plan(...))`` call, so it
        lands in the run's start document (``cat[uid].metadata["start"]``).
        It is *also* recorded in the experiment report here -- the start
        document is only readable once the catalog has ingested the run,
        whereas the report is the thing the user is reading right now. The
        full command goes with it so `report_builder.attach_notes` can file
        the note under the run it belongs to rather than by bare timestamp.
        """
        detectors = self._sender_area_detectors()
        startup = det_startup_state.build_startup_commands(
            config.get("beamline"), detectors
        )
        # Sent as its own execute() call (not string-concatenated with
        # `command`) so it lands as its own history/plan-history entry --
        # det_startup and the real plan are separate plan invocations. Uses
        # run_code_sequence (waits for det_startup's result) rather than two
        # bare run_code() calls -- see its docstring for why firing both
        # immediately can silently drop `command` if det_startup errors.
        # The panel's command text carries its `from ... import ...` line for
        # display; whether that line is actually sent is per-profile -- see
        # command_builder.for_console / config's `send_import_line`.
        self.report.add_note(notes, command)
        command = command_builder.for_console(command)
        self.console.run_code_sequence([startup, command] if startup else [command])
        # Only trigger the MIDAS_GUI live-view bridge for dispatches that
        # came from the plan-form panel itself, not the SwitchTo popup
        # (out of scope for v1 — see B_PILOT/midas_bridge.py's plan notes).
        if self.sender() is self.runner:
            midas_bridge.notify_interactive(
                self.console,
                detectors,
                config.get("midas_bridge_enabled"),
            )

    def _on_queue(self, command: str, notes: str, qs_item: dict) -> None:
        """Append a plan to the active queue backend (see
        ``config.get("queue_backend")`` / :func:`queue_panel.create_queue_panel`).

        **Native** (default): `notes` is stored separately by `queue_store`
        for the queue panel's tooltip display only — the actual attachment
        to the run's start document happens via the ``md={'notes': ...}``
        already embedded in `command` (see `plan_runner._make_re_line`). Any
        needed `det_startup` is injected later, by `queue_runner.py` right
        before it actually dispatches this item — not here, since the item
        may sit queued for a while and the kernel's real state (or what's
        already been started by something else) could change before it runs.

        **QS**: `qs_item` (built by `plan_runner`/`switchto_popup` via
        `command_builder.make_queue_item`) is the structured dict actually
        sent to the queue server; `command` is used only as a fallback for
        logging. `notes` land on the item's ``meta`` (spread as the run's
        own ``RE(plan(...), **meta)`` metadata kwargs), not a separate
        store, since QS reliably preserves ``meta`` but drops any other
        unrecognized top-level key. Any needed `det_startup` is enqueued
        now, as its own separate queue item immediately ahead of this one
        (see `det_startup_state.build_startup_items`) — QS dispatches items
        itself, so B-PILOT has no hook to inject text right before actual
        execution the way the native queue runner does; this checks
        staleness at enqueue time instead of dispatch time, an accepted,
        documented behavior change.
        """
        area_detectors = self._sender_area_detectors()
        if self._queue_backend == "qs":
            if not qs_item:
                return  # hand-edited text / unsupported shape -- see plan_runner
            for startup_item in det_startup_state.build_startup_items(
                config.get("beamline"), area_detectors
            ):
                self.queue.add(startup_item)
            item = dict(qs_item)
            if notes:
                item["meta"] = {**item.get("meta", {}), "notes": notes}
            self.queue.add(item)
        else:
            self.queue.add(command, notes, area_detectors=area_detectors)
        # Recorded now, but filed under the run once it actually dispatches:
        # report_builder recovers the note from the command's own md={'notes'}
        # and de-duplicates this copy away. This copy only earns its keep for a
        # HAND-EDITED command, where plan_runner drops md= entirely and the
        # note would otherwise be lost (see report_builder.attach_notes).
        self.report.add_note(notes, command)

    # ── Menu ──────────────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        pym = self.menuBar().addMenu("&Python")
        act_config = pym.addAction("Configuration…")
        act_config.setShortcut("Ctrl+,")
        act_config.setToolTip("Configure the visible plan files and launch command.")
        act_config.triggered.connect(self._open_config)

        pym.addSeparator()
        act_viewer = pym.addAction("Open Bluesky Viewer")
        act_viewer.setToolTip(
            "Open the data viewer in a separate, independent window/process."
        )
        act_viewer.triggered.connect(self._toggle_viewer)

        self._act_autopilot = pym.addAction("AutoPILOT")
        self._act_autopilot.setCheckable(True)
        self._act_autopilot.setChecked(
            autopilot_bridge.AVAILABLE and bool(config.get("autopilot_enabled"))
        )
        if autopilot_bridge.AVAILABLE:
            self._act_autopilot.setToolTip("Show/hide the AutoPILOT chat panel.")
        else:
            self._act_autopilot.setEnabled(False)
            self._act_autopilot.setToolTip(
                "AutoPILOT was not found (or its dependencies aren't "
                "installed) next to this B-PILOT checkout."
            )
        self._act_autopilot.toggled.connect(self._on_autopilot_toggled)
        self._act_report = pym.addAction("Experiment Report")
        self._act_report.setCheckable(True)
        self._act_report.setChecked(bool(config.get("report_enabled")))
        self._act_report.setToolTip(
            "Show the live lab record for the running experiment."
        )
        self._act_report.toggled.connect(self._on_report_toggled)

        m = self.menuBar().addMenu("&Console")
        self._act_attach = m.addAction("Attach to running kernel…")
        self._act_attach.triggered.connect(self._attach_console)
        self._act_restart = m.addAction("Restart kernel")
        self._act_restart.setEnabled(False)
        self._act_restart.triggered.connect(self._restart_kernel)
        m.addSeparator()
        self._act_shutdown = m.addAction("Shut down kernel")
        self._act_shutdown.setEnabled(False)
        self._act_shutdown.triggered.connect(self._shutdown_kernel)

    def _open_config(self) -> None:
        """Open the Configuration dialog; apply changes on Save."""
        from .config_dialog import ConfigDialog

        old_scale = config.get("ui_scale")
        old_theme = config.get("theme")
        old_font = config.get("font_family")
        old_bluesky_root = config.get("bluesky_root")
        dlg = ConfigDialog(self)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            # The dialog may have created/deleted/renamed profiles or
            # switched the active one — refresh the toolbar switcher's item
            # list and selection to match before pushing the derived state.
            self._refresh_profile_switcher()
            self._apply_profile_change("Configuration saved.")
            if (
                config.get("ui_scale") != old_scale
                or config.get("theme") != old_theme
                or config.get("font_family") != old_font
                or config.get("bluesky_root") != old_bluesky_root
            ):
                QtWidgets.QMessageBox.information(
                    self,
                    "Restart required",
                    "Restart B-PILOT for the new appearance and/or "
                    "Bluesky-root settings to take effect.",
                )

    def _show_autopilot_diagnostics(self) -> None:
        """AutoPILOT's ribbon-tab click when it failed to load -- report
        why instead of the tab doing nothing (see autopilot_bridge.diagnose,
        which checks the folder + dependency failure modes .context/DEPLOY.md
        calls out, then falls back to the raw import error)."""
        issues = autopilot_bridge.diagnose()
        detail = "\n\n".join(issues) or "AutoPILOT could not be loaded for an unknown reason."
        QtWidgets.QMessageBox.warning(
            self,
            "AutoPILOT unavailable",
            "AutoPILOT is not available in this B-PILOT checkout:\n\n" + detail,
        )

    def _on_autopilot_toggled(self, checked: bool) -> None:
        if self.autopilot_chat is not None:
            self.autopilot_chat.setVisible(checked)

    def _sync_autopilot_visibility(self) -> None:
        """Show/hide the AutoPILOT chat dock to match the current "Enable
        the AutoPILOT chat panel" setting (Configuration -> Appearance).

        The dock itself is created once, at startup (see __init__), and
        never destroyed — its ribbon tab is permanent, so this only ever
        toggles visibility. Called after every Configuration save, so
        toggling the checkbox takes effect immediately — no restart needed.
        """
        if self.autopilot_chat is not None:
            self.autopilot_chat.setVisible(bool(config.get("autopilot_enabled")))

    def _on_autopilot_visibility_changed(self, visible: bool) -> None:
        """Keep config + the menu checkbox in sync no matter what changed
        the dock's visibility (ribbon tab, title-bar close, menu, config)."""
        config.update({"autopilot_enabled": visible})
        act = getattr(self, "_act_autopilot", None)
        if act is not None:
            act.blockSignals(True)
            act.setChecked(visible)
            act.blockSignals(False)

    def _on_report_toggled(self, checked: bool) -> None:
        self.report.setVisible(checked)

    def _on_report_visibility_changed(self, visible: bool) -> None:
        """Keep config + the menu checkbox in sync however the dock was hidden
        (ribbon tab, title-bar close, menu) -- mirrors the AutoPILOT pair above."""
        config.update({"report_enabled": visible})
        act = getattr(self, "_act_report", None)
        if act is not None:
            act.blockSignals(True)
            act.setChecked(visible)
            act.blockSignals(False)

    def _restart_kernel(self) -> None:
        # The kernel runs detached (client-only connection), so "restart" is a
        # shutdown + fresh launch in the same work dir. Only for kernels we
        # started (an attached kernel is someone else's to manage).
        if not self.console.is_running() or self.console.is_attached():
            return
        ok = QtWidgets.QMessageBox.question(
            self,
            "Restart kernel",
            "Restart the IPython kernel? All in-console state is lost.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if ok != QtWidgets.QMessageBox.Yes:
            return
        self.console.shutdown()
        self.console.reset_view()
        self._reset_console_ui()
        self.session_log.load(None, None)   # own kernel gone — clear the view
        self._launch_console()

    # ── Console lifecycle ───────────────────────────────────────────────────────

    def _launch_console(self) -> None:
        """Confirm Experiment/Setup file, then launch (cancel aborts entirely)."""
        from .launch_dialog import LaunchDialog

        dlg = LaunchDialog(self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        # Persist the experiment args so the embedded starter script picks them up.
        config.update({
            "dm_experiment": dlg.experiment(),
            "setup_file": dlg.setup_file(),
        })
        self._last_launch_experiment = dlg.experiment()
        self._launch_embedded()

    def _launch_embedded(self) -> None:
        typed = self._workdir.text().strip()
        # Expand before use: this path is about to be created, and an
        # unexpanded "~/runs" would make a literal "~" directory.
        work_dir = os.path.abspath(os.path.expanduser(typed)) if typed else _DEFAULT_LAUNCH_DIR
        try:
            os.makedirs(work_dir, exist_ok=True)
        except OSError as exc:
            self._set_toolbar_status(f"Cannot create {work_dir}: {exc}", error=True)
            return
        # Remember it for next time (per profile). Store "" when it is just
        # the Bluesky root, so an unchanged profile keeps no absolute path.
        config.update({
            "kernel_work_dir": "" if work_dir == _DEFAULT_LAUNCH_DIR else work_dir
        })
        self._workdir.setText(work_dir)
        self._launch_btn.setEnabled(False)
        self._attach_btn.setEnabled(False)
        self._set_toolbar_status("Starting IPython…", error=False)
        # Let the label paint before the (brief) kernel spin-up blocks.
        QtCore.QTimer.singleShot(0, lambda: self.console.start(cwd=work_dir))

    def _attach_console(self) -> None:
        """Reconnect to this beamline's running kernel (or a picked connection file)."""
        if self.console.is_running():
            self._set_toolbar_status("A session is already connected.", error=True)
            return
        cf = self.console.default_connection_file()
        if not cf or not os.path.exists(cf):
            # No running kernel for this beamline — let the user pick a file.
            start_dir = os.path.dirname(cf) if cf else os.path.expanduser("~")
            if not os.path.isdir(start_dir):
                start_dir = os.path.expanduser("~")
            cf, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "Select kernel connection file",
                start_dir,
                "Kernel connection (*.json)",
            )
            if not cf:
                return
        self._launch_btn.setEnabled(False)
        self._attach_btn.setEnabled(False)
        self._set_toolbar_status("Attaching to running kernel…")
        QtCore.QTimer.singleShot(0, lambda: self.console.attach(cf))
        # A connected-but-silent kernel is either busy (fine) or dead — check
        # once the connection has had time to settle.
        QtCore.QTimer.singleShot(4500, self._verify_attach)

    def _on_launch_blocked(self, info) -> None:
        """Launch refused because a kernel is already running — offer to attach."""
        self._reset_console_ui()
        info = info or {}
        detail = (
            f"session: {info.get('session_name', '?')}\n"
            f"host: {info.get('host', '?')}\n"
            f"started: {info.get('started', '?')}\n"
            f"hosted in: {info.get('hosted_in', '?')}"
        )
        ans = QtWidgets.QMessageBox.question(
            self,
            "Kernel already running",
            "A Bluesky kernel is already running for this beamline — only one is "
            f"allowed at a time.\n\n{detail}\n\nAttach to it instead?\n\n"
            "(To stop it: Console → Shut down kernel, or run\n"
            f"  python -m B_PILOT.kernel_session stop --beamline "
            f"{info.get('beamline', '')})",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes,
        )
        if ans == QtWidgets.QMessageBox.Yes:
            self._attach_console()
        else:
            self._set_toolbar_status("Launch cancelled — a kernel is already running.")

    def _on_console_started(self) -> None:
        attached = self.console.is_attached()
        # Show the full persistent history for this experiment (from disk,
        # newest first) and tail it live — this is where you see everything,
        # including activity from before this GUI and live output while the
        # kernel is busy.
        self.session_log.load(config.get("beamline"), self.console.experiment)
        self.report.load(config.get("beamline"), self.console.experiment)
        if attached:
            # Jump to the transcript so a reattached (possibly busy) kernel shows
            # activity immediately, instead of the blank interactive prompt.
            self._console_tabs.setCurrentWidget(self.session_log)
        self._workdir.setEnabled(False)
        self._profile_combo.setEnabled(False)
        self._launch_btn.setEnabled(False)
        self._attach_btn.setEnabled(False)
        # Restarting only works for a kernel we started (not an attached one).
        self._act_restart.setEnabled(not attached)
        self._act_shutdown.setEnabled(True)
        self._shutdown_btn.setEnabled(True)
        self.runner.set_console_ready(True)
        self._mode_btn.set_console_ready(True)
        self._contacq_btn.set_console_ready(True)
        self.run_controls.set_console_ready(True)
        self.mode_buttons.set_console_ready(True)
        self.report.set_console_ready(True)
        where = self._workdir.text().strip()
        if attached:
            self._set_toolbar_status(
                "Reattached — if the panel is blank the kernel is busy; "
                "it will respond when the running task finishes."
            )
            # The GUI never prompted for an experiment this session — read the
            # truth from the kernel itself rather than trust stale GUI config.
            self._set_experiment_banner("checking…", confirmed=False)
            self._start_experiment_probe()
        else:
            self._set_toolbar_status(f"IPython running in {where}")
            # Trusted immediately: this is exactly what LaunchDialog just wrote
            # to dm_experiment.txt before this kernel started.
            self._set_experiment_banner(self._last_launch_experiment, confirmed=True)
            # Startup commands are NOT dispatched here -- `started` fires as
            # soon as the client's channels are wired, before the kernel
            # handshake (see `_begin_handshake`) has actually completed, so
            # the console isn't reliably ready to accept input yet. See
            # `_on_console_ready` below, which fires once that handshake's
            # kernel_info_reply actually arrives.

    def _start_experiment_probe(self) -> None:
        """Poll the attached kernel for DM_EXP until it resolves, then stop.

        ``DM_EXP`` (defined in `instrument.devices.global_variables`) is only
        bound in the kernel namespace once Bluesky is loaded there — which
        may not have happened yet at attach time — so this retries rather
        than giving up after one failed query.
        """
        if getattr(self, "_exp_probe_timer", None) is None:
            self._exp_probe_timer = QtCore.QTimer(self)
            self._exp_probe_timer.setInterval(3000)
            self._exp_probe_timer.timeout.connect(self._probe_experiment)
        self._exp_probe_inflight = False
        self._exp_probe_timer.start()
        self._probe_experiment()

    def _probe_experiment(self) -> None:
        if self._exp_probe_inflight or not self.console.is_running():
            return
        self._exp_probe_inflight = True
        # Bare name, not a dotted `instrument.devices.global_variables.DM_EXP`
        # path: the kernel loads Bluesky via `from instrument.collection
        # import *`, which star-imports DM_EXP into the namespace but never
        # binds the `instrument` package name itself — the dotted form always
        # raised NameError, silently degrading to None on every poll.
        self.console.query_values(
            {"__dm_exp__": "DM_EXP"},
            self._on_experiment_probed,
        )

    def _on_experiment_probed(self, result: dict) -> None:
        self._exp_probe_inflight = False
        value = result.get("__dm_exp__")
        if value:
            self._set_experiment_banner(value, confirmed=True)
            self._exp_probe_timer.stop()
        else:
            self._set_experiment_banner(
                "unknown — Bluesky not loaded yet", confirmed=False
            )

    def _on_console_ready(self) -> None:
        """Kernel finished its handshake (idle) — safe to run and prompt visible."""
        if self.console.is_attached():
            self._set_toolbar_status("Reattached and ready.")
        else:
            # Fresh kernel only -- an attached one already went through this
            # under its own original launch (whatever state the profile's
            # startup commands established is part of that same
            # already-established IPython session). Deferred to here (rather
            # than `_on_console_started`) because this is the first point the
            # console is actually confirmed ready to accept input -- see the
            # comment in `_on_console_started`'s fresh-kernel branch.
            #
            # Also deferred by one event-loop tick (0 ms): this handler runs
            # synchronously nested inside the shell-channel message-received
            # callback that made `ready` fire in the first place (see
            # `_on_shell_msg`/`ready.emit()` in console_panel.py). Calling
            # `jupyter_widget.execute()` from there sends the request fine,
            # but the console visibly sat on the FIRST line until the user
            # pressed a key -- the widget's own screen update for that
            # request's reply wasn't flushed until the next real event-loop
            # iteration. Same idiom as `_start_console`/`_attach_console`
            # above for kernel-related dispatches.
            QtCore.QTimer.singleShot(0, self.console.run_startup_commands)

    def _verify_attach(self) -> None:
        """After attach settles: distinguish a busy kernel from a dead one."""
        if not self.console.is_running() or self.console.is_ready():
            return  # not attached, or already responded — nothing to warn about
        if self.console.is_alive():
            self._set_toolbar_status(
                "Reattached — kernel is busy (a task is running); it will "
                "respond when done."
            )
        else:
            self._set_toolbar_status(
                "Attached, but the kernel is not responding — it may have shut "
                "down. Use Console → Shut down, then Launch.",
                error=True,
            )

    def _on_attach_failed(self, reason: str) -> None:
        """Attach could not connect — restore the idle toolbar state."""
        self._reset_console_ui()
        QtWidgets.QMessageBox.warning(self, "Attach failed", reason)
        self._set_toolbar_status("Attach failed.", error=True)

    def _shutdown_kernel(self) -> None:
        """Explicitly terminate the kernel and return to the idle state."""
        self.run_controls.hide_recovery()
        ok = QtWidgets.QMessageBox.question(
            self,
            "Shut down kernel",
            "Terminate the IPython kernel?\n\n"
            "A killed kernel CANNOT be resumed later — all session state and any "
            "running plan will be lost (unlike closing the GUI, which leaves the "
            "kernel running to reattach).",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if ok != QtWidgets.QMessageBox.Yes:
            return
        self.console.shutdown()
        self.console.reset_view()
        self._reset_console_ui()
        self.session_log.load(None, None)   # own kernel gone — clear the view
        self._set_toolbar_status("Kernel shut down.")

    def _reset_console_ui(self) -> None:
        """Return the toolbar/menu to the pre-launch state."""
        self._workdir.setEnabled(True)
        self._profile_combo.setEnabled(True)
        self._launch_btn.setEnabled(True)
        self._attach_btn.setEnabled(True)
        self._act_restart.setEnabled(False)
        self._act_shutdown.setEnabled(False)
        self._shutdown_btn.setEnabled(False)
        self.runner.set_console_ready(False)
        self._mode_btn.set_console_ready(False)
        self._contacq_btn.set_console_ready(False)
        self.run_controls.set_console_ready(False)
        self.mode_buttons.set_console_ready(False)
        self.report.set_console_ready(False)
        self.session_log.stop()   # kernel gone — stop polling (keep text visible)
        self.report.stop()        # same: the report file on disk is untouched
        self._clear_experiment_banner()
        QtCore.QTimer.singleShot(0, self._refresh_attach_availability)

    def _refresh_attach_availability(self) -> None:
        """Color Attach green when a kernel is already live for the current
        beamline. Checked once per relevant event (startup, profile change,
        console-idle) -- see call sites -- never as a background poll.

        Guarded against firing while connected: none of the call sites can
        run while a console is up (profile switch is locked; __init__
        predates any console; reset-console-ui means idle by definition),
        but the is_running() check is kept here too as defense in depth.
        """
        if self.console.is_running():
            return
        cf = self.console.default_connection_file()
        if ks.is_alive(cf):
            self._attach_btn.setStyleSheet(S.status_button_qss(S.SUCCESS))
            self._attach_btn.setToolTip(self._ATTACH_TOOLTIP_FOUND)
        else:
            self._attach_btn.setStyleSheet("")
            self._attach_btn.setToolTip(self._ATTACH_TOOLTIP_IDLE)

    def _set_experiment_banner(self, value: str, *, confirmed: bool) -> None:
        """Show "Experiment:\\n<value>" in a bold banner above the console tabs.

        `confirmed` picks accent (trusted value) vs warning (not yet known)
        styling — see :meth:`_on_console_started` / :meth:`_on_experiment_probed`.
        """
        color = S.ACCENT if confirmed else S.WARNING
        self._exp_label.setStyleSheet(
            f"font-weight: 700; font-size: {S.px(13)}px; padding: 4px; "
            f"border-radius: 3px; background: {color}; color: white;"
        )
        self._exp_label.setText(f"Experiment:\n{value}")
        self._exp_label.setVisible(True)

    def _clear_experiment_banner(self) -> None:
        self._exp_label.setVisible(False)
        self._exp_label.setText("")
        if getattr(self, "_exp_probe_timer", None) is not None:
            self._exp_probe_timer.stop()

    def _toggle_viewer(self) -> None:
        """Launch the Bluesky data viewer as a detached, independent process,
        or report it's already running.

        The viewer is a fully separate process (not a dock/panel of this
        window), so its ribbon tab can only track *a* launched instance's
        liveness by polling its pid — best-effort, not true window focus.
        """
        if self._viewer_pid is not None and self._is_pid_alive(self._viewer_pid):
            self._set_toolbar_status("Viewer already running — look for its window.")
            return
        ok, pid = QtCore.QProcess.startDetached(
            sys.executable, ["-m", "B_PILOT.viewer"], paths.PKG_PARENT
        )
        if not ok:
            self._set_toolbar_status("Could not launch the viewer.", error=True)
            return
        self._set_toolbar_status("Opened Bluesky Viewer (separate window).")
        self._viewer_pid = pid
        self.ribbon.set_active("viewer", True)
        if self._viewer_poll_timer is None:
            self._viewer_poll_timer = QtCore.QTimer(self)
            self._viewer_poll_timer.setInterval(2000)
            self._viewer_poll_timer.timeout.connect(self._poll_viewer_alive)
        self._viewer_poll_timer.start()

    def _poll_viewer_alive(self) -> None:
        if self._viewer_pid is None or not self._is_pid_alive(self._viewer_pid):
            self._viewer_pid = None
            self.ribbon.set_active("viewer", False)
            self._viewer_poll_timer.stop()

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _on_runner_both_minimized(self, both: bool) -> None:
        """Let the console reclaim the runner's width when Plans-list and
        Plan-form are both minimized (and give it back when either restores).

        Minimizing just one of the two only redistributes space inside the
        runner's own inner splitter (unaffected by this) — only the "both"
        case shrinks the *outer* split's runner pane down to a thin strip.
        """
        if both:
            self._main_split_saved_sizes = self._main_split.sizes()
            total = sum(self._main_split_saved_sizes)
            runner_min = max(self.runner.minimumSizeHint().width(), S.px(40))
            self._main_split.setStretchFactor(0, 0)
            self._main_split.setSizes([runner_min, total - runner_min])
        else:
            self._main_split.setStretchFactor(0, 1)
            if self._main_split_saved_sizes is not None:
                self._main_split.setSizes(self._main_split_saved_sizes)
                self._main_split_saved_sizes = None

    def _set_toolbar_status(self, msg: str, *, error: bool = False) -> None:
        self._toolbar_status.setStyleSheet(f"color: {S.ERROR if error else S.MUTED};")
        self._toolbar_status.setText(msg)

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # noqa: N802
        """On close, keep or kill the kernel per config (see close_session)."""
        try:
            self.console.close_session()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)
