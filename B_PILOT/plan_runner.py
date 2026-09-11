"""Plan-runner panel: file browser + plan dropdown + parameter form + command.

The parameter form is built directly from each plan's docstring + signature
(via :mod:`plan_parser`, AST-only — nothing is imported), so any new plan is
picked up automatically.

Beyond the tk GUI it adds **live datatype validation** — numeric fields reject
non-numeric input and every field is checked on the fly (required / format),
with invalid fields flagged in red and the *Run* button gated on a valid form —
and **rich hover tooltips** describing each parameter.

Emits :pyattr:`runRequested` (a two-line ``from ... import ...`` +
``RE(plan(...))`` string) when the user clicks **Run**; the main window feeds
that into the embedded console.
"""
from __future__ import annotations

import ast
import html
import os
import re

from PyQt5 import QtCore
from PyQt5 import QtGui
from PyQt5 import QtWidgets

from . import command_builder
from . import config
from . import midas_bridge
from . import param_form
from . import plan_parser as P
from . import style as S
from .panel_ribbon import CollapsibleSplitterPanel
from .plan_parser import ParamSpec
from .skeleton_widgets import MotorAxisPicker
from .skeleton_widgets import MotorRowsWidget

# Plan name inside an ``RE(<plan>(...))`` command — mirrors queue_store._RE_PLAN,
# used to recover a queued command's plan name for `load_from_command`.
_RE_PLAN = re.compile(r"\bRE\(\s*([A-Za-z_]\w*)\s*\(")

# *args token-count-per-row for each scan_skeletons.py shape (see
# plan_parser.SKELETON_SHAPES / skeleton_widgets.py's module docstring) — used
# to chunk a queued command's leading positional tokens back into motor rows.
_SKELETON_ROW_WIDTH = {"list": 2, "list_grid": 2, "step": 3, "step_grid": 4}

# Preset "motors" object keys, in *args token order, for each skeleton shape --
# the JSON-side spelling of _SKELETON_ROW_WIDTH above (see the presets'
# README.md in the mpe_bluesky repo). A key a preset omits becomes a blank
# field the user fills in.
_SKELETON_ROW_KEYS = {
    "list":      ("motor", "positions"),
    "list_grid": ("motor", "positions"),
    "step":      ("motor", "start", "stop"),
    "step_grid": ("motor", "start", "stop", "nsteps"),
}


