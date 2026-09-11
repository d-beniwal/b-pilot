"""Configuration dialog: a profile bar on top, tabbed pages below.

Tabs: Paths, Plans, Launch Session, Devices, Scan blocks, Queue Backend,
Data Viewer, Appearance — one page each,
selected via a left-hand list (`QListWidget` + `QStackedWidget`). A profile
bar above the tabs lets you switch which on-disk profile
(`b-pilot/profiles/<name>/{default_config.json,active_config.json}`) you're
editing; see :mod:`config` for the profile lifecycle — `default_config.json`
is the shared, git-committed baseline for that beamline, `active_config.json`
is the live, per-workstation settings actually used day to day. Selecting a
different profile loads its *active* values into the form for
editing/preview only — nothing is written to disk or made active until
*Save*, same as every other field here. *Restore Defaults* previews the
profile's `default_config.json` instead; *Save as Default* is the only
action that writes back to it.

Reachable from the main window's **Python → Configuration…** menu. Edits are
written through :mod:`config` on *Save*; the caller then refreshes the panels
so changes take effect immediately (no restart) — except **UI scale**, which
is read once at startup (see :mod:`app`) and needs a relaunch to apply.
"""
from __future__ import annotations

import os

from PyQt5 import QtCore, QtWidgets

from . import autopilot_bridge
from . import config
from . import device_discovery as ddisc
from . import device_source
from . import paths as _paths
from . import plan_parser as P
from . import qs_client
from . import scan_building_discovery as sdisc
from . import style as S


class _CategoryDropdowns(QtWidgets.QWidget):
    """One-or-more category dropdowns for a single device row.

    Each dropdown is an editable `QComboBox` pre-populated with every
    category currently known across the profile (still free-text, so a
    brand-new category can be typed in) — replaces the old single
    comma-separated text field with an explicit widget per assigned
    category, plus a "+" to add another when a device should appear under
    more than one. `on_change` fires with the resulting ordered list of
    non-blank category strings whenever a dropdown's value is committed
    (item picked, or Enter/blur after typing) or a row is added/removed.
    """

    def __init__(self, categories, all_categories, on_change, parent=None) -> None:
        super().__init__(parent)
        self._all_categories = all_categories
        self._on_change = on_change
        self._rows: list[tuple[QtWidgets.QComboBox, QtWidgets.QToolButton]] = []

        self._lay = QtWidgets.QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(4)

        for cat in (categories or [""]):
            self._add_row(cat)
        # Baseline for _emit()'s change check — see its docstring for why this
        # matters: without it, every focus-out (e.g. clicking a *different*
        # dropdown) would look like a change and trigger a rebuild.
        self._last_emitted = self._current_cats()

        self._add_btn = QtWidgets.QToolButton()
        self._add_btn.setText("+")
        self._add_btn.setToolTip("Add another category for this device")
        self._add_btn.clicked.connect(lambda: self._add_row(""))
        self._lay.addWidget(self._add_btn)

    def _add_row(self, value: str) -> None:
        combo = S.NoScrollComboBox()
        combo.setEditable(True)
        combo.setMinimumWidth(S.px(130))
        combo.addItems(self._all_categories)
        combo.setCurrentText(value)
        combo.activated.connect(lambda _i: self._emit())
        combo.lineEdit().editingFinished.connect(self._emit)

        remove_btn = QtWidgets.QToolButton()
        remove_btn.setText("×")
        remove_btn.setToolTip("Remove this category")
        remove_btn.clicked.connect(lambda: self._remove_row(combo, remove_btn))

        # Insert before the "+" button once it exists; append during __init__.
        insert_at = self._lay.indexOf(self._add_btn) if hasattr(self, "_add_btn") else self._lay.count()
        self._lay.insertWidget(insert_at, combo)
        self._lay.insertWidget(insert_at + 1, remove_btn)
        self._rows.append((combo, remove_btn))

    def _remove_row(self, combo: QtWidgets.QComboBox, btn: QtWidgets.QToolButton) -> None:
        if len(self._rows) <= 1:
            return  # always keep at least one dropdown (blank = auto-detected)
        self._rows = [(c, b) for c, b in self._rows if c is not combo]
        combo.deleteLater()
        btn.deleteLater()
        self._emit()

    def _current_cats(self) -> list[str]:
        return [c for c in (combo.currentText().strip() for combo, _ in self._rows) if c]

    def _emit(self) -> None:
        """Fire `on_change` only when the category list actually changed.

        `editingFinished` fires on *any* focus-out of an editable combo's
        line edit, not just when its text changed — so merely clicking a
        different dropdown (e.g. to open its popup) would otherwise look
        like a commit here and trigger a full device-list rebuild via
        `on_change`. That rebuild, landing mid-click on the next combo,
        is what made every dropdown's popup collapse the instant it opened.
        """
        cats = self._current_cats()
        if cats == self._last_emitted:
            return
        self._last_emitted = cats
        self._on_change(cats)


