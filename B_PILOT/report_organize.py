"""Arranging the experiment report: drag-to-reorder, hide, and place-anywhere.

Two widgets, both driven entirely by ids that :mod:`report_builder` produced,
and neither of which writes anything. They emit what the user asked for --
"move this after that", "hide this", "file the new note here" -- and
:mod:`report_panel` turns that into the append-only overlay entries that
actually change the record (:func:`report_builder.plan_move`,
:func:`report_store.append_edits`). Keeping the maths in ``report_builder``
and the persistence in ``report_panel`` is what lets the ordering rules be
tested without a display.

**Why a list beside the report rather than dragging in the report itself.**
The rendered report is a ``QTextBrowser``; Qt's rich-text engine has no notion
of a draggable block, so reordering by dragging the rendered prose is not
available at any price. A list view gives real drag-and-drop for free, and it
doubles as the thing the "insert here" picker is built from -- the two features
need the same one-line-per-entry summary, so they share :func:`entry_label`.
"""
from __future__ import annotations

import time

from PyQt5 import QtCore
from PyQt5 import QtWidgets

from . import report_builder as rb
from . import report_store as rs
from . import style as S

#: One glyph per entry kind, so a long report can be skimmed by shape. Matches
#: the markers `report_builder` renders into the document itself.
_ICONS = {
    rs.RUN: "▶",
    rs.NOTE: "✎",
    rs.SNAPSHOT: "📸",
    rs.IMAGE: "🖼",
    rs.HEADING: "§",
    rs.AGENT: "✨",
}

#: Longest summary shown in a row before it is elided.
_LABEL_CHARS = 60


def entry_label(entry: dict) -> str:
    """One-line summary of an entry, for a list row or a picker item."""
    kind = entry.get("kind")
    stamp = time.strftime("%H:%M:%S", time.localtime(entry.get("ts") or 0.0))
    icon = _ICONS.get(kind, "•")

    if kind == rs.RUN:
        body = entry.get("plan_name") or "run"
    elif kind == rs.HEADING:
        body = entry.get("title") or entry.get("text") or "section"
    elif kind in (rs.SNAPSHOT, rs.IMAGE):
        body = entry.get("title") or ("snapshot" if kind == rs.SNAPSHOT else "figure")
    else:
        body = (entry.get("title") or entry.get("text") or "").strip()
        body = next((ln for ln in body.splitlines() if ln.strip()), "") or "note"

    if len(body) > _LABEL_CHARS:
        body = body[: _LABEL_CHARS - 1] + "…"
    return f"{stamp}  {icon}  {body}"


def signature(entries: list[dict]) -> tuple:
    """Cheap fingerprint of "what the list should be showing".

    The Report panel polls once a second. Repopulating a ``QListWidget`` that
    often would cancel a drag in progress and throw away the selection on every
    tick, so the panel rebuilds only when this value changes -- which is order,
    membership, hidden state and label, i.e. exactly what a row displays.
    """
    return tuple(
        (rb.entry_id(e), bool(e.get("hidden")), entry_label(e))
        for e in rb.ordered(entries)
    )