class PlanRunnerPanel(QtWidgets.QWidget):
    """File browser, plan selector, parameter form, and command builder."""

    # emitted with (command_text, run_notes) when the user clicks Run
    runRequested = QtCore.pyqtSignal(str, str)
    # emitted with (command_text, run_notes, qs_item) when the user clicks
    # Add to Queue -- qs_item is the structured dict for the QS-backed queue
    # (command_builder.make_queue_item()), or {} when none could be built
    # (hand-edited text, or an unsupported plan shape). Always populated
    # best-effort regardless of which backend is actually active, so
    # main_window._on_queue has what it needs without a second round trip.
    queueRequested = QtCore.pyqtSignal(str, str, dict)
    # emitted when Plans-list and Plan-form are both minimized, or when they
    # stop both being minimized -- lets the main window reclaim the freed
    # width for the console (see main_window._on_runner_both_minimized).
    bothPanelsMinimizedChanged = QtCore.pyqtSignal(bool)

    def __init__(self, parent=None, *, ribbon=None) -> None:
        """Build the panel and populate the file browser + plan dropdown.

        `ribbon` (a :class:`B_PILOT.panel_ribbon.PanelRibbon`), if given, wires
        a minimize button onto the file-browser and plan-form panels so they
        can be tucked into the main window's left-edge ribbon. Omitted in
        contexts that don't have one (there are none today, but keeps this
        panel usable standalone).
        """
        super().__init__(parent)
        self._ribbon = ribbon
        self._file_checks: dict[str, QtWidgets.QCheckBox] = {}
        self._plan_origins: dict[str, str] = {}
        self._plan_specs: dict[str, dict] = {}
        self._plan_list: list[str] = []
        self._param_widgets: dict[str, tuple] = {}
        self._current_params: list[ParamSpec] = []
        self._console_ready = False
        self._editing = False
        # scan_skeletons.py's six *args-based plans (see plan_parser.SKELETON_SHAPES):
        # a dedicated motor-rows widget replaces the ordinary per-ParamSpec field
        # for the bare `*args` (plan_opener/per_step/plan_closer are ordinary
        # `block`-dtype ParamSpecs and render in the normal grid below it). Reset
        # alongside every other param-grid rebuild in `_clear_param_grid`.
        self._skeleton: tuple[str, bool] | None = None
        self._motor_rows_widget: MotorRowsWidget | None = None
        # The scan preset currently applied to the form, or None for an
        # ordinary plan. A preset is pre-filled values for its `parent_plan`,
        # never a plan itself -- see plan_parser.find_preset_specs and
        # `_dispatch_name`. Reset in `_clear_param_grid` like the rest.
        self._preset: dict | None = None
        self._both_minimized_last = False
        # area_detector device name(s) bound in the most recently *composed*
        # (not hand-edited) command -- see _compose_lines / midas_bridge.py.
        self._last_area_detectors: list = []

        self._build_ui()
        self._populate_file_browser()
        self._refresh_plan_dropdown(preserve_selection=False)

    # ── UI construction ─────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        split = S.Splitter(QtCore.Qt.Horizontal)
        S.configure_splitter(split)
        outer.addWidget(split, 1)

        # ── Left: file browser card ─────────────────────────────────────────
        fb_card = S.make_card("User files")
        if self._ribbon is not None:
            fb_head = QtWidgets.QHBoxLayout()
            fb_head.setContentsMargins(0, 0, 0, 0)
            fb_head.addStretch(1)
            fb_min_btn = QtWidgets.QToolButton()
            fb_min_btn.setText("—")
            fb_min_btn.setAutoRaise(True)
            fb_min_btn.setToolTip("Minimize this panel to the ribbon")
            fb_head.addWidget(fb_min_btn)
            fb_card.body.addLayout(fb_head)
        self._fb_container = QtWidgets.QWidget()
        self._fb_layout = QtWidgets.QVBoxLayout(self._fb_container)
        self._fb_layout.setContentsMargins(2, 2, 2, 2)
        self._fb_layout.setSpacing(2)
        self._fb_layout.addStretch(1)
        fb_scroll = QtWidgets.QScrollArea()
        fb_scroll.setWidgetResizable(True)
        fb_scroll.setWidget(self._fb_container)
        fb_card.body.addWidget(fb_scroll, 1)
        refresh_btn = QtWidgets.QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_files)
        fb_card.body.addWidget(refresh_btn)
        fb_card.setMinimumWidth(S.px(170))
        split.addWidget(fb_card)
        if self._ribbon is not None:
            self._fb_collapsible = CollapsibleSplitterPanel(
                split, fb_card, self._ribbon, "plans", "Plans list",
                on_change=self._update_both_minimized,
            )
            fb_min_btn.clicked.connect(self._fb_collapsible.minimize)

        # ── Right: plan selector + params + command ─────────────────────────
        right = QtWidgets.QWidget()
        rlay = QtWidgets.QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(6)

        sel_row = QtWidgets.QHBoxLayout()
        sel_row.addWidget(S.LabelRight("Plan:"))
        self._plan_cb = S.NoScrollComboBox()
        self._plan_cb.setMinimumWidth(S.px(220))
        self._plan_cb.currentIndexChanged.connect(self._on_plan_change)
        sel_row.addWidget(self._plan_cb)
        history_btn = QtWidgets.QPushButton("🔍 History…")
        history_btn.setToolTip(
            "Browse past experiments and import a previously-run plan into this form."
        )
        history_btn.clicked.connect(self._open_history_search)
        sel_row.addWidget(history_btn)
        sel_row.addStretch(1)
        if self._ribbon is not None:
            form_min_btn = QtWidgets.QToolButton()
            form_min_btn.setText("—")
            form_min_btn.setAutoRaise(True)
            form_min_btn.setToolTip("Minimize this panel to the ribbon")
            sel_row.addWidget(form_min_btn)
        rlay.addLayout(sel_row)

        # Full-width row below the dropdown -- long descriptions used to be
        # squeezed into the leftover space beside the combo box; a dedicated
        # row lets them use the whole panel width instead.
        self._doc_lbl = QtWidgets.QLabel("")
        self._doc_lbl.setWordWrap(True)
        self._doc_lbl.setStyleSheet(f"color: {S.MUTED};")
        rlay.addWidget(self._doc_lbl)

        # Resizable stack: Parameters / Command / Run notes.  A vertical splitter
        # gives each panel a draggable divider so heights can be adjusted.
        vsplit = S.Splitter(QtCore.Qt.Vertical)
        S.configure_splitter(vsplit)

        # ── Parameters card (scrollable grid) ──
        param_card = S.make_card("Parameters   (hover a name · ★ = required)")
        self._param_host = QtWidgets.QWidget()
        self._param_grid = QtWidgets.QGridLayout(self._param_host)
        self._param_grid.setContentsMargins(4, 4, 4, 4)
        self._param_grid.setHorizontalSpacing(8)
        self._param_grid.setVerticalSpacing(6)
        self._param_grid.setColumnStretch(1, 1)
        param_scroll = QtWidgets.QScrollArea()
        param_scroll.setWidgetResizable(True)
        param_scroll.setWidget(self._param_host)
        param_card.body.addWidget(param_scroll)
        vsplit.addWidget(param_card)

        # ── Run notes card ── attached to the run on Run, then cleared.
        notes_card = S.make_card("Run notes   (attached to this run, then cleared)")
        self._notes = QtWidgets.QPlainTextEdit()
        self._notes.setPlaceholderText(
            "Notes about this run… attached to the Bluesky run on Run, then cleared."
        )
        self._notes.setMinimumHeight(S.px(40))
        self._notes.textChanged.connect(self._live_validate)
        notes_card.body.addWidget(self._notes)
        vsplit.addWidget(notes_card)

        # ── Command card (live, coloured) ──
        cmd_card = S.make_card("Command  (updates live · Run in console →, or Copy)")
        self._cmd_display = QtWidgets.QTextEdit()
        self._cmd_display.setObjectName("mono")
        self._cmd_display.setReadOnly(True)
        self._cmd_display.setMinimumHeight(S.px(44))
        self._cmd_display.setFont(QtGui.QFont(S.MONO_FAMILIES[0]))
        self._cmd_display.textChanged.connect(self._on_cmd_display_edited)
        cmd_card.body.addWidget(self._cmd_display)
        vsplit.addWidget(cmd_card)

        vsplit.setStretchFactor(0, 1)   # Parameters takes the slack by default
        vsplit.setStretchFactor(1, 0)
        vsplit.setStretchFactor(2, 0)
        vsplit.setSizes([420, 110, 130])
        rlay.addWidget(vsplit, 1)

        # ── Fixed action row (always visible below the resizable panels) ──
        btn_row = QtWidgets.QHBoxLayout()
        self._copy_btn = QtWidgets.QPushButton("Copy")
        self._copy_btn.clicked.connect(self._copy_command)
        self._edit_btn = QtWidgets.QPushButton("✎ Edit")
        self._edit_btn.setCheckable(True)
        self._edit_btn.setToolTip(
            "Hand-edit the command text before sending it — for edge cases the "
            "parameter form can't express. Toggling off discards edits and "
            "resyncs to the form."
        )
        self._edit_btn.toggled.connect(self._on_edit_toggled)
        self._add_btn = QtWidgets.QPushButton("Add to Queue")
        self._add_btn.setToolTip("Append this plan to the queue (bottom-right).")
        self._add_btn.clicked.connect(self._queue_command)
        self._add_btn.setEnabled(False)
        self._run_btn = S.primary_btn("▶  Run in console")
        self._run_btn.clicked.connect(self._run_command)
        self._run_btn.setEnabled(False)
        self._run_btn.setToolTip("Launch the IPython session first (top toolbar).")
        btn_row.addWidget(self._copy_btn)
        btn_row.addWidget(self._edit_btn)
        btn_row.addStretch(1)
        self._status_lbl = QtWidgets.QLabel("")
        self._status_lbl.setStyleSheet(f"color: {S.MUTED};")
        btn_row.addWidget(self._status_lbl)
        btn_row.addWidget(self._add_btn)
        btn_row.addWidget(self._run_btn)
        rlay.addLayout(btn_row)

        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([190, 560])
        if self._ribbon is not None:
            self._form_collapsible = CollapsibleSplitterPanel(
                split, right, self._ribbon, "planform", "Plan form",
                on_change=self._update_both_minimized,
            )
            form_min_btn.clicked.connect(self._form_collapsible.minimize)

    def _update_both_minimized(self) -> None:
        """Notify listeners when Plans-list + Plan-form's combined minimized
        state flips, so the main window can reclaim/return the freed width."""
        both = self._fb_collapsible.is_minimized and self._form_collapsible.is_minimized
        if both != self._both_minimized_last:
            self._both_minimized_last = both
            self.bothPanelsMinimizedChanged.emit(both)

    # ── Console-readiness (set by the main window) ──────────────────────────────

    def set_console_ready(self, ready: bool) -> None:
        """Enable/disable *Run* depending on whether the console is live."""
        self._console_ready = ready
        self._run_btn.setToolTip(
            "" if ready else "Launch the IPython session first (top toolbar)."
        )
        self._live_validate()

    # ── Edit mode (hand-edit the command before sending) ────────────────────────

    def _on_edit_toggled(self, editing: bool) -> None:
        self._editing = editing
        if editing:
            text = self._cmd_display.toPlainText()
            self._cmd_display.setPlainText(text)  # drop HTML colouring
            self._cmd_display.setReadOnly(False)
            self._cmd_display.setStyleSheet(f"border: 2px solid {S.ACCENT};")
            self._edit_btn.setStyleSheet(
                f"QPushButton{{background:{S.ACCENT};color:white;font-weight:bold;}}"
                f"QPushButton:hover{{background:{S.ACCENT_D};}}"
            )
            self._cmd_display.setFocus()
        else:
            self._cmd_display.setReadOnly(True)
            self._cmd_display.setStyleSheet("")
            self._edit_btn.setStyleSheet("")
            self._live_validate()  # discard edits, resync from the form

    def _on_cmd_display_edited(self) -> None:
        if self._editing:
            self._live_validate()

    def _force_exit_edit_mode(self) -> None:
        """Bail out of edit mode when the plan/form changes out from under it."""
        if self._editing:
            self._edit_btn.setChecked(False)  # triggers _on_edit_toggled(False)

    # ── File browser ────────────────────────────────────────────────────────────

    def _populate_file_browser(self) -> None:
        # Clear existing widgets (keep the trailing stretch).
        while self._fb_layout.count() > 1:
            item = self._fb_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        old = {p: cb.isChecked() for p, cb in self._file_checks.items()}
        self._file_checks.clear()

        plans_dir = config.get("plans_dir")
        default_file = config.get("default_plan_file")
        visible = set(config.get("visible_plan_files") or [])
        insert_at = 0
        # Stack of (display_name, depth) dir headers not yet shown. A dir at
        # depth d replaces any stacked entries at depth >= d (those were
        # sibling/uncle groups with no visible files); a visible file flushes
        # every remaining ancestor header, in root-to-leaf order, once.
        pending_stack: list[tuple[str, int]] = []
        for display_name, kind, abs_path, depth in P.scan_user_dir(plans_dir):
            # Any stacked header at depth >= this entry's depth belongs to a
            # sibling/uncle group we've now moved past (with no visible files
            # under it, or it would have been flushed and cleared already).
            while pending_stack and pending_stack[-1][1] >= depth:
                pending_stack.pop()
            if kind == "dir":
                pending_stack.append((display_name, depth))
                continue
            rel = os.path.relpath(abs_path, plans_dir).replace(os.sep, "/")
            if rel not in visible:
                continue
            for header, _ in pending_stack:
                lbl = QtWidgets.QLabel(f"📁 {header}")
                lbl.setStyleSheet(f"color: {S.MUTED};")
                self._fb_layout.insertWidget(insert_at, lbl)
                insert_at += 1
            pending_stack.clear()
            cb = QtWidgets.QCheckBox(display_name)
            if depth:
                cb.setStyleSheet(f"margin-left: {16 * depth}px;")
            checked = old.get(abs_path, display_name == default_file)
            cb.setChecked(checked)
            cb.toggled.connect(self._on_file_toggle)
            self._file_checks[abs_path] = cb
            self._fb_layout.insertWidget(insert_at, cb)
            insert_at += 1

    def _refresh_files(self) -> None:
        self._populate_file_browser()
        self._refresh_plan_dropdown(preserve_selection=True)

    def apply_config(self) -> None:
        """Re-scan the file browser / plan dropdown after a config change."""
        self._refresh_files()

    def has_plan(self, name: str) -> bool:
        """Whether `name` is currently selectable (its source file is checked)."""
        return name in self._plan_list

    # ── Import from persistent experiment history ────────────────────────────────

    def _open_history_search(self) -> None:
        """Browse every experiment's persistent history for this beamline and
        import a previously-run plan straight into this form (see
        :mod:`plan_history_dialog` / :mod:`experiment_history`)."""
        from .plan_history_dialog import PlanHistoryDialog

        dlg = PlanHistoryDialog(self)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            command = dlg.selected_command()
            if command:
                self.load_from_command(command)

    # ── Load from a queued command ("Copy to form") ─────────────────────────────

    def load_from_command(self, command: str) -> None:
        """Populate the form from a previously-generated ``RE(plan(...))`` command.

        Used by the queue panel's "Copy to form" action. `_make_re_line` always
        emits ordinary plan args as keywords (`name=value`) — only a
        scan_skeletons.py plan's leading `*args` (motor rows) are positional —
        which makes this a tractable, if best-effort, reverse of that method
        rather than a full Python-source interpreter. A field that can't be
        restored (e.g. the queue item was hand-edited) is left at its default
        and reported in the status line rather than aborting the whole load.
        """
        self._force_exit_edit_mode()
        match = _RE_PLAN.search(command or "")
        if not match:
            self._flash_status("Couldn't find a plan call in that command.")
            return
        plan_name = match.group(1)
        if plan_name not in self._plan_list:
            self._flash_status(f"Plan '{plan_name}' isn't in a currently-visible file.")
            return

        self._plan_cb.setCurrentText(plan_name)  # triggers _on_plan_change

        try:
            call = self._find_plan_call(command, plan_name)
        except SyntaxError:
            call = None
        if call is None:
            self._flash_status(f"Loaded '{plan_name}' — couldn't parse its arguments.")
            return

        skipped: list[str] = []
        if self._skeleton:
            self._apply_skeleton_args(call, skipped)
        for kw in call.keywords:
            if kw.arg is None or kw.arg not in self._param_widgets:
                continue  # **kwargs splat (never emitted here), or a skeleton-only name
            try:
                self._apply_param_value(kw.arg, kw.value)
            except Exception:  # noqa: BLE001 — one bad field shouldn't abort the rest
                skipped.append(kw.arg)

        self._live_validate()
        if skipped:
            self._flash_status(f"Loaded '{plan_name}' — couldn't restore: {', '.join(skipped)}")
        else:
            self._flash_status(f"Loaded '{plan_name}' from queue.")

    @staticmethod
    def _find_plan_call(command: str, plan_name: str) -> ast.Call | None:
        """Find the ``plan_name(...)`` call node nested inside the command's ``RE(...)``."""
        tree = ast.parse(command)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == plan_name
            ):
                return node
        return None

    def _apply_param_value(self, name: str, value_node: ast.expr) -> None:
        """Set `self._param_widgets[name]`'s widget from a parsed argument value."""
        spec, widget = self._param_widgets[name]
        if spec.dtype == "device" and spec.category == "motor" and not spec.motor_whole:
            # `motor.axis` (ast.Attribute) — or a bare motor (ast.Name) for an
            # axis-less device / a hand-edited command.
            if isinstance(value_node, ast.Attribute) and isinstance(
                value_node.value, ast.Name
            ):
                widget.set_from(value_node.value.id, value_node.attr)
            elif isinstance(value_node, ast.Name):
                widget.set_from(value_node.id, None)
            else:
                raise ValueError("expected a motor.axis reference")
        elif spec.dtype == "device_list" and spec.category == "motor":
            if not isinstance(value_node, (ast.List, ast.Tuple)):
                raise ValueError("expected a motor list")
            pairs: list[tuple[str, str | None]] = []
            for elt in value_node.elts:
                if isinstance(elt, ast.Attribute) and isinstance(elt.value, ast.Name):
                    pairs.append((elt.value.id, elt.attr))
                elif isinstance(elt, ast.Name):
                    pairs.append((elt.id, None))
            widget.set_from_pairs(pairs)
        elif spec.dtype == "device":
            if not isinstance(value_node, ast.Name):
                raise ValueError("expected a bare device reference")
            idx = widget.findText(value_node.id)
            if idx < 0:
                widget.addItem(value_node.id)
                idx = widget.count() - 1
            widget.setCurrentIndex(idx)
        elif spec.dtype == "device_list":
            if not isinstance(value_node, (ast.List, ast.Tuple)):
                raise ValueError("expected a device list")
            names = {elt.id for elt in value_node.elts if isinstance(elt, ast.Name)}
            for i in range(widget.count()):
                item = widget.item(i)
                item.setSelected(item.text() in names)
        elif spec.dtype == "bool":
            widget.setChecked(bool(ast.literal_eval(value_node)))
        elif spec.dtype == "positions":
            triples = ast.literal_eval(value_node)
            widget.setPlainText(
                "\n".join(", ".join(str(v) for v in triple) for triple in triples)
            )
        elif spec.dtype == "choice":
            widget.setCurrentText(str(ast.literal_eval(value_node)))
        elif spec.dtype == "block_list":
            if not isinstance(value_node, (ast.List, ast.Tuple)):
                raise ValueError("expected a list of function references")
            names = {elt.id for elt in value_node.elts if isinstance(elt, ast.Name)}
            for i in range(widget.count()):
                item = widget.item(i)
                item.setSelected(item.text() in names)
        elif spec.dtype == "block":
            if not isinstance(value_node, ast.Name):
                raise ValueError("expected a bare function reference")
            idx = widget.findText(value_node.id)
            if idx < 0:
                widget.addItem(value_node.id)
                idx = widget.count() - 1
            widget.setCurrentIndex(idx)
        elif spec.dtype == "code":
            # Restore the argument's SOURCE TEXT, not a literal value: a code
            # field can hold an expression `literal_eval` cannot evaluate at
            # all (`[scaler1, tc32E]` -- bare device names), which would
            # otherwise be dropped and silently revert the field to its
            # default. `unparse` also keeps a string value's quotes, which
            # `str(literal_eval(...))` would strip.
            widget.setText(ast.unparse(value_node))
        else:  # str / int / float / unknown -> line edit
            widget.setText(str(ast.literal_eval(value_node)))

    def _apply_skeleton_args(self, call: ast.Call, skipped: list[str]) -> None:
        """Restore the motor rows for a scan_skeletons.py plan.

        plan_opener/per_step/plan_closer are ordinary `block`-dtype ParamSpecs
        now, so they're restored by `load_from_command`'s generic keyword loop,
        not here.
        """
        shape, _relative = self._skeleton
        width = _SKELETON_ROW_WIDTH.get(shape)
        if width and call.args:
            try:
                tokens = [ast.unparse(node) for node in call.args]
                rows = [tokens[i : i + width] for i in range(0, len(tokens), width)]
                self._motor_rows_widget.load_rows(rows)
            except Exception:  # noqa: BLE001
                skipped.append("Motors")

    def _on_file_toggle(self, _checked: bool) -> None:
        self._refresh_plan_dropdown(preserve_selection=True)

    # ── Plan dropdown ─────────────────────────────────────────────────────────────

    def _refresh_plan_dropdown(self, preserve_selection: bool = True) -> None:
        old = self._plan_cb.currentText() if preserve_selection else ""
        self._plan_origins.clear()
        self._plan_specs.clear()
        self._plan_list.clear()

        import_root = config.get("import_root")
        for abs_path, cb in self._file_checks.items():
            if not cb.isChecked():
                continue
            module = P.file_to_module(abs_path, import_root)
            for name, spec in P.find_plan_specs(abs_path, import_root).items():
                if name not in self._plan_specs:
                    self._plan_specs[name] = spec
                    # A preset dispatches its PARENT plan, so the import line
                    # has to name the parent's module -- not the preset's own
                    # path, which is a .json and imports nothing.
                    dispatch = spec.get("dispatch")
                    self._plan_origins[name] = dispatch["module"] if dispatch else module
                    self._plan_list.append(name)

        self._plan_cb.blockSignals(True)
        self._plan_cb.clear()
        self._plan_cb.addItems(self._plan_list)
        self._plan_cb.blockSignals(False)

        if self._plan_list:
            keep = preserve_selection and old in self._plan_list
            idx = self._plan_list.index(old) if keep else 0
            self._plan_cb.setCurrentIndex(idx)
            self._on_plan_change()
        else:
            self._doc_lbl.setText("(no plans — check a .py file on the left)")
            self._rebuild_param_form([])
            self._set_cmd_text("(no plan selected)")

    # ── Parameter form ────────────────────────────────────────────────────────────

    def _on_plan_change(self, *_) -> None:
        self._force_exit_edit_mode()
        plan_name = self._plan_cb.currentText()
        if not plan_name:
            return
        spec = self._plan_specs.get(plan_name)
        module = self._plan_origins.get(plan_name, "")
        fallback = f"from {module}" if module else ""
        skeleton = spec.get("skeleton") if spec else None
        if skeleton:
            # scan_skeletons.py plan (see plan_parser.SKELETON_SHAPES) — routes here
            # even once `documented` is True, since the composite form replaces the
            # ordinary grid for *args/plan_opener/per_step/plan_closer regardless.
            self._doc_lbl.setText(spec["summary"] or fallback)
            self._current_params = spec["params"]
            self._rebuild_skeleton_form(skeleton, spec["params"])
        elif spec and spec["documented"]:
            self._doc_lbl.setText(spec["summary"] or fallback)
            self._current_params = spec["params"]
            self._rebuild_param_form(spec["params"])
        else:
            summary = spec["summary"] if spec else ""
            self._doc_lbl.setText(summary or fallback)
            self._current_params = []
            self._rebuild_generic_form()
        # A preset pre-fills the form just built for its parent plan. Must run
        # after the rebuild, which clears `self._preset` along with the grid.
        if spec and spec.get("preset"):
            self._apply_preset(spec["preset"])
        self._live_validate()   # marks fields, gates Run, and renders the command

    def _clear_param_grid(self) -> None:
        while self._param_grid.count():
            item = self._param_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._param_widgets.clear()
        # Composite skeleton widgets live in this same grid (cleared above) — drop
        # the Python-side references too so a stale MotorRowsWidget/combo can't be
        # read after switching to a different plan.
        self._skeleton = None
        self._motor_rows_widget = None
        self._preset = None

    def _rebuild_param_form(self, params: list[ParamSpec], row_offset: int = 0) -> None:
        """Build the ordinary per-`ParamSpec` grid, starting at grid row `row_offset`.

        `row_offset` lets `_rebuild_skeleton_form` place the motor-rows widget
        in the row above this one without a second grid; every other call site
        keeps the default (row 0) unchanged. Widget building/dtype logic lives
        in :mod:`param_form`, shared with the switch-to/cont-acq popups.
        """
        if row_offset == 0:
            self._clear_param_grid()
        self._param_widgets.update(
            param_form.build_grid(self._param_grid, params, self._live_validate, row_offset)
        )

    def _rebuild_skeleton_form(self, skeleton: tuple[str, bool], params: list[ParamSpec]) -> None:
        """Composite form for a scan_skeletons.py plan: motor rows (row 0), then
        the ordinary docstring-driven kwargs below (row 1+) — including
        plan_opener/per_step/plan_closer, which are ordinary `block`-dtype
        ParamSpecs and need no special handling here.

        See plan_parser.SKELETON_SHAPES for `skeleton` = (shape, relative), and
        skeleton_widgets.MotorRowsWidget for the motor-rows widget itself.
        """
        self._clear_param_grid()
        shape, relative = skeleton
        self._skeleton = skeleton

        self._motor_rows_widget = MotorRowsWidget(shape, relative)
        self._motor_rows_widget.changed.connect(self._live_validate)
        motors_lbl = S.LabelRight("Motors:  ★")
        motors_lbl.setWordWrap(True)
        S.HoverTip(
            motors_lbl,
            "The plan's *args — one or more motors, each with its own "
            "position(s)/range. Not documentable via the ordinary Parameters "
            "grammar (a bare *args can't be bound to a single field), so it gets "
            "this dedicated widget instead.",
        )
        self._param_grid.addWidget(motors_lbl, 0, 0)
        self._param_grid.addWidget(self._motor_rows_widget, 0, 1)

        self._rebuild_param_form(params, row_offset=1)

    # ── Scan presets ──────────────────────────────────────────────────────────────

    @staticmethod
    def _preset_value_node(spec: ParamSpec, value) -> ast.expr:
        """A JSON preset value as the AST node `_apply_param_value` expects.

        Going through AST rather than adding a second set of per-dtype widget
        setters means presets and "Copy to form" share exactly one code path.
        Device/block values are *names* in the running session (``pg6``,
        ``samE.y``, ``tomo_sweep_``) so they are parsed as expressions; every
        other dtype is an ordinary literal and goes through ``repr()``.
        """
        if spec.dtype in ("device", "block"):
            return ast.parse(str(value), mode="eval").body
        if spec.dtype in ("device_list", "block_list"):
            items = value if isinstance(value, (list, tuple)) else [value]
            return ast.parse(
                "[" + ", ".join(str(i) for i in items) + "]", mode="eval"
            ).body
        return ast.parse(repr(value), mode="eval").body

    def _apply_preset(self, preset: dict) -> None:
        """Fill the parent plan's form in from `preset` (see its README.md).

        A key the preset omits is deliberately left blank / at the plan's own
        default, so this only ever *sets* fields -- it never clears one the
        rebuild already populated with a signature default.
        """
        self._preset = preset
        skipped: list[str] = []

        # -- motor rows (the parent plan's *args) --
        if self._skeleton and self._motor_rows_widget is not None:
            shape, _relative = self._skeleton
            keys = _SKELETON_ROW_KEYS.get(shape)
            rows = preset.get("motors") or []
            if keys and rows:
                token_rows = []
                for row in rows:
                    row = row if isinstance(row, dict) else {}
                    tokens = []
                    for key in keys:
                        value = row.get(key)
                        if value is None:
                            tokens.append("")          # user fills this in
                        elif isinstance(value, (list, tuple)):
                            tokens.append("[" + ", ".join(str(v) for v in value) + "]")
                        else:
                            tokens.append(str(value))
                    token_rows.append(tokens)
                try:
                    self._motor_rows_widget.load_rows(token_rows)
                except Exception:  # noqa: BLE001 — one bad row shouldn't kill the form
                    skipped.append("Motors")

        # -- keyword values --
        for name, value in (preset.get("values") or {}).items():
            if name not in self._param_widgets:
                skipped.append(name)   # renamed/removed parameter in a stale preset
                continue
            spec, _widget = self._param_widgets[name]
            try:
                self._apply_param_value(name, self._preset_value_node(spec, value))
            except Exception:  # noqa: BLE001
                skipped.append(name)

        # -- fields this preset makes mandatory --
        # Rewriting the ParamSpec (rather than special-casing `required` in
        # _live_validate/_parse_params) means the existing validation and value
        # extraction pick it up with no new logic. Build a new list so the
        # cached parent-plan spec shared with every other preset is untouched.
        required = set(preset.get("required") or ())
        if required:
            promoted = []
            for spec in self._current_params:
                if spec.name in required and not spec.required:
                    spec = spec._replace(required=True, blank_omits=False)
                    if spec.name in self._param_widgets:
                        self._param_widgets[spec.name] = (
                            spec, self._param_widgets[spec.name][1],
                        )
                promoted.append(spec)
            self._current_params = promoted

        if skipped:
            self._flash_status(
                f"Preset '{preset.get('name', '?')}' — couldn't apply: "
                f"{', '.join(skipped)}"
            )

    def _dispatch_name(self) -> str:
        """The plan the generated command actually calls.

        Normally the selected plan. For a preset it is the `parent_plan` the
        preset fills in — a preset's own name is a GUI label and never reaches
        the RunEngine or the queueserver.
        """
        plan_name = self._plan_cb.currentText()
        spec = self._plan_specs.get(plan_name)
        dispatch = spec.get("dispatch") if spec else None
        return dispatch["plan"] if dispatch else plan_name

    def _preset_md(self) -> dict:
        """Provenance metadata for the active preset (``{}`` when none).

        Lands in the run's start document, so a scan run from a preset can be
        traced back to it even though the dispatched command names only the
        skeleton plan.
        """
        if not self._preset:
            return {}
        return {"preset": self._preset.get("name")}

    def _rebuild_generic_form(self) -> None:
        self._clear_param_grid()
        lbl = QtWidgets.QLabel("Arguments  (Python syntax, comma-separated):")
        self._param_grid.addWidget(lbl, 0, 0, 1, 2)
        txt = QtWidgets.QPlainTextEdit()
        txt.setObjectName("mono")
        txt.setFixedHeight(S.px(90))
        txt.setPlaceholderText("file_name='test', p_start=-5, p_end=5")
        txt.textChanged.connect(self._live_validate)
        self._param_grid.addWidget(txt, 1, 0, 1, 2)
        self._param_widgets["__args__"] = ("generic", txt)
        self._param_grid.setRowStretch(2, 1)

    # ── Validation ────────────────────────────────────────────────────────────────

    def _field_error(self, spec: ParamSpec, widget) -> str | None:
        """Return an error string for `widget`, or None when it is acceptable.

        Dtype-specific logic lives in :func:`param_form.field_error`, shared
        with the switch-to/cont-acq popups.
        """
        return param_form.field_error(spec, widget)

    def _skeleton_errors(self) -> list[str]:
        """Errors for the dedicated motor-rows widget.

        Empty list when no skeleton is active, or the widget is valid. Shared by
        `_live_validate` (form-field flagging) and `_parse_params` (value
        extraction) so the two can't drift out of sync. plan_opener/per_step/
        plan_closer are ordinary `block`-dtype ParamSpecs now, so their errors
        come from the normal per-spec loop, not from here.
        """
        if not self._skeleton:
            return []
        return [f"Motors: {e}" for e in self._motor_rows_widget.errors()]

    def _live_validate(self) -> None:
        """Re-check every field, flag invalid ones, and gate the Run button."""
        errors: list[str] = []
        if "__args__" not in self._param_widgets:
            errors.extend(self._skeleton_errors())
            for spec in self._current_params:
                widget = self._param_widgets[spec.name][1]
                err = self._field_error(spec, widget)
                if isinstance(widget, MotorAxisPicker):
                    # Flag the specific combo: motor if unchosen, else axis.
                    bad = err is not None
                    S.mark_invalid(widget.motor_cb, bad and not widget.motor())
                    S.mark_invalid(widget.axis_cb, bad and bool(widget.motor()))
                # bool / device_list have no single-border widget to flag
                elif spec.dtype not in ("bool", "device_list"):
                    S.mark_invalid(widget, err is not None)
                if err:
                    errors.append(err)

        if self._editing:
            # The command box is user-owned text now — never rebuild it here,
            # and gate on non-blank text rather than per-field validity.
            has_text = bool(self._cmd_display.toPlainText().strip())
            self._run_btn.setEnabled(self._console_ready and has_text)
            # No structured ParamSpec/value snapshot exists for hand-edited
            # text, so no QS queue item can be built from it -- stays
            # disabled while editing only for the QS backend (the native
            # backend queues raw command text just fine, unchanged).
            qs_backend = config.get("queue_backend") == "qs"
            self._add_btn.setEnabled(has_text and not qs_backend)
            self._add_btn.setToolTip(
                "Not available while hand-editing the command — queuing via "
                "the queue server needs the structured form values. Use Run "
                "instead, or stop editing (✎) to queue the form as composed."
                if qs_backend else
                "Append this plan to the queue (bottom-right)."
            )
            self._edit_btn.setEnabled(True)
            self._status_lbl.setText("" if has_text else "⚠ command is empty")
            self._status_lbl.setToolTip("")
            return
        self._add_btn.setToolTip("Append this plan to the queue (bottom-right).")

        self._run_btn.setEnabled(self._console_ready and not errors)
        # Add-to-queue needs only a valid form (you can build the queue before
        # launching IPython; the scheduler dispatches once the console is up).
        self._add_btn.setEnabled(not errors)
        # Can't start hand-editing an invalid/placeholder command.
        self._edit_btn.setEnabled(not errors)
        if errors:
            n = len(errors)
            self._status_lbl.setText(f"⚠ {n} field{'s' if n > 1 else ''} to fix")
            self._status_lbl.setToolTip("\n".join(errors))
        else:
            self._status_lbl.setText("")
            self._status_lbl.setToolTip("")

        self._refresh_command(has_errors=bool(errors))

    # ── Parameter parsing ─────────────────────────────────────────────────────────

    def _parse_params(self) -> tuple[dict | None, list[str]]:
        """Extract `{name: value}` from the current form, plus validation
        errors. Per-dtype extraction lives in :func:`param_form.parse_values`,
        shared with the switch-to/cont-acq popups; the skeleton (`*args`) and generic
        (`__args__`) shapes are plan-runner-specific and handled here.
        """
        plan_name = self._plan_cb.currentText()
        if not plan_name:
            return None, ["No plan selected."]
        if "__args__" in self._param_widgets:
            _, txt = self._param_widgets["__args__"]
            return {"__args__": txt.toPlainText().strip()}, []

        values: dict = {}
        errors: list[str] = []

        if self._skeleton:
            skeleton_errors = self._skeleton_errors()
            errors.extend(skeleton_errors)
            if not skeleton_errors:
                # Already-rendered source fragments (bare motor names, numeric
                # literals, "[..]" list literals) — spliced verbatim as leading
                # positional args in command_builder.make_re_line, no repr()/
                # RawCode needed here.
                values["__positional__"] = self._motor_rows_widget.tokens()

        param_values, param_errors = param_form.parse_values(
            self._current_params, self._param_widgets
        )
        values.update(param_values)
        errors.extend(param_errors)
        return values, errors

    # ── Command generation ───────────────────────────────────────────────────────

    def _make_import_line(self, plan_name: str) -> str:
        # Fallback to instrument.collection when the plan's source module is
        # unknown: the MPE session loads `from instrument.collection import *`,
        # which re-exports every plan, so this import resolves for any real plan.
        module = self._plan_origins.get(plan_name, "instrument.collection")
        # For a preset both of these describe the PARENT plan: the module came
        # from its `dispatch` in `_refresh_plan_dropdown`, the name from here.
        return command_builder.make_import_line(self._dispatch_name(), module)

    def _make_re_line(self, plan_name: str, values: dict, notes: str = "") -> str:
        return command_builder.make_re_line(
            self._dispatch_name(), self._current_params, values, notes,
            extra_md=self._preset_md(),
        )

    def _compose_lines(self) -> tuple[str, str] | tuple[None, None]:
        """Return (import_line, re_line) if the form is valid, else (None, None)."""
        plan_name = self._plan_cb.currentText()
        if not plan_name:
            return None, None
        values, errors = self._parse_params()
        if errors:
            return None, None
        self._last_area_detectors = midas_bridge.area_detector_devices(
            self._current_params, values
        )
        notes = self._notes.toPlainText().strip()
        return (
            self._make_import_line(plan_name),
            self._make_re_line(plan_name, values, notes),
        )

    def _refresh_command(self, has_errors: bool) -> None:
        """Re-render the command preview.  Called live on every change."""
        if not self._plan_cb.currentText():
            self._set_cmd_text("(no plan selected)")
            return
        if has_errors:
            self._set_cmd_text("(fix the highlighted fields to build the command)")
            return
        import_line, re_line = self._compose_lines()
        if not re_line:
            self._set_cmd_text("(fill in the parameters above)")
            return
        self._set_cmd_colored(import_line, re_line)

    def _set_cmd_text(self, text: str) -> None:
        """Show a plain (muted) message in the command box."""
        self._cmd_display.setPlainText(text)

    def _set_cmd_colored(self, import_line: str, re_line: str) -> None:
        """Show the two-line command with the import and RE lines coloured."""
        doc = (
            f'<div style="color:{S.CMD_IMPORT}; white-space:pre-wrap;">'
            f"{html.escape(import_line)}</div>"
            f'<div style="color:{S.CMD_RE}; white-space:pre-wrap;">'
            f"{html.escape(re_line)}</div>"
        )
        self._cmd_display.setHtml(doc)

    def _command_text(self) -> str | None:
        """The text to send/copy: the hand-edited box verbatim while editing,
        else the form-composed two-line command."""
        if self._editing:
            # No reliable ParamSpec/value snapshot exists for hand-edited text
            # -- don't trigger the MIDAS bridge off a stale composed value.
            self._last_area_detectors = []
            text = self._cmd_display.toPlainText().strip()
            return text or None
        import_line, re_line = self._compose_lines()
        if not re_line:
            return None
        return f"{import_line}\n{re_line}"

    def last_dispatch_area_detector_devices(self) -> list:
        """area_detector device name(s) bound in the command last produced by
        `_command_text()` -- `[]` for a hand-edited command or one with no
        such param. Used by main_window to trigger the MIDAS_GUI live-view
        bridge only for dispatches that actually came from this panel."""
        return self._last_area_detectors

    def _copy_command(self) -> None:
        text = self._command_text()
        if not text:
            self._flash_status("Nothing to copy — fix fields first.")
            return
        QtWidgets.QApplication.clipboard().setText(text)
        self._flash_status("Copied.")

    def _run_command(self) -> None:
        text = self._command_text()
        if not text:
            return
        notes = self._notes.toPlainText().strip()
        self.runRequested.emit(text, notes)
        # The notes' job is done once the run is launched — clear them.
        self._notes.clear()
        if self._editing:
            self._flash_status(
                "Sent to console (edited)." + (
                    " Notes NOT attached — add md={'notes': ...} yourself."
                    if notes else ""
                )
            )
        else:
            self._flash_status(
                "Sent to console." + (" Notes attached & cleared." if notes else "")
            )

    def _queue_command(self) -> None:
        text = self._command_text()
        if not text:
            return
        notes = self._notes.toPlainText().strip()
        qs_item: dict = {}
        # Only ever built for the QS backend -- make_queue_item() is QS-
        # specific machinery the native backend has no use for, so it must
        # never run (and never be able to fail) for a native-only session,
        # regardless of what shape the target mpe_bluesky checkout's plans
        # happen to be in.
        if config.get("queue_backend") == "qs" and not self._editing:
            values, errors = self._parse_params()
            if not errors and values:
                qs_item = command_builder.make_queue_item(
                    self._dispatch_name(), self._current_params, values,
                    extra_md=self._preset_md(),
                ) or {}
        if config.get("queue_backend") == "qs" and not qs_item:
            # Hand-edited text or an unsupported plan shape (the generic
            # "__args__" fallback) has no structured QS item -- _live_validate
            # already disables Add-to-Queue while editing for this backend,
            # but an undocumented plan can still reach here while not editing.
            self._flash_status("Can't queue this plan shape via the queue server.")
            return
        self.queueRequested.emit(text, notes, qs_item)
        self._notes.clear()
        if self._editing:
            self._flash_status(
                "Added to queue (edited)." + (
                    " Notes NOT attached — add md={'notes': ...} yourself."
                    if notes else ""
                )
            )
        else:
            self._flash_status("Added to queue." + (" Notes attached." if notes else ""))

    def _flash_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)
        QtCore.QTimer.singleShot(3000, self._live_validate)
