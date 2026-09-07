"""Capture a snapshot of live beamline readings into the experiment report.

**How the values are read, and why that is safe.** B-PILOT never talks to EPICS
and never opens a channel of its own. This dialog asks the kernel *the user
already launched* to evaluate a list of read-only expressions, through
:meth:`console_panel.ConsolePanel.query_values` -- the same mechanism
``mode_buttons.py`` and ``contacq_popup.py`` have always used for their status
polling. The expressions come from the active profile, so they are the user's
own configuration, evaluated in the user's own session.

``query_values`` is silent by construction: an empty code string with
``silent=True`` suppresses IOPub entirely, so a snapshot never appears in the
console, the Session log, ``history.jsonl``, or the ``In [n]`` counter.

**The decoding constraint.** The reply carries each expression's ``repr``,
which ``_dispatch_query_reply`` runs through :func:`ast.literal_eval`. Only
Python *literals* survive that -- an ophyd ``Device``, a numpy scalar, or a
namedtuple decodes to ``None``. Every configured reading therefore declares a
``kind`` that this dialog wraps the expression in (``float(...)``, ``str(...)``
and so on), exactly as the existing call sites do by hand:
``bool(BEAMMODE.get())`` in ``mode_buttons.py:100``, the ``as_string`` unwrap in
``contacq_popup.py:91``.

**The one real limitation.** A kernel executes ``execute_request`` messages
serially, so a snapshot requested while a plan is mid-cell does not come back
until that cell finishes. That is the kernel's behaviour, not something this
dialog can work around; it is surfaced as an explicit timeout message rather
than a hung dialog.
"""
from __future__ import annotations

from PyQt5 import QtCore
from PyQt5 import QtWidgets

from . import config
from . import style as S

# How long to wait for the kernel's reply before telling the user why nothing
# came back. Generous enough for a loaded kernel, short enough that a plan
# already running is diagnosed rather than waited out.
_REPLY_TIMEOUT_MS = 8000

_MISSING = "—"

# Coercions that turn an arbitrary device reading into something whose repr
# survives ast.literal_eval. "raw" is the escape hatch for an expression that
# is already literal-shaped (a dict, a plain number).
_WRAPPERS = {
    "float": "float({})",
    "int": "int({})",
    "str": "str({})",
    "bool": "bool({})",
    "raw": "{}",
}


def wrap_expression(expr: str, kind: str) -> str:
    """The expression actually sent to the kernel, coerced for `kind`."""
    return _WRAPPERS.get(kind or "raw", "{}").format(expr)


def format_value(value, kind: str, fmt: str = "") -> str:
    """Render one captured value for the report table.

    ``None`` means the expression errored in the kernel or its repr was not a
    literal; it shows as an em dash rather than being dropped, so a reader can
    see that the reading was attempted and did not resolve.
    """
    if value is None:
        return _MISSING
    if fmt:
        try:
            return fmt.format(value)
        except (ValueError, TypeError, KeyError, IndexError):
            return str(value)
    if kind == "float":
        try:
            return f"{float(value):.4f}"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