class ArrangeList(QtWidgets.QListWidget):
    """Drag-to-reorder list of report entries, with a visibility checkbox each.

    Emits intent only; the caller persists it. ``moved`` carries the entry that
    was dragged and the entry it now follows (``""`` for the very top), which is
    the shape :func:`report_builder.plan_move` takes.
    """

    #: (moved entry id, id it now follows -- "" means the top of the report)
    moved = QtCore.pyqtSignal(str, str)
    #: (entry id, visible)
    visibility_changed = QtCore.pyqtSignal(str, bool)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.setDefaultDropAction(QtCore.Qt.MoveAction)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setMinimumHeight(S.px(120))
        self.setToolTip(
            "Drag an entry to move it in the report. Untick to hide it.\n"
            "Timestamps never change — only where the entry reads."
        )
        # Suppresses `visibility_changed` while `populate` builds the rows;
        # `setCheckState` emits `itemChanged`, which is the same signal a real
        # click arrives on.
        self._loading = False
        self.itemChanged.connect(self._on_item_changed)

    # ------------------------------------------------------------- contents --

    def ids(self) -> list[str]:
        """Row ids, top to bottom."""
        return [
            self.item(i).data(QtCore.Qt.UserRole) for i in range(self.count())
        ]

    def selected_id(self) -> str:
        item = self.currentItem()
        return item.data(QtCore.Qt.UserRole) if item is not None else ""

    def populate(self, entries: list[dict]) -> None:
        """Rebuild the rows from `entries` (any order), keeping the selection."""
        keep = self.selected_id()
        self._loading = True
        try:
            self.clear()
            for entry in rb.ordered(entries):
                identity = rb.entry_id(entry)
                item = QtWidgets.QListWidgetItem(entry_label(entry))
                item.setData(QtCore.Qt.UserRole, identity)
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                hidden = bool(entry.get("hidden"))
                item.setCheckState(
                    QtCore.Qt.Unchecked if hidden else QtCore.Qt.Checked
                )
                if hidden:
                    item.setForeground(QtWidgets.QApplication.palette().mid())
                    item.setToolTip(
                        entry.get("hidden_reason") or "Hidden from the report."
                    )
                self.addItem(item)
                if identity == keep:
                    self.setCurrentItem(item)
        finally:
            self._loading = False

    # -------------------------------------------------------------- signals --

    def _on_item_changed(self, item: QtWidgets.QListWidgetItem) -> None:
        if self._loading:
            return
        self.visibility_changed.emit(
            item.data(QtCore.Qt.UserRole), item.checkState() == QtCore.Qt.Checked
        )

    def dropEvent(self, event) -> None:  # noqa: N802 -- Qt's name
        """Let Qt do the move, then report what actually changed.

        The order is diffed before and after rather than derived from the drop
        index: ``InternalMove`` has several ways to land an item (above a row,
        below it, past the end) and each computes its index differently, while
        the resulting order is unambiguous.
        """
        before = self.ids()
        super().dropEvent(event)
        after = self.ids()
        if before == after:
            return
        identity = _moved_id(before, after)
        if not identity:
            return
        index = after.index(identity)
        self.moved.emit(identity, after[index - 1] if index > 0 else "")


def _moved_id(before: list[str], after: list[str]) -> str:
    """The one id whose removal makes the two orders identical.

    A drag moves exactly one row, so the moved entry is the only one that can
    account for the difference. Returns ``""`` if no single row explains it
    (which should not happen, and is better ignored than guessed at).
    """
    for identity in after:
        if [x for x in before if x != identity] == [x for x in after if x != identity]:
            return identity
    return ""


class EntryDialog(QtWidgets.QDialog):
    """Ask for an entry's text and *where in the report it goes*.

    The placement combo is the whole point: a screenshot is very often taken
    minutes after the scan it illustrates, and filing it at "now" is what makes
    a lab notebook stop matching the experiment. Choosing a position writes a
    ``pos`` override -- the entry's timestamp still records when it was
    actually captured.
    """

    def __init__(
        self,
        title: str,
        prompt: str,
        entries: list[dict],
        *,
        multiline: bool = True,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QtWidgets.QVBoxLayout(self)

        layout.addWidget(QtWidgets.QLabel(prompt))
        if multiline:
            self._text: QtWidgets.QWidget = QtWidgets.QPlainTextEdit()
            self._text.setMinimumHeight(S.px(90))
        else:
            self._text = QtWidgets.QLineEdit()
        layout.addWidget(self._text)

        place_row = QtWidgets.QHBoxLayout()
        place_row.addWidget(QtWidgets.QLabel("Insert:"))
        self._place = QtWidgets.QComboBox()
        self._place.setToolTip(
            "Where this lands in the report. Its timestamp is unaffected —\n"
            "the record still shows when it was captured."
        )
        self._place.addItem("At the end (now)", "")
        # Newest first: the entry a user wants to file something beside is
        # nearly always recent, and a long beamtime's report is a long list.
        for entry in reversed(rb.ordered(entries)):
            self._place.addItem(f"After:  {entry_label(entry)}", rb.entry_id(entry))
        place_row.addWidget(self._place, 1)
        layout.addLayout(place_row)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._text.setFocus()

    def text(self) -> str:
        if isinstance(self._text, QtWidgets.QPlainTextEdit):
            return self._text.toPlainText().strip()
        return self._text.text().strip()

    def after_id(self) -> str:
        """Id of the entry the new one should follow (``""`` = the end)."""
        return self._place.currentData() or ""