class ConfigDialog(QtWidgets.QDialog):
    """Modal dialog: profile bar + tabbed Paths/Plans/Launch Session/Devices/Appearance."""

    def __init__(self, parent=None) -> None:
        """Build every tab's widgets once, then populate them from the active profile."""
        super().__init__(parent)
        self.setWindowTitle("Configuration")
        self.setMinimumWidth(S.px(760))
        self.setMinimumHeight(S.px(520))

        self._current_profile = config.active_profile()

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        outer.addLayout(self._build_profile_bar())

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(8)

        self._tab_list = QtWidgets.QListWidget()
        self._tab_list.setFixedWidth(S.px(140))
        self._stack = QtWidgets.QStackedWidget()

        # Build order matters (Plans depends on Paths' plans_dir field); it
        # happens to match the desired tab display order too.
        pages = [
            ("Paths", self._page(self._build_files_card())),
            ("Plans", self._page(self._build_visibility_card())),
            ("Launch Session", self._page(self._build_launch_card(), self._build_session_card())),
            ("Devices", self._page(self._build_devices_card())),
            ("Scan blocks", self._page(self._build_scan_blocks_card())),
            ("Queue Backend", self._page(self._build_queue_backend_card())),
            ("Data Viewer", self._page(self._build_data_viewer_card())),
            ("Reports", self._page(self._build_reports_card())),
            ("Appearance", self._page(
                self._build_appearance_card(), self._build_autopilot_card()
            )),
        ]
        for title, page in pages:
            self._tab_list.addItem(title)
            self._stack.addWidget(page)
        self._tab_list.currentRowChanged.connect(self._stack.setCurrentIndex)

        body.addWidget(self._tab_list)
        body.addWidget(self._stack, 1)
        outer.addLayout(body, 1)

        outer.addWidget(self._build_buttons())

        self._load_from(config.as_dict())
        self._tab_list.setCurrentRow(0)

    @staticmethod
    def _page(*widgets: QtWidgets.QWidget) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        for w in widgets:
            layout.addWidget(w)
        layout.addStretch(1)
        return page

    # ── Profile bar ──────────────────────────────────────────────────────────────

    def _build_profile_bar(self) -> QtWidgets.QLayout:
        row = QtWidgets.QHBoxLayout()
        row.addWidget(S.LabelRight("Profile:"))
        self._profile_combo = S.NoScrollComboBox()
        self._profile_combo.setMinimumWidth(S.px(160))
        self._profile_combo.setToolTip(
            "Beamline configuration profile. Each profile is a folder "
            "(profiles/<name>/) holding a shared default_config.json and a "
            "live active_config.json — paths, plans, launch/session commands, "
            "devices, and appearance all travel together."
        )
        self._refresh_profile_combo()
        self._profile_combo.currentTextChanged.connect(self._on_profile_selected)
        row.addWidget(self._profile_combo)

        new_btn = QtWidgets.QPushButton("New…")
        new_btn.setToolTip("Create a new profile, cloned from the one currently shown.")
        new_btn.clicked.connect(self._new_profile)
        save_as_btn = QtWidgets.QPushButton("Save As…")
        save_as_btn.setToolTip("Save the current form values as a new profile.")
        save_as_btn.clicked.connect(self._save_profile_as)
        save_default_btn = QtWidgets.QPushButton("Save as Default")
        save_default_btn.setToolTip(
            "Overwrite this profile's shared default_config.json with the "
            "current form values. This is the git-committed baseline other "
            "workstations reset to — use deliberately."
        )
        save_default_btn.clicked.connect(self._save_as_default)
        delete_btn = QtWidgets.QPushButton("Delete")
        delete_btn.setToolTip("Delete the selected profile (at least one must remain).")
        delete_btn.clicked.connect(self._delete_profile)

        row.addWidget(new_btn)
        row.addWidget(save_as_btn)
        row.addWidget(save_default_btn)
        row.addWidget(delete_btn)
        row.addStretch(1)
        return row

    def _refresh_profile_combo(self) -> None:
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        self._profile_combo.addItems(config.list_profiles())
        idx = self._profile_combo.findText(self._current_profile)
        self._profile_combo.setCurrentIndex(max(0, idx))
        self._profile_combo.blockSignals(False)

    def _on_profile_selected(self, name: str) -> None:
        if not name or name == self._current_profile:
            return
        self._current_profile = name
        self._load_from(config.profile_values(name))

    def _new_profile(self) -> None:
        name, ok = QtWidgets.QInputDialog.getText(self, "New profile", "Profile name:")
        name = name.strip()
        if not ok or not name:
            return
        try:
            config.new_profile(name, clone_from=self._current_profile)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "New profile", str(exc))
            return
        self._current_profile = name
        self._refresh_profile_combo()
        self._load_from(config.profile_values(name))

    def _save_profile_as(self) -> None:
        name, ok = QtWidgets.QInputDialog.getText(self, "Save profile as", "Profile name:")
        name = name.strip()
        if not ok or not name:
            return
        config.save_profile_as(name, self.values())
        self._current_profile = name
        self._refresh_profile_combo()

    def _save_as_default(self) -> None:
        name = self._current_profile
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Save as Default",
            f"Overwrite the shared default_config.json for '{name}' with the "
            "current form values? This is the git-committed baseline other "
            "workstations reset to.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return
        config.save_as_default(name, self.values())

    def _delete_profile(self) -> None:
        name = self._profile_combo.currentText()
        if not name:
            return
        if len(config.list_profiles()) <= 1:
            QtWidgets.QMessageBox.warning(
                self, "Delete profile", "Cannot delete the last remaining profile."
            )
            return
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Delete profile",
            f"Delete profile '{name}'? This cannot be undone.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return
        config.delete_profile(name)
        self._current_profile = config.active_profile()
        self._refresh_profile_combo()
        self._load_from(config.profile_values(self._current_profile))

    # ── Paths ────────────────────────────────────────────────────────────────────

    def _build_files_card(self) -> QtWidgets.QWidget:
        card = S.make_card("Paths  (where the runner looks for plans)")
        grid = QtWidgets.QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        self._bluesky_root = QtWidgets.QLineEdit()
        self._bluesky_root.setPlaceholderText(
            "(auto-detect -- assumes B-PILOT is nested inside mpe_bluesky, as today)"
        )
        self._bluesky_root.setToolTip(
            "Optional: the real mpe_bluesky checkout, if B-PILOT itself isn't\n"
            "nested inside it. Must contain instrument/ plus blueskyStarter.sh\n"
            "or qserver.sh -- an invalid path is ignored (falls back to\n"
            "auto-detect, with a startup warning explaining why). Leave blank\n"
            "for the normal nested layout. Takes effect on the next launch,\n"
            "not live."
        )
        grid.addWidget(S.LabelRight("Bluesky root:"), 0, 0)
        grid.addWidget(self._bluesky_root, 0, 1)
        grid.addWidget(self._browse_button(self._bluesky_root), 0, 2)

        self._plans_dir = QtWidgets.QLineEdit()
        self._plans_dir.setToolTip(
            "Folder scanned for plan .py files (top level + one subfolder deep)."
        )
        grid.addWidget(S.LabelRight("Plans directory:"), 1, 0)
        grid.addWidget(self._plans_dir, 1, 1)
        grid.addWidget(self._browse_button(self._plans_dir), 1, 2)

        self._import_root = QtWidgets.QLineEdit()
        self._import_root.setToolTip(
            "Root the 'from <module> import <plan>' line is resolved against.\n"
            "The module = plan file path relative to this root.\n"
            "e.g. root=mpe_bluesky/ -> instrument/plans/foo.py -> "
            "instrument.plans.foo"
        )
        grid.addWidget(S.LabelRight("Import root:"), 2, 0)
        grid.addWidget(self._import_root, 2, 1)
        grid.addWidget(self._browse_button(self._import_root), 2, 2)

        self._default_file = QtWidgets.QLineEdit()
        self._default_file.setToolTip(
            "File in the plans directory checked (shown) by default on startup."
        )
        grid.addWidget(S.LabelRight("Default plan file:"), 3, 0)
        grid.addWidget(self._default_file, 3, 1)

        card.body.addLayout(grid)
        return card

    # ── Plans (plan visibility) ──────────────────────────────────────────────────

    def _build_visibility_card(self) -> QtWidgets.QWidget:
        card = S.make_card("Plan visibility  (which files appear in the User files panel)")

        # Leaf (file) tree items, keyed by plans_dir-relative path.
        self._visibility_items: dict[str, QtWidgets.QTreeWidgetItem] = {}
        self._visible_files_initial: set[str] = set()

        btn_row = QtWidgets.QHBoxLayout()
        select_all_btn = QtWidgets.QPushButton("Select all")
        select_all_btn.clicked.connect(lambda: self._set_all_visibility_checked(True))
        deselect_all_btn = QtWidgets.QPushButton("Deselect all")
        deselect_all_btn.clicked.connect(lambda: self._set_all_visibility_checked(False))
        expand_all_btn = QtWidgets.QPushButton("Expand all")
        expand_all_btn.clicked.connect(lambda: self._visibility_tree.expandAll())
        collapse_all_btn = QtWidgets.QPushButton("Collapse all")
        collapse_all_btn.clicked.connect(lambda: self._visibility_tree.collapseAll())
        refresh_btn = QtWidgets.QPushButton("Refresh list")
        refresh_btn.setToolTip(
            "Re-scan the Plans directory (Paths tab) — picks up files added/"
            "removed on disk, or an edited Plans directory field."
        )
        refresh_btn.clicked.connect(self._rebuild_visibility_list)
        btn_row.addWidget(select_all_btn)
        btn_row.addWidget(deselect_all_btn)
        btn_row.addWidget(expand_all_btn)
        btn_row.addWidget(collapse_all_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(refresh_btn)
        card.body.addLayout(btn_row)

        self._visibility_tree = QtWidgets.QTreeWidget()
        self._visibility_tree.setHeaderHidden(True)
        self._visibility_tree.setMinimumHeight(S.px(160))
        card.body.addWidget(self._visibility_tree)

        # Re-scan automatically when the Plans directory field is edited.
        self._plans_dir.editingFinished.connect(self._rebuild_visibility_list)

        return card

    def _rebuild_visibility_list(self) -> None:
        """Re-scan the (possibly just-edited) Plans directory and rebuild the tree.

        Preserves already-toggled checkbox states and expand/collapse state
        (by folder path) across a rescan.
        """
        plans_dir = self._plans_dir.text().strip()
        old_checked = {
            rel: item.checkState(0) == QtCore.Qt.Checked
            for rel, item in self._visibility_items.items()
        }
        old_expanded = {
            item.data(0, QtCore.Qt.UserRole): item.isExpanded()
            for item in self._all_visibility_dir_items()
        }

        self._visibility_tree.clear()
        self._visibility_items.clear()

        root = self._visibility_tree.invisibleRootItem()
        parent_by_depth: dict[int, QtWidgets.QTreeWidgetItem] = {-1: root}
        for display_name, kind, abs_path, depth in P.scan_user_dir(plans_dir):
            parent = parent_by_depth[depth - 1]
            if kind == "dir":
                item = QtWidgets.QTreeWidgetItem(parent, [f"📁 {display_name}"])
                item.setFlags(item.flags() & ~QtCore.Qt.ItemIsUserCheckable)
                item.setData(0, QtCore.Qt.UserRole, abs_path)
                item.setExpanded(old_expanded.get(abs_path, True))
                parent_by_depth[depth] = item
                continue
            rel = os.path.relpath(abs_path, plans_dir).replace(os.sep, "/")
            item = QtWidgets.QTreeWidgetItem(parent, [display_name])
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(
                0,
                QtCore.Qt.Checked
                if old_checked.get(rel, rel in self._visible_files_initial)
                else QtCore.Qt.Unchecked,
            )
            self._visibility_items[rel] = item

    def _all_visibility_dir_items(self):
        """Yield every folder QTreeWidgetItem currently in the visibility tree."""
        stack = [self._visibility_tree.invisibleRootItem()]
        while stack:
            node = stack.pop()
            for i in range(node.childCount()):
                child = node.child(i)
                if not (child.flags() & QtCore.Qt.ItemIsUserCheckable):
                    yield child  # dirs are the only non-checkable items
                stack.append(child)

    def _set_all_visibility_checked(self, checked: bool) -> None:
        state = QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked
        for item in self._visibility_items.values():
            item.setCheckState(0, state)

    @staticmethod
    def _set_all_checked(checks: dict[str, QtWidgets.QCheckBox], checked: bool) -> None:
        for cb in checks.values():
            cb.setChecked(checked)

    # ── Launch Session: auto-run startup command(s) ──────────────────────────────

    def _build_launch_card(self) -> QtWidgets.QWidget:
        card = S.make_card("Launch  (auto-run on start)")
        card.body.addWidget(
            QtWidgets.QLabel(
                "Run automatically in the console right after Launch IPython "
                "connects (one command per line — CONNECTS TO HARDWARE on a "
                "beamline):"
            )
        )
        self._startup = QtWidgets.QPlainTextEdit()
        self._startup.setObjectName("mono")
        self._startup.setFixedHeight(S.px(110))
        self._startup.setToolTip(
            "Normally EMPTY: the instrument import is done by this profile's "
            "starter script (starter_scripts/*.sh) as the kernel starts, not "
            "from here. Use this only for EXTRA commands to run once the "
            "console connects -- each line runs as its own command, in order."
        )
        card.body.addWidget(self._startup)

        self._send_import = QtWidgets.QCheckBox(
            "Send the 'from … import …' line along with the RE(…) command"
        )
        self._send_import.setToolTip(
            "The Command panel always SHOWS the import line; this controls "
            "whether it is also sent to the console.\n"
            "Off (default): only the RE(...) call is sent — the starter script "
            "has already imported the instrument into the kernel, so the line "
            "is redundant, and a stale module path can't kill the run (import "
            "and RE(...) share one execution).\n"
            "On: both lines are sent, as before."
        )
        card.body.addWidget(self._send_import)

        self._keep_kernel = QtWidgets.QCheckBox(
            "Keep the IPython kernel running when the GUI closes "
            "(so it can be reattached)"
        )
        self._keep_kernel.setToolTip(
            "On: closing the GUI leaves the kernel (and any running plan) alive; "
            "relaunch and use Console → Attach to reconnect.\n"
            "Off: the kernel is shut down when the GUI closes."
        )
        card.body.addWidget(self._keep_kernel)
        return card

    # ── Launch Session: one kernel per beamline ──────────────────────────────────

    def _build_session_card(self) -> QtWidgets.QWidget:
        card = S.make_card("Session  (one kernel per beamline)")
        grid = QtWidgets.QGridLayout()
        grid.setColumnStretch(1, 1)

        self._beamline = QtWidgets.QLineEdit()
        self._beamline.setToolTip(
            "Identifies the single interactive kernel for this beamline "
            "(screen session name + fixed connection-file path) and the "
            "device catalog used by this profile."
        )
        grid.addWidget(S.LabelRight("Beamline id:"), 0, 0)
        grid.addWidget(self._beamline, 0, 1)

        card.body.addLayout(grid)

        self._use_screen = QtWidgets.QCheckBox(
            "Host the kernel in a named 'screen' session (recommended)"
        )
        self._use_screen.setToolTip(
            "On: the kernel runs inside 'screen bluesky-kernel-<beamline>' so it "
            "survives the GUI and staff can attach a terminal with\n"
            "  screen -r bluesky-kernel-<beamline>\n"
            "Off: the kernel is launched as a plain detached process."
        )
        card.body.addWidget(self._use_screen)

        srow = QtWidgets.QGridLayout()
        srow.setColumnStretch(1, 1)
        self._embedded_starter = QtWidgets.QLineEdit()
        self._embedded_starter.setToolTip(
            "Script run for the embedded kernel launch — activates the env + "
            "records the experiment, then starts a connectable ipykernel. "
            "Called as:\n"
            "  <script> <dm_experiment> <setup_file> <connection_file> <screen>\n"
            "Leave blank to launch a bare kernel with no env activation."
        )
        srow.addWidget(S.LabelRight("Embedded starter:"), 0, 0)
        srow.addWidget(self._embedded_starter, 0, 1)
        srow.addWidget(self._browse_button(self._embedded_starter, kind="file"), 0, 2)

        card.body.addLayout(srow)
        return card

    # ── Devices (search paths + discover) ────────────────────────────────────────

    def _build_devices_card(self) -> QtWidgets.QWidget:
        card = S.make_card("Devices  (search paths + discovered device names)")

        card.body.addWidget(
            QtWidgets.QLabel(
                "Directories scanned for device-defining .py files (never imported "
                "— only their __all__ list is read):"
            )
        )
        paths_row = QtWidgets.QHBoxLayout()
        self._device_paths_widget = QtWidgets.QListWidget()
        self._device_paths_widget.setFixedHeight(S.px(70))
        paths_row.addWidget(self._device_paths_widget, 1)
        paths_btns = QtWidgets.QVBoxLayout()
        add_path_btn = QtWidgets.QPushButton("Add…")
        add_path_btn.clicked.connect(self._add_device_path)
        remove_path_btn = QtWidgets.QPushButton("Remove")
        remove_path_btn.clicked.connect(self._remove_device_path)
        paths_btns.addWidget(add_path_btn)
        paths_btns.addWidget(remove_path_btn)
        paths_btns.addStretch(1)
        paths_row.addLayout(paths_btns)
        card.body.addLayout(paths_row)

        btn_row = QtWidgets.QHBoxLayout()
        discover_btn = QtWidgets.QPushButton("Discover")
        discover_btn.setToolTip(
            "Re-scan the search paths above for __all__-exported device names. "
            "Newly found devices are shown by default; existing checkbox states "
            "are preserved."
        )
        discover_btn.clicked.connect(self._rebuild_device_list)
        select_all_btn = QtWidgets.QPushButton("Select all")
        select_all_btn.clicked.connect(lambda: self._set_all_device_checked(True))
        deselect_all_btn = QtWidgets.QPushButton("Deselect all")
        deselect_all_btn.clicked.connect(lambda: self._set_all_device_checked(False))
        btn_row.addWidget(discover_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(select_all_btn)
        btn_row.addWidget(deselect_all_btn)
        card.body.addLayout(btn_row)

        # category -> name -> checkbox; mirrors the on-disk device_selection shape.
        self._device_checks: dict[str, dict[str, QtWidgets.QCheckBox]] = {}
        self._device_selection_initial: dict[str, dict[str, bool]] = {}
        # {device_name: [category, ...]} — manual per-profile override of the
        # categor(y/ies) device_discovery infers; lets a device appear as an
        # option in more than one plan-parameter field. See the Devices card's
        # per-device category field below.
        self._device_category_overrides: dict[str, list[str]] = {}
        # {device_name: discovered_category} — recomputed on every rescan, used
        # to know what "(auto)" resolves to and when an override is redundant.
        self._device_raw_category: dict[str, str] = {}
        self._device_container = QtWidgets.QWidget()
        self._device_layout = QtWidgets.QVBoxLayout(self._device_container)
        self._device_layout.setContentsMargins(2, 2, 2, 2)
        self._device_layout.setSpacing(2)
        dev_scroll = QtWidgets.QScrollArea()
        dev_scroll.setWidgetResizable(True)
        dev_scroll.setWidget(self._device_container)
        dev_scroll.setMinimumHeight(S.px(200))
        card.body.addWidget(dev_scroll)

        return card

    def _add_device_path(self) -> None:
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select device search directory", _paths.BLUESKY_ROOT
        )
        if not chosen:
            return
        rel = os.path.relpath(chosen, _paths.BLUESKY_ROOT)
        value = rel if not rel.startswith("..") else chosen
        self._device_paths_widget.addItem(value)
        self._rebuild_device_list()

    def _remove_device_path(self) -> None:
        for item in self._device_paths_widget.selectedItems():
            self._device_paths_widget.takeItem(self._device_paths_widget.row(item))
        self._rebuild_device_list()

    def _rebuild_device_list(self) -> None:
        """Re-scan the search paths and rebuild the checkbox list, grouped by category.

        Discovery's inferred category is never changed by this — per-device
        category dropdowns let the user assign one or more categories for
        THIS profile only (`self._device_category_overrides`), applied on top of
        the discovered category before grouping. A device with more than one
        category gets one row (with its own independent shown/hidden
        checkbox) per category group it belongs to.

        Preserves already-toggled checkbox states across a rescan, keyed by
        (category, name) — NOT name alone, since a multi-category device has
        independent checked state per category group it appears in; a
        (category, name) never seen before defaults to checked (shown) —
        "everything found is shown by default."
        """
        raw_paths = [
            self._device_paths_widget.item(i).text() for i in range(self._device_paths_widget.count())
        ]
        old_checked: dict[tuple[str, str], bool] = {
            (cat, name): cb.isChecked()
            for cat, names in self._device_checks.items()
            for name, cb in names.items()
        }
        resolved = [device_source.resolve_path(p) for p in raw_paths]
        discovered = ddisc.scan(resolved)

        while self._device_layout.count():
            item = self._device_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._device_checks.clear()
        self._device_raw_category.clear()

        by_category: dict[str, list] = {}
        for device in discovered:
            self._device_raw_category[device.name] = device.category
            categories = self._device_category_overrides.get(device.name) or [device.category]
            for category in categories:
                by_category.setdefault(category, []).append(device)

        # Every category currently in use anywhere in this profile — offered
        # as dropdown options (still editable, so a brand-new one can be typed).
        all_categories = sorted(by_category)

        for category in sorted(by_category):
            hdr = QtWidgets.QLabel(category)
            hdr.setStyleSheet(f"color: {S.MUTED}; font-weight: bold;")
            self._device_layout.addWidget(hdr)
            self._device_checks[category] = {}
            for device in sorted(by_category[category], key=lambda d: d.name.lower()):
                cb = QtWidgets.QCheckBox(device.name)
                cb.setToolTip(device.source_file)
                checked = old_checked.get((category, device.name))
                if checked is None:
                    checked = self._initial_device_checked(category, device.name)
                cb.setChecked(checked)
                self._device_checks[category][device.name] = cb

                field = self._make_category_field(device.name, device.category, all_categories)

                row = QtWidgets.QWidget()
                row_lay = QtWidgets.QHBoxLayout(row)
                row_lay.setContentsMargins(0, 0, 0, 0)
                row_lay.setSpacing(6)
                row_lay.addWidget(cb)
                row_lay.addWidget(field)
                row_lay.addStretch(1)
                self._device_layout.addWidget(row)
        self._device_layout.addStretch(1)

    def _initial_device_checked(self, category: str, name: str) -> bool:
        """Fall back to the on-disk `device_selection` for a device with no
        session-local checked state yet — first under its current (possibly
        overridden) category, then any category (covers a device recategorized
        after `device_selection` was last saved under its old category)."""
        sel = self._device_selection_initial
        if name in sel.get(category, {}):
            return sel[category][name]
        for cat_sel in sel.values():
            if name in cat_sel:
                return cat_sel[name]
        return True

    def _make_category_field(
        self, name: str, raw_category: str, all_categories: list[str]
    ) -> _CategoryDropdowns:
        """One-or-more category dropdowns for `name` (this profile only) —
        discovery's own inference is never changed. Assigning more than one
        category lets a device appear as an option in more than one
        plan-parameter field. Rendered once per category group `name`
        currently belongs to; editing any copy updates the same override."""
        override = self._device_category_overrides.get(name)
        field = _CategoryDropdowns(
            override or [raw_category],
            all_categories,
            lambda cats, n=name: self._apply_device_categories(n, cats),
        )
        field.setToolTip(
            "Category/categories this device should appear under — add more "
            "with '+' to let it show up as an option in more than one "
            f"plan-parameter field. Auto-detected category: {raw_category}."
        )
        return field

    def _apply_device_categories(self, name: str, cats: list[str]) -> None:
        raw = self._device_raw_category.get(name)
        if not cats or cats == [raw]:
            self._device_category_overrides.pop(name, None)
        else:
            self._device_category_overrides[name] = cats
        # Deferred: this is called from inside the very combo box's own
        # activated/editingFinished signal handler. Rebuilding synchronously
        # tears down that combo's widget hierarchy mid-signal, which made the
        # dropdown appear to close itself instead of registering the pick.
        QtCore.QTimer.singleShot(0, self._rebuild_device_list)

    def _set_all_device_checked(self, checked: bool) -> None:
        for names in self._device_checks.values():
            for cb in names.values():
                cb.setChecked(checked)

    # ── Scan blocks (scan_skeletons.py plan_opener/per_step/plan_closer/suspenders) ─

    _SCAN_BLOCK_LABELS = {
        "plan_opener": "Plan openers",
        "per_step": "Per-steps",
        "plan_closer": "Plan closers",
        "suspender": "Suspenders",
        "pseudo_suspender": "Pseudo-suspenders",
    }

    # Number of name "chips" laid out per row inside each category card.
    _SCAN_BLOCK_COLUMNS = 3

    def _build_scan_blocks_card(self) -> QtWidgets.QWidget:
        card = S.make_card(
            "Scan building blocks  (plan_opener / per_step / plan_closer / suspenders)"
        )
        card.body.addWidget(
            QtWidgets.QLabel(
                "Files scanned for scan_skeletons.py's plan_opener/per_step/"
                "plan_closer names (never imported — only their __all__ list "
                "is read):"
            )
        )
        stub_row = QtWidgets.QHBoxLayout()
        self._plan_building_paths_widget = QtWidgets.QListWidget()
        self._plan_building_paths_widget.setFixedHeight(S.px(50))
        stub_row.addWidget(self._plan_building_paths_widget, 1)
        stub_btns = QtWidgets.QVBoxLayout()
        add_stub_btn = QtWidgets.QPushButton("Add…")
        add_stub_btn.clicked.connect(self._add_plan_building_path)
        remove_stub_btn = QtWidgets.QPushButton("Remove")
        remove_stub_btn.clicked.connect(self._remove_plan_building_path)
        stub_btns.addWidget(add_stub_btn)
        stub_btns.addWidget(remove_stub_btn)
        stub_btns.addStretch(1)
        stub_row.addLayout(stub_btns)
        card.body.addLayout(stub_row)

        card.body.addWidget(
            QtWidgets.QLabel(
                "Files scanned for suspender/pseudo_suspender names (common "
                "files plus this beamline's own <bl>_suspenders.py):"
            )
        )
        susp_row = QtWidgets.QHBoxLayout()
        self._suspender_paths_widget = QtWidgets.QListWidget()
        self._suspender_paths_widget.setFixedHeight(S.px(50))
        susp_row.addWidget(self._suspender_paths_widget, 1)
        susp_btns = QtWidgets.QVBoxLayout()
        add_susp_btn = QtWidgets.QPushButton("Add…")
        add_susp_btn.clicked.connect(self._add_suspender_path)
        remove_susp_btn = QtWidgets.QPushButton("Remove")
        remove_susp_btn.clicked.connect(self._remove_suspender_path)
        susp_btns.addWidget(add_susp_btn)
        susp_btns.addWidget(remove_susp_btn)
        susp_btns.addStretch(1)
        susp_row.addLayout(susp_btns)
        card.body.addLayout(susp_row)

        btn_row = QtWidgets.QHBoxLayout()
        discover_btn = QtWidgets.QPushButton("Discover")
        discover_btn.setToolTip(
            "Re-scan the search paths above for __all__-exported plan_opener/"
            "per_step/plan_closer/suspender/pseudo_suspender names. Replaces "
            "the catalog shown below — Save (or Save as Default) to persist it."
        )
        discover_btn.clicked.connect(self._rebuild_scan_blocks)
        btn_row.addWidget(discover_btn)
        btn_row.addStretch(1)
        card.body.addLayout(btn_row)

        # {category: [name, ...]} — the persisted catalog itself (unlike
        # devices, refreshed only on an explicit Discover, never live).
        self._plan_building_blocks: dict[str, list[str]] = {}
        self._scan_blocks_container = QtWidgets.QWidget()
        self._scan_blocks_layout = QtWidgets.QVBoxLayout(self._scan_blocks_container)
        self._scan_blocks_layout.setContentsMargins(S.px(2), S.px(2), S.px(2), S.px(2))
        self._scan_blocks_layout.setSpacing(S.px(8))
        scan_scroll = QtWidgets.QScrollArea()
        scan_scroll.setWidgetResizable(True)
        scan_scroll.setWidget(self._scan_blocks_container)
        scan_scroll.setMinimumHeight(S.px(200))
        card.body.addWidget(scan_scroll)

        return card

    def _add_plan_building_path(self) -> None:
        chosen, _filt = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select plan_opener/per_step/plan_closer file", _paths.BLUESKY_ROOT,
            "Python files (*.py)",
        )
        if not chosen:
            return
        rel = os.path.relpath(chosen, _paths.BLUESKY_ROOT)
        value = rel if not rel.startswith("..") else chosen
        self._plan_building_paths_widget.addItem(value)
        self._rebuild_scan_blocks()

    def _remove_plan_building_path(self) -> None:
        for item in self._plan_building_paths_widget.selectedItems():
            self._plan_building_paths_widget.takeItem(self._plan_building_paths_widget.row(item))
        self._rebuild_scan_blocks()

    def _add_suspender_path(self) -> None:
        chosen, _filt = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select suspender file", _paths.BLUESKY_ROOT, "Python files (*.py)",
        )
        if not chosen:
            return
        rel = os.path.relpath(chosen, _paths.BLUESKY_ROOT)
        value = rel if not rel.startswith("..") else chosen
        self._suspender_paths_widget.addItem(value)
        self._rebuild_scan_blocks()

    def _remove_suspender_path(self) -> None:
        for item in self._suspender_paths_widget.selectedItems():
            self._suspender_paths_widget.takeItem(self._suspender_paths_widget.row(item))
        self._rebuild_scan_blocks()

    def _rebuild_scan_blocks(self) -> None:
        """Re-scan the search paths and replace the persisted catalog."""
        stub_paths = [
            self._plan_building_paths_widget.item(i).text()
            for i in range(self._plan_building_paths_widget.count())
        ]
        susp_paths = [
            self._suspender_paths_widget.item(i).text()
            for i in range(self._suspender_paths_widget.count())
        ]
        resolved_stub = [device_source.resolve_path(p) for p in stub_paths]
        resolved_susp = [device_source.resolve_path(p) for p in susp_paths]
        self._plan_building_blocks = sdisc.scan(resolved_stub, resolved_susp)
        self._render_scan_blocks_display()

    def _render_scan_blocks_display(self) -> None:
        """Repaint the read-only catalog display from `self._plan_building_blocks`
        (no rescan — called on profile load too, to show the persisted catalog
        as-is until the user explicitly clicks Discover).

        Each category is its own colour-coded card: a coloured left bar + bold
        header + count badge, then the discovered names laid out as monospace
        "chips" across :data:`_SCAN_BLOCK_COLUMNS` columns (or a muted
        empty-state line). This reads far better than the old flat single-column
        list once a beamline has a few dozen building blocks."""
        while self._scan_blocks_layout.count():
            item = self._scan_blocks_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        for category in sdisc.CATEGORIES:
            names = self._plan_building_blocks.get(category) or []
            self._scan_blocks_layout.addWidget(
                self._make_scan_block_card(category, names)
            )
        self._scan_blocks_layout.addStretch(1)

    def _make_scan_block_card(
        self, category: str, names: list[str]
    ) -> QtWidgets.QWidget:
        """Build one colour-coded category card for the Scan blocks display."""
        color = S.SCAN_BLOCK_COLORS.get(category, S.ACCENT)

        frame = QtWidgets.QFrame()
        frame.setObjectName("scanBlockCard")
        frame.setStyleSheet(
            f"QFrame#scanBlockCard {{ background: {S.PANEL};"
            f" border: 1px solid {S.BORDER};"
            f" border-left: {S.px(3)}px solid {color};"
            f" border-radius: {S.px(4)}px; }}"
        )
        v = QtWidgets.QVBoxLayout(frame)
        v.setContentsMargins(S.px(8), S.px(6), S.px(8), S.px(8))
        v.setSpacing(S.px(6))

        # Header: bold category name + a pill count badge.
        hdr = QtWidgets.QHBoxLayout()
        hdr.setSpacing(S.px(6))
        title = QtWidgets.QLabel(self._SCAN_BLOCK_LABELS[category])
        title.setStyleSheet(
            f"color: {color}; font-weight: bold; border: none; background: transparent;"
        )
        hdr.addWidget(title)
        badge = QtWidgets.QLabel(str(len(names)))
        badge.setAlignment(QtCore.Qt.AlignCenter)
        badge.setStyleSheet(
            f"color: white; background: {color}; border: none;"
            f" border-radius: {S.px(8)}px; padding: 0 {S.px(6)}px;"
            f" min-width: {S.px(14)}px; font-weight: bold;"
        )
        hdr.addWidget(badge)
        hdr.addStretch(1)
        v.addLayout(hdr)

        if not names:
            empty = QtWidgets.QLabel("— none discovered —")
            empty.setStyleSheet(
                f"color: {S.MUTED}; font-style: italic;"
                f" border: none; background: transparent;"
            )
            v.addWidget(empty)
            return frame

        chip_qss = (
            f"QLabel {{ background: {S.INPUT_BG}; color: {S.INPUT_FG};"
            f" border: 1px solid {S.BORDER}; border-radius: {S.px(3)}px;"
            f" padding: {S.px(2)}px {S.px(6)}px; font-family: {S.MONO_CSS}; }}"
        )
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(S.px(6))
        grid.setVerticalSpacing(S.px(4))
        cols = self._SCAN_BLOCK_COLUMNS
        for i, name in enumerate(names):
            chip = QtWidgets.QLabel(name)
            chip.setStyleSheet(chip_qss)
            chip.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            row, col = divmod(i, cols)
            grid.addWidget(chip, row, col, QtCore.Qt.AlignLeft)
        grid.setColumnStretch(cols, 1)  # push chips left, leave slack on the right
        v.addLayout(grid)
        return frame

    # ── Queue Backend (native persistent queue, or the bluesky queueserver) ──────

    def _build_queue_backend_card(self) -> QtWidgets.QWidget:
        card = S.make_card("Queue Backend")
        v = QtWidgets.QVBoxLayout()
        v.setSpacing(6)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(S.LabelRight("Backend:"))
        self._queue_backend = QtWidgets.QComboBox()
        self._queue_backend.addItem("Native (B-PILOT's own queue)", "native")
        self._queue_backend.addItem("Queue Server (QS)", "qs")
        self._queue_backend.setToolTip(
            "Which plan-queue backend \"Add to Queue\"/the queue panel use.\n"
            "Native (default): B-PILOT's own persistent per-beamline queue,\n"
            "driven by queue_runner.py against the embedded console kernel.\n"
            "Queue Server: dispatches through the Bluesky queueserver (QS)\n"
            "instead, using the connection settings below. Takes effect on\n"
            "the next launch, not live."
        )
        self._queue_backend.currentIndexChanged.connect(self._on_queue_backend_changed)
        row.addWidget(self._queue_backend)
        row.addStretch(1)
        v.addLayout(row)

        note = QtWidgets.QLabel(
            "Only the plan QUEUE (Add to Queue / the queue panel) is affected. "
            "Run / interactive commands always go to the embedded console "
            "kernel, regardless of this setting. Restart required to take effect."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {S.MUTED};")
        v.addWidget(note)

        grid = QtWidgets.QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        self._qs_control_addr = QtWidgets.QLineEdit()
        self._qs_control_addr.setToolTip(
            "ZMQ control address of the queueserver's start-re-manager "
            "process (e.g. tcp://redwood.xray.aps.anl.gov:60615). Used for "
            "adding/removing/starting queue items and RunEngine control."
        )
        grid.addWidget(S.LabelRight("Control address:"), 0, 0)
        grid.addWidget(self._qs_control_addr, 0, 1)

        self._qs_info_addr = QtWidgets.QLineEdit()
        self._qs_info_addr.setToolTip(
            "ZMQ info/console-publish address of the same queueserver "
            "(e.g. tcp://redwood.xray.aps.anl.gov:60625)."
        )
        grid.addWidget(S.LabelRight("Info address:"), 1, 0)
        grid.addWidget(self._qs_info_addr, 1, 1)

        self._qs_user = QtWidgets.QLineEdit()
        self._qs_user.setToolTip(
            "User name attached to queue items this GUI adds. Leave blank "
            "to use the logged-in account name."
        )
        grid.addWidget(S.LabelRight("User:"), 2, 0)
        grid.addWidget(self._qs_user, 2, 1)

        self._qs_user_group = QtWidgets.QLineEdit()
        self._qs_user_group.setToolTip(
            "User group for queueserver permission checks — must match a "
            "group defined in mpe_bluesky/qserver/user_group_permissions.yaml "
            "(default 'primary')."
        )
        grid.addWidget(S.LabelRight("User group:"), 3, 0)
        grid.addWidget(self._qs_user_group, 3, 1)

        v.addLayout(grid)
        self._qs_fields = (
            self._qs_control_addr, self._qs_info_addr, self._qs_user, self._qs_user_group,
        )
        card.body.addLayout(v)
        return card

    def _on_queue_backend_changed(self) -> None:
        """Grey out the QS connection fields whenever Native is selected —
        purely a visual hint that they're unused; they're still saved
        unchanged either way, so switching back to "qs" later doesn't lose
        them."""
        is_qs = self._queue_backend.currentData() == "qs"
        for w in self._qs_fields:
            w.setEnabled(is_qs)

    # ── Data Viewer (databroker connection) ──────────────────────────────────────

    def _build_data_viewer_card(self) -> QtWidgets.QWidget:
        card = S.make_card("Data Viewer  (databroker connection)")
        grid = QtWidgets.QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        self._databroker_catalog = QtWidgets.QLineEdit()
        self._databroker_catalog.setToolTip(
            "Name of a databroker catalog registered in ~/.local/share/intake/*.yml "
            "(e.g. 'hexm', 'ht_hedm', '1id_hexm') — see instrument/iconfig.yml's "
            "DATABROKER_CATALOG per account. This is a NAME, not a connection "
            "string: the actual MongoDB URI (with credentials) is resolved locally "
            "from the pre-registered intake file, never stored here.\n"
            "Leave blank to auto-detect from iconfig.yml by the logged-in account."
        )
        grid.addWidget(S.LabelRight("Databroker catalog:"), 0, 0)
        grid.addWidget(self._databroker_catalog, 0, 1)

        self._databroker_uri = QtWidgets.QLineEdit()
        self._databroker_uri.setToolTip(
            "Optional Tiled (or other) URI override — when set, this replaces the "
            "named catalog above. Do NOT put a credentialed mongodb://user:pass@ "
            "URI here: profiles are meant to be committed to git and shared "
            "between beamline staff, so secrets don't belong in this field."
        )
        grid.addWidget(S.LabelRight("Alternate URI:"), 1, 0)
        grid.addWidget(self._databroker_uri, 1, 1)

        self._databroker_nexus_dir = QtWidgets.QLineEdit()
        self._databroker_nexus_dir.setToolTip(
            "Optional folder holding raw NeXus files alongside catalog records."
        )
        grid.addWidget(S.LabelRight("NeXus files dir:"), 2, 0)
        grid.addWidget(self._databroker_nexus_dir, 2, 1)
        grid.addWidget(self._browse_button(self._databroker_nexus_dir), 2, 2)

        card.body.addLayout(grid)
        note = QtWidgets.QLabel(
            "These are starting defaults for the standalone Data Viewer window "
            "(python -m B_PILOT.viewer) — still editable there per-session."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {S.MUTED};")
        card.body.addWidget(note)

        self._midas_bridge_enabled = QtWidgets.QCheckBox(
            "Auto-start MIDAS_GUI live view when a detector plan runs"
        )
        self._midas_bridge_enabled.setToolTip(
            "When a Run/Queue dispatch includes an area-detector device, send "
            "its EPICS prefix to a locally-running MIDAS_GUI so its Data "
            "Viewer auto-starts Live Data on that detector's PVA channel — "
            "zero clicks in MIDAS_GUI. No-op if MIDAS_GUI isn't running; "
            "never launches it. On by default; also mirrored by the "
            "toolbar's \"Bridge Live-View\" checkbox. Takes effect "
            "immediately on Save, no restart needed."
        )
        card.body.addWidget(self._midas_bridge_enabled)
        return card

    # ── Appearance (display scale) ───────────────────────────────────────────────

    def _build_appearance_card(self) -> QtWidgets.QWidget:
        card = S.make_card("Appearance")

        theme_row = QtWidgets.QHBoxLayout()
        theme_row.addWidget(S.LabelRight("Color theme:"))
        self._theme = S.NoScrollComboBox()
        for key, label in S.THEME_CHOICES:
            self._theme.addItem(label, key)
        self._theme.setToolTip(
            "Color theme for the whole app. Takes effect on the next launch."
        )
        theme_row.addWidget(self._theme)
        theme_row.addStretch(1)
        card.body.addLayout(theme_row)

        font_row = QtWidgets.QHBoxLayout()
        font_row.addWidget(S.LabelRight("Font:"))
        self._font_family = S.NoScrollComboBox()
        for key, label in S.FONT_CHOICES:
            self._font_family.addItem(label, key)
        self._font_family.setToolTip(
            "Font family for the whole app. Takes effect on the next launch."
        )
        font_row.addWidget(self._font_family)
        font_row.addStretch(1)
        card.body.addLayout(font_row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(S.LabelRight("UI scale:"))
        self._ui_scale = QtWidgets.QDoubleSpinBox()
        self._ui_scale.setRange(0.5, 3.0)
        self._ui_scale.setSingleStep(0.1)
        self._ui_scale.setDecimals(2)
        self._ui_scale.setSuffix("×")
        self._ui_scale.setToolTip(
            "Multiplier applied to every font, widget, and window size — for "
            "high-DPI screens (e.g. 4K). Takes effect on the next launch."
        )
        row.addWidget(self._ui_scale)
        row.addStretch(1)
        card.body.addLayout(row)
        note = QtWidgets.QLabel(
            "Restart B-PILOT for a theme, font, or scale change to take effect."
        )
        note.setStyleSheet(f"color: {S.MUTED};")
        card.body.addWidget(note)
        return card

    # ── AutoPILOT (optional AI chat dock) ────────────────────────────────────────

    def _build_reports_card(self) -> QtWidgets.QWidget:
        """Experiment Report settings: the dock toggle, its title, and the
        readings its Snapshot button offers."""
        card = S.make_card("Experiment Report")

        self._report_enabled = QtWidgets.QCheckBox("Show the Experiment Report panel")
        self._report_enabled.setToolTip(
            "A live lab record for the running experiment, built from the "
            "plans that run plus the notes and snapshots you add. Takes "
            "effect immediately on Save, no restart needed."
        )
        card.body.addWidget(self._report_enabled)

        title_row = QtWidgets.QHBoxLayout()
        title_row.addWidget(S.LabelRight("Report title:"))
        self._report_title = QtWidgets.QLineEdit()
        self._report_title.setPlaceholderText("(blank — use the experiment name)")
        self._report_title.setToolTip(
            "Heading at the top of the report, e.g. 'HEDM — Ni625 sample B'."
        )
        title_row.addWidget(self._report_title, 1)
        card.body.addLayout(title_row)

        excluded = QtWidgets.QLabel(
            "<b>Plans kept out of the report.</b> Names or <tt>fnmatch</tt> "
            "patterns (<tt>cont_acq*</tt>). Excluded runs are <i>still "
            "recorded</i> — they show under the report's “Show hidden” toggle "
            "and come back if you remove the pattern. Typically continuous "
            "acquisition, which is a tool for aligning rather than a "
            "measurement worth filing."
        )
        excluded.setWordWrap(True)
        excluded.setStyleSheet(f"color: {S.MUTED};")
        card.body.addWidget(excluded)

        self._report_excluded = QtWidgets.QListWidget()
        self._report_excluded.setMaximumHeight(S.px(90))
        self._report_excluded.setToolTip(
            "Plan names excluded from the rendered report. Double-click to edit."
        )
        card.body.addWidget(self._report_excluded)

        excl_row = QtWidgets.QHBoxLayout()
        for text, tip, slot in (
            ("+ Add plan…", "Pick a plan from this profile's plan files, or type a name.",
             self._report_add_excluded),
            ("Remove", "Stop excluding the selected plan.", self._report_remove_excluded),
        ):
            btn = QtWidgets.QPushButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            excl_row.addWidget(btn)
        excl_row.addStretch(1)
        card.body.addLayout(excl_row)

        note = QtWidgets.QLabel(
            "Snapshot readings. Each is evaluated <b>in the running kernel</b> "
            "when you press Snapshot — B-PILOT never opens an EPICS channel "
            "itself. <b>Kind</b> matters: the kernel's reply is decoded with "
            "<tt>ast.literal_eval</tt>, so a value that isn't already a Python "
            "literal (an ophyd device, a numpy scalar) reads as blank unless "
            "it is coerced. Use <tt>float</tt> for positions and readbacks."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {S.MUTED};")
        card.body.addWidget(note)

        self._report_snapshots = QtWidgets.QTreeWidget()
        self._report_snapshots.setHeaderLabels(
            ["Group / Label", "Expression", "Kind", "Units"]
        )
        self._report_snapshots.setColumnWidth(0, S.px(160))
        self._report_snapshots.setColumnWidth(1, S.px(220))
        self._report_snapshots.setColumnWidth(2, S.px(60))
        self._report_snapshots.setMinimumHeight(S.px(200))
        self._report_snapshots.setEditTriggers(
            QtWidgets.QAbstractItemView.DoubleClicked
            | QtWidgets.QAbstractItemView.SelectedClicked
        )
        card.body.addWidget(self._report_snapshots)

        row = QtWidgets.QHBoxLayout()
        for text, tip, slot in (
            ("+ Group", "Add a new group of readings.", self._report_add_group),
            ("+ Reading", "Add a reading to the selected group.", self._report_add_reading),
            ("Add from devices…", "Pick motors from this profile's device catalog.",
             self._report_add_from_devices),
            ("Remove", "Remove the selected row.", self._report_remove_row),
        ):
            btn = QtWidgets.QPushButton(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        row.addStretch(1)
        card.body.addLayout(row)
        return card

    # -- excluded-plan list helpers -----------------------------------------

    def _report_discovered_plans(self) -> list[str]:
        """Plan names found in this profile's plan files, for the picker.

        Same principle as "Add from devices…" below: offer only names that
        really exist, so an exclusion cannot silently do nothing because of a
        typo. Falls back to free text when discovery turns up nothing (a plans
        directory that is not reachable from this machine, for instance).
        """
        plans_dir = self._plans_dir.text().strip()
        if not plans_dir:
            return []
        names: set[str] = set()
        for _display, kind, abs_path, _depth in P.scan_user_dir(plans_dir):
            if kind == "dir":
                continue
            names.update(P.find_plan_specs(abs_path))
        return sorted(names)

    def _report_excluded_names(self) -> list[str]:
        return [
            self._report_excluded.item(i).text().strip()
            for i in range(self._report_excluded.count())
            if self._report_excluded.item(i).text().strip()
        ]

    def _report_add_excluded(self) -> None:
        choices = self._report_discovered_plans()
        if choices:
            picked, ok = QtWidgets.QInputDialog.getItem(
                self, "Exclude a plan from the report", "Plan:", choices, 0, True
            )
        else:
            picked, ok = QtWidgets.QInputDialog.getText(
                self,
                "Exclude a plan from the report",
                "Plan name or pattern (e.g. cont_acq):",
            )
        picked = (picked or "").strip()
        if not (ok and picked) or picked in self._report_excluded_names():
            return
        item = QtWidgets.QListWidgetItem(picked)
        item.setFlags(item.flags() | QtCore.Qt.ItemIsEditable)
        self._report_excluded.addItem(item)

    def _report_remove_excluded(self) -> None:
        row = self._report_excluded.currentRow()
        if row >= 0:
            self._report_excluded.takeItem(row)

    def _report_load_excluded(self, names: list) -> None:
        self._report_excluded.clear()
        for name in names or []:
            item = QtWidgets.QListWidgetItem(str(name))
            item.setFlags(item.flags() | QtCore.Qt.ItemIsEditable)
            self._report_excluded.addItem(item)

    # -- snapshot table helpers ---------------------------------------------

    _REPORT_KINDS = ("float", "int", "str", "bool", "raw")

    @staticmethod
    def _report_make_reading(parent, label="", expr="", kind="float", units=""):
        item = QtWidgets.QTreeWidgetItem(parent, [label, expr, kind, units])
        item.setFlags(item.flags() | QtCore.Qt.ItemIsEditable)
        return item

    def _report_selected_group(self) -> QtWidgets.QTreeWidgetItem | None:
        """The group a new reading should go into: the selected group, the
        selected reading's parent, or the last group as a fallback."""
        item = self._report_snapshots.currentItem()
        if item is not None:
            return item.parent() or item
        count = self._report_snapshots.topLevelItemCount()
        return self._report_snapshots.topLevelItem(count - 1) if count else None

    def _report_add_group(self) -> None:
        name, ok = QtWidgets.QInputDialog.getText(self, "New group", "Group name:")
        if not (ok and name.strip()):
            return
        group = QtWidgets.QTreeWidgetItem(self._report_snapshots, [name.strip(), "", "", ""])
        group.setFlags(group.flags() | QtCore.Qt.ItemIsEditable)
        group.setExpanded(True)
        self._report_snapshots.setCurrentItem(group)

    def _report_add_reading(self) -> None:
        group = self._report_selected_group()
        if group is None:
            QtWidgets.QMessageBox.information(
                self, "No group", "Add a group first, then add readings to it."
            )
            return
        item = self._report_make_reading(group, "new reading", "", "float", "")
        group.setExpanded(True)
        self._report_snapshots.setCurrentItem(item)
        self._report_snapshots.editItem(item, 0)

    def _report_add_from_devices(self) -> None:
        """Offer the motors this profile's OFFLINE device discovery found.

        Names come from `device_source`'s static AST scan, so the list can only
        contain devices that really exist in this profile's search paths -- far
        safer than hand-typing a name that silently reads back blank. Axes are
        qualified the same way `skeleton_widgets.MotorAxisPicker` does.
        """
        catalog = device_source.get_catalog()
        targets: list[str] = []
        for name in catalog.names_for("motor"):
            axes = catalog.axes_for(name)
            targets.extend(f"{name}.{axis}" for axis in axes) if axes else targets.append(name)
        if not targets:
            QtWidgets.QMessageBox.information(
                self,
                "No motors found",
                "This profile's device search paths turned up no motors. "
                "Set them in Configuration -> Devices first.",
            )
            return
        picked, ok = QtWidgets.QInputDialog.getItem(
            self, "Add reading from device catalog", "Motor:", targets, 0, False
        )
        if not (ok and picked):
            return
        group = self._report_selected_group()
        if group is None:
            group = QtWidgets.QTreeWidgetItem(self._report_snapshots, ["Motors", "", "", ""])
            group.setFlags(group.flags() | QtCore.Qt.ItemIsEditable)
        # `.position` is the ophyd-wide readback property for a positioner and
        # avoids guessing between user_readback / readback / .get() shapes.
        item = self._report_make_reading(group, picked, f"{picked}.position", "float", "mm")
        group.setExpanded(True)
        self._report_snapshots.setCurrentItem(item)

    def _report_remove_row(self) -> None:
        item = self._report_snapshots.currentItem()
        if item is None:
            return
        parent = item.parent()
        if parent is None:
            self._report_snapshots.takeTopLevelItem(
                self._report_snapshots.indexOfTopLevelItem(item)
            )
        else:
            parent.removeChild(item)

    def _report_load_snapshots(self, groups: list) -> None:
        self._report_snapshots.clear()
        for group in groups or []:
            node = QtWidgets.QTreeWidgetItem(
                self._report_snapshots, [group.get("name") or "Readings", "", "", ""]
            )
            node.setFlags(node.flags() | QtCore.Qt.ItemIsEditable)
            node.setExpanded(True)
            for item in group.get("items") or []:
                self._report_make_reading(
                    node,
                    item.get("label") or "",
                    item.get("expr") or "",
                    item.get("kind") or "raw",
                    item.get("units") or "",
                )

    def _report_snapshot_values(self) -> list:
        """The tree back as the `report_snapshot_groups` list. Rows with no
        expression are dropped -- a half-typed row would only ever read blank."""
        groups = []
        for i in range(self._report_snapshots.topLevelItemCount()):
            node = self._report_snapshots.topLevelItem(i)
            items = []
            for j in range(node.childCount()):
                child = node.child(j)
                expr = child.text(1).strip()
                if not expr:
                    continue
                kind = child.text(2).strip().lower()
                items.append({
                    "label": child.text(0).strip() or expr,
                    "expr": expr,
                    "kind": kind if kind in self._REPORT_KINDS else "raw",
                    "units": child.text(3).strip(),
                })
            if items:
                groups.append({"name": node.text(0).strip() or "Readings", "items": items})
        return groups

    def _build_autopilot_card(self) -> QtWidgets.QWidget:
        card = S.make_card("AutoPILOT (optional AI chat panel)")
        self._autopilot_enabled = QtWidgets.QCheckBox(
            "Enable the AutoPILOT chat panel"
        )
        self._autopilot_enabled.setToolTip(
            "Adds a dockable AI chat panel that can draft Bluesky plans from "
            "natural-language requests. Off by default; takes effect "
            "immediately on Save, no restart needed."
        )
        if not autopilot_bridge.AVAILABLE:
            self._autopilot_enabled.setEnabled(False)
            self._autopilot_enabled.setToolTip(
                "AutoPILOT was not found (or its dependencies aren't "
                "installed) next to this B-PILOT checkout."
            )
        card.body.addWidget(self._autopilot_enabled)
        note = QtWidgets.QLabel(
            "AutoPILOT is a separate, optional add-on — B-PILOT works fully "
            "without it." if autopilot_bridge.AVAILABLE else
            "AutoPILOT/ is not present or not importable — nothing to enable."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {S.MUTED};")
        card.body.addWidget(note)
        return card

    # ── Buttons ──────────────────────────────────────────────────────────────────

    def _build_buttons(self) -> QtWidgets.QWidget:
        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save
            | QtWidgets.QDialogButtonBox.Cancel
            | QtWidgets.QDialogButtonBox.RestoreDefaults
        )
        box.button(QtWidgets.QDialogButtonBox.Save).setObjectName("primary")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        box.button(QtWidgets.QDialogButtonBox.RestoreDefaults).setToolTip(
            "Preview this profile's saved default_config.json. Nothing is "
            "written until Save."
        )
        box.button(QtWidgets.QDialogButtonBox.RestoreDefaults).clicked.connect(
            lambda: self._load_from(config.default_profile_values(self._current_profile))
        )
        return box

    # ── Load / collect values ────────────────────────────────────────────────────

    def _browse_button(self, target: QtWidgets.QLineEdit, kind: str = "dir"):
        btn = QtWidgets.QPushButton("Browse…")
        btn.clicked.connect(lambda: self._browse_into(target, kind))
        return btn

    def _browse_into(self, target: QtWidgets.QLineEdit, kind: str = "dir") -> None:
        start = target.text().strip() or os.path.expanduser("~")
        base = start if os.path.isdir(start) else os.path.dirname(start)
        if not os.path.isdir(base):
            base = os.path.expanduser("~")
        if kind == "file":
            chosen, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Select launch script", base
            )
        else:
            chosen = QtWidgets.QFileDialog.getExistingDirectory(
                self, "Select directory", base
            )
        if chosen:
            target.setText(chosen)

    def _load_from(self, cfg: dict) -> None:
        """Populate every tab's widgets from `cfg` (a full effective-config dict)."""
        self._bluesky_root.setText(cfg.get("bluesky_root") or "")
        self._plans_dir.setText(cfg["plans_dir"])
        self._import_root.setText(cfg["import_root"])
        self._default_file.setText(cfg["default_plan_file"])
        self._visible_files_initial = set(cfg.get("visible_plan_files") or [])
        self._visibility_items.clear()
        self._rebuild_visibility_list()

        self._startup.setPlainText(cfg["bluesky_startup"])
        self._send_import.setChecked(bool(cfg["send_import_line"]))
        self._keep_kernel.setChecked(bool(cfg["keep_kernel_on_exit"]))
        self._beamline.setText(cfg["beamline"])
        self._use_screen.setChecked(bool(cfg["use_screen"]))
        self._embedded_starter.setText(cfg["embedded_starter_script"])

        theme_idx = self._theme.findData(cfg.get("theme", "light"))
        self._theme.setCurrentIndex(theme_idx if theme_idx >= 0 else 0)
        font_idx = self._font_family.findData(cfg.get("font_family", "system"))
        self._font_family.setCurrentIndex(font_idx if font_idx >= 0 else 0)
        self._ui_scale.setValue(float(cfg["ui_scale"]))
        self._autopilot_enabled.setChecked(bool(cfg.get("autopilot_enabled", False)))
        self._report_enabled.setChecked(bool(cfg.get("report_enabled", True)))
        self._report_title.setText(cfg.get("report_title", "") or "")
        self._report_load_snapshots(cfg.get("report_snapshot_groups") or [])
        self._report_load_excluded(cfg.get("report_excluded_plans") or [])

        self._device_paths_widget.clear()
        self._device_paths_widget.addItems(cfg.get("device_search_paths") or [])
        self._device_selection_initial = dict(cfg.get("device_selection") or {})
        # Normalize a stray scalar (e.g. a hand-edited config, or one written
        # before multi-category support) to a one-item list.
        self._device_category_overrides = {
            k: (v if isinstance(v, list) else [v])
            for k, v in (cfg.get("device_category_overrides") or {}).items()
        }
        self._device_checks.clear()
        self._rebuild_device_list()

        self._plan_building_paths_widget.clear()
        self._plan_building_paths_widget.addItems(cfg.get("plan_building_search_paths") or [])
        self._suspender_paths_widget.clear()
        self._suspender_paths_widget.addItems(cfg.get("suspender_search_paths") or [])
        self._plan_building_blocks = dict(cfg.get("plan_building_blocks") or {})
        self._render_scan_blocks_display()

        backend_idx = self._queue_backend.findData(cfg.get("queue_backend") or "native")
        self._queue_backend.setCurrentIndex(backend_idx if backend_idx >= 0 else 0)
        self._qs_control_addr.setText(cfg.get("qs_zmq_control_addr") or "")
        self._qs_info_addr.setText(cfg.get("qs_zmq_info_addr") or "")
        self._qs_user.setText(cfg.get("qs_user") or "")
        self._qs_user_group.setText(cfg.get("qs_user_group") or "")
        self._on_queue_backend_changed()

        self._databroker_catalog.setText(cfg.get("databroker_catalog") or "")
        self._databroker_uri.setText(cfg.get("databroker_uri") or "")
        self._databroker_nexus_dir.setText(cfg.get("databroker_nexus_dir") or "")
        self._midas_bridge_enabled.setChecked(bool(cfg.get("midas_bridge_enabled", False)))

    def values(self) -> dict:
        """Return the edited settings (all tabs) as a config dict."""
        return {
            "bluesky_root": self._bluesky_root.text().strip(),
            "plans_dir": self._plans_dir.text().strip(),
            "import_root": self._import_root.text().strip(),
            "default_plan_file": self._default_file.text().strip(),
            "visible_plan_files": sorted(
                rel
                for rel, item in self._visibility_items.items()
                if item.checkState(0) == QtCore.Qt.Checked
            ),
            "bluesky_startup": self._startup.toPlainText().strip(),
            "send_import_line": self._send_import.isChecked(),
            "keep_kernel_on_exit": self._keep_kernel.isChecked(),
            "beamline": self._beamline.text().strip(),
            "use_screen": self._use_screen.isChecked(),
            "embedded_starter_script": self._embedded_starter.text().strip(),
            "theme": self._theme.currentData(),
            "font_family": self._font_family.currentData(),
            "ui_scale": self._ui_scale.value(),
            "autopilot_enabled": self._autopilot_enabled.isChecked(),
            "report_enabled": self._report_enabled.isChecked(),
            "report_title": self._report_title.text().strip(),
            "report_excluded_plans": self._report_excluded_names(),
            "report_snapshot_groups": self._report_snapshot_values(),
            "device_search_paths": [
                self._device_paths_widget.item(i).text()
                for i in range(self._device_paths_widget.count())
            ],
            "device_selection": {
                cat: {name: cb.isChecked() for name, cb in names.items()}
                for cat, names in self._device_checks.items()
            },
            "device_category_overrides": dict(self._device_category_overrides),
            "plan_building_search_paths": [
                self._plan_building_paths_widget.item(i).text()
                for i in range(self._plan_building_paths_widget.count())
            ],
            "suspender_search_paths": [
                self._suspender_paths_widget.item(i).text()
                for i in range(self._suspender_paths_widget.count())
            ],
            "plan_building_blocks": dict(self._plan_building_blocks),
            "queue_backend": self._queue_backend.currentData(),
            "qs_zmq_control_addr": self._qs_control_addr.text().strip(),
            "qs_zmq_info_addr": self._qs_info_addr.text().strip(),
            "qs_user": self._qs_user.text().strip(),
            "qs_user_group": self._qs_user_group.text().strip(),
            "databroker_catalog": self._databroker_catalog.text().strip(),
            "databroker_uri": self._databroker_uri.text().strip(),
            "databroker_nexus_dir": self._databroker_nexus_dir.text().strip(),
            "midas_bridge_enabled": self._midas_bridge_enabled.isChecked(),
        }

    def accept(self) -> None:  # noqa: D102
        values = self.values()
        config.set_active_profile(self._current_profile)
        config.update(values)
        # Only touch qs_client when the QS backend is actually selected --
        # calling it unconditionally would lazily create its background
        # worker thread (and start attempting a connection) even for a
        # Native-only user who just changed an unrelated setting.
        if values.get("queue_backend") == "qs":
            qs_client.reset()  # reconnect against any changed QS connection settings
        super().accept()