class SnapshotDialog(QtWidgets.QDialog):
    """Pick readings, capture them from the kernel, review, then insert."""

    def __init__(self, console, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Beamline snapshot")
        self.setMinimumWidth(S.px(520))

        self._console = console
        self._rows: list[list[str]] = []
        self._token = 0
        # Guards the group<->reading check cascade against re-entering itself:
        # setCheckState emits itemChanged, which is the very signal driving it.
        self._syncing = False

        layout = QtWidgets.QVBoxLayout(self)

        hint = QtWidgets.QLabel(
            "Tick the readings to capture. Values are read from the running "
            "kernel — configure the list in Configuration → Reports."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{S.MUTED};")
        layout.addWidget(hint)

        self._tree = QtWidgets.QTreeWidget()
        self._tree.setHeaderLabels(["Reading", "Expression"])
        self._tree.setRootIsDecorated(True)
        self._tree.setColumnWidth(0, S.px(200))
        layout.addWidget(self._tree, 1)
        self._populate()
        # Connected after populating, so seeding the initial check states does
        # not fire the cascade once per row.
        self._tree.itemChanged.connect(self._on_item_changed)

        self._title = QtWidgets.QLineEdit()
        self._title.setPlaceholderText("Snapshot title (optional), e.g. 'after realignment'")
        layout.addWidget(self._title)

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        buttons = QtWidgets.QHBoxLayout()
        self._capture_btn = QtWidgets.QPushButton("Capture")
        self._capture_btn.setToolTip("Read the ticked values from the kernel now.")
        self._capture_btn.clicked.connect(self._on_capture)
        buttons.addWidget(self._capture_btn)
        buttons.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self._insert_btn = QtWidgets.QPushButton("Insert into report")
        self._insert_btn.setObjectName("primary")
        self._insert_btn.setEnabled(False)
        self._insert_btn.clicked.connect(self.accept)
        buttons.addWidget(self._insert_btn)
        layout.addLayout(buttons)

    # ------------------------------------------------------------ selection --

    def _populate(self) -> None:
        """One top-level node per configured group, checked by default."""
        groups = config.get("report_snapshot_groups") or []
        if not groups:
            placeholder = QtWidgets.QTreeWidgetItem(
                self._tree, ["No readings configured", "Configuration → Reports"]
            )
            placeholder.setDisabled(True)
            return
        for group in groups:
            parent = QtWidgets.QTreeWidgetItem(self._tree, [group.get("name") or "Readings", ""])
            parent.setFlags(parent.flags() | QtCore.Qt.ItemIsUserCheckable)
            parent.setCheckState(0, QtCore.Qt.Checked)
            parent.setExpanded(True)
            for item in group.get("items") or []:
                kind = item.get("kind") or "raw"
                child = QtWidgets.QTreeWidgetItem(
                    parent,
                    [item.get("label") or item.get("expr") or "?", wrap_expression(item.get("expr") or "", kind)],
                )
                child.setFlags(child.flags() | QtCore.Qt.ItemIsUserCheckable)
                child.setCheckState(0, QtCore.Qt.Checked)
                child.setData(0, QtCore.Qt.UserRole, item)

    def _on_item_changed(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        """Keep a group and its readings in step, in both directions.

        Ticking a group ticks every reading under it (and unticking clears
        them) -- with several groups configured, that is the difference between
        one click and a dozen. Ticking readings individually leaves the group
        showing partially-checked, so the header still tells the truth about
        what is selected.
        """
        if column != 0 or self._syncing:
            return
        self._syncing = True
        try:
            if item.childCount():
                state = item.checkState(0)
                if state == QtCore.Qt.PartiallyChecked:
                    return  # only ever set by this method, never by a click
                for j in range(item.childCount()):
                    item.child(j).setCheckState(0, state)
                return
            parent = item.parent()
            if parent is None:
                return
            checked = sum(
                1
                for j in range(parent.childCount())
                if parent.child(j).checkState(0) == QtCore.Qt.Checked
            )
            parent.setCheckState(
                0,
                QtCore.Qt.Checked
                if checked == parent.childCount()
                else QtCore.Qt.Unchecked
                if checked == 0
                else QtCore.Qt.PartiallyChecked,
            )
        finally:
            self._syncing = False

    def _selected_items(self) -> list[dict]:
        out: list[dict] = []
        for i in range(self._tree.topLevelItemCount()):
            parent = self._tree.topLevelItem(i)
            for j in range(parent.childCount()):
                child = parent.child(j)
                if child.checkState(0) != QtCore.Qt.Checked:
                    continue
                item = child.data(0, QtCore.Qt.UserRole)
                if item and item.get("expr"):
                    out.append(item)
        return out

    # -------------------------------------------------------------- capture --

    def _on_capture(self) -> None:
        items = self._selected_items()
        if not items:
            self._status.setText("Tick at least one reading first.")
            return
        if self._console is None or not self._console.is_running():
            self._status.setText("No kernel is running — nothing to read from.")
            return

        # Labels can repeat across groups; key by index so a duplicate label
        # cannot silently overwrite another reading's value.
        exprs = {
            f"r{n}": wrap_expression(item["expr"], item.get("kind") or "raw")
            for n, item in enumerate(items)
        }

        self._token += 1
        token = self._token
        self._capture_btn.setEnabled(False)
        self._insert_btn.setEnabled(False)
        busy = ""
        try:
            busy = " (the kernel is busy — this waits for the running cell)" if self._console.is_busy() else ""
        except AttributeError:
            pass
        self._status.setText(f"Reading {len(items)} value(s) from the kernel…{busy}")

        QtCore.QTimer.singleShot(_REPLY_TIMEOUT_MS, lambda: self._on_timeout(token))
        self._console.query_values(exprs, lambda result: self._on_values(token, items, result))

    def _on_timeout(self, token: int) -> None:
        if token != self._token or self._rows:
            return
        self._token += 1  # invalidate the in-flight reply
        self._capture_btn.setEnabled(True)
        self._status.setText(
            "The kernel did not answer in time. A kernel executes one cell at a "
            "time, so if a plan is running the reading only returns once that "
            "cell finishes. Try again when it is idle."
        )

    def _on_values(self, token: int, items: list[dict], result: dict) -> None:
        if token != self._token:
            return
        self._rows = [
            [
                item.get("label") or item.get("expr") or "?",
                format_value(result.get(f"r{n}"), item.get("kind") or "raw", item.get("fmt") or ""),
                item.get("units") or "",
            ]
            for n, item in enumerate(items)
        ]
        missing = sum(1 for row in self._rows if row[1] == _MISSING)
        self._capture_btn.setEnabled(True)
        self._insert_btn.setEnabled(True)
        self._preview()
        if missing:
            self._status.setText(
                f"{len(self._rows) - missing} of {len(self._rows)} read. "
                f"{missing} did not resolve — the name may not exist in this "
                "kernel, or the value's repr is not a literal (set its Kind in "
                "Configuration → Reports)."
            )
        else:
            self._status.setText(f"Captured {len(self._rows)} reading(s).")

    def _preview(self) -> None:
        """Show what was actually read, so nothing is inserted unseen."""
        self._syncing = True   # these rows are a result table, not a selection
        self._tree.clear()
        self._tree.setHeaderLabels(["Reading", "Value"])
        for label, value, units in self._rows:
            QtWidgets.QTreeWidgetItem(
                self._tree, [label, f"{value} {units}".strip()]
            )
        self._syncing = False

    # --------------------------------------------------------------- result --

    def captured_rows(self) -> list[list[str]]:
        """``[[label, value, units], ...]`` as captured, or ``[]``."""
        return list(self._rows)

    def captured_title(self) -> str:
        return self._title.text().strip() or "Beamline snapshot"
