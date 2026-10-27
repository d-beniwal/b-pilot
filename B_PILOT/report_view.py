"""The report's rendered view, with in-place editing of the text a person wrote.

``report_render`` marks every editable span (a note, a heading, a figure
caption, a run's own note) with two named anchors -- ``body:<id>:<m|s>`` on its
first character and ``end:<id>`` on a zero-width space after its last. This
view finds those spans and accepts typing **only inside one of them**: plan
data, snapshots, stamps and the report's own headings are never writable, so
"edit your text, but not the record" is enforced by the widget rather than
trusted to the caller.

An edit is a *session* on one span. It starts on the first accepted keystroke
(or ``begin_edit``) and ends -- committing -- when the caret leaves the span,
the view loses focus, or Ctrl/Cmd+Enter is pressed (plain Enter ends a
single-line span); Esc abandons it. The view never writes anything itself: it
emits :attr:`edit_finished` and the panel appends the overlay entry. While a
session is open the panel must not rebuild the document.

Two things the guard deliberately will not do: delete a span's last character
(a span can be replaced by typing over a selection, but not emptied -- removing
a note is a menu action) and accept rich-text paste (plain text only).
"""
from __future__ import annotations

from PyQt5 import QtCore
from PyQt5 import QtGui
from PyQt5 import QtWidgets

from . import report_builder as rb

_NAV_KEYS = {
    QtCore.Qt.Key_Left, QtCore.Qt.Key_Right, QtCore.Qt.Key_Up, QtCore.Qt.Key_Down,
    QtCore.Qt.Key_Home, QtCore.Qt.Key_End, QtCore.Qt.Key_PageUp,
    QtCore.Qt.Key_PageDown, QtCore.Qt.Key_Shift, QtCore.Qt.Key_Control,
    QtCore.Qt.Key_Meta, QtCore.Qt.Key_Alt, QtCore.Qt.Key_CapsLock,
}
_ENTER = (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter)
_LINE_SEP = " "


def _plain(text: str) -> str:
    return text.replace(" ", "\n").replace(" ", "\n")


class ReportView(QtWidgets.QTextBrowser):
    """``QTextBrowser`` whose editable spans can be typed into."""

    #: (entry id, final text, changed, fresh, committed)
    edit_finished = QtCore.pyqtSignal(str, str, bool, bool, bool)
    #: (entry id the click landed in or "" , global position)
    menu_requested = QtCore.pyqtSignal(str, QtCore.QPoint)
    #: a short reason an input was refused (shown as a tooltip by the panel)
    refused = QtCore.pyqtSignal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        # Editable flags *plus* link activation: the inline [hide]/[↑]/[↓]
        # controls and figure links must keep working in an editable view.
        self.setTextInteractionFlags(
            QtCore.Qt.TextEditorInteraction | QtCore.Qt.LinksAccessibleByMouse
        )
        self.setAcceptDrops(False)
        self._sess: dict | None = None
        self.document().contentsChange.connect(self._on_contents_change)
        self.cursorPositionChanged.connect(self._on_cursor_moved)

    # ------------------------------------------------------------- regions --

    @staticmethod
    def _scan_block(block: QtGui.QTextBlock) -> list[dict]:
        """Every editable span that lies in `block`."""
        found, open_ = [], {}
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            for name in frag.charFormat().anchorNames():
                if name.startswith("body:"):
                    _, ident, kind = name.split(":", 2)
                    fmt = QtGui.QTextCharFormat(frag.charFormat())
                    fmt.setAnchorNames([])
                    open_[ident] = {
                        "id": ident, "s": frag.position(),
                        "multi": kind == "m", "fmt": fmt,
                    }
                elif name.startswith("end:") and name[4:] in open_:
                    region = open_.pop(name[4:])
                    region["e"] = frag.position()
                    found.append(region)
            it += 1
        return found

    def region_at(self, position: int) -> dict | None:
        """The editable span containing `position` (ends inclusive), if any."""
        block = self.document().findBlock(position)
        for region in self._scan_block(block):
            if region["s"] <= position <= region["e"]:
                return region
        return None

    def _find_region(self, ident: str) -> dict | None:
        block = self.document().begin()
        while block.isValid():
            for region in self._scan_block(block):
                if region["id"] == ident:
                    return region
            block = block.next()
        return None

    def _text(self, start: int, end: int) -> str:
        cur = QtGui.QTextCursor(self.document())
        cur.setPosition(start)
        cur.setPosition(end, QtGui.QTextCursor.KeepAnchor)
        return _plain(cur.selectedText())

    def _selection(self) -> tuple[int, int]:
        cur = self.textCursor()
        return min(cur.anchor(), cur.position()), max(cur.anchor(), cur.position())

    def _region_for_selection(self) -> dict | None:
        """The span a typed change would land in -- or ``None`` to refuse it."""
        a, b = self._selection()
        if self._sess:
            r = self._sess
            return r if r["s"] <= a and b <= r["e"] else None
        r = self.region_at(a)
        return r if r and b <= r["e"] else None

    # ------------------------------------------------------------- session --

    def has_session(self) -> bool:
        return self._sess is not None

    def _start(self, region: dict, *, fresh: bool = False) -> dict:
        self._sess = dict(
            region, fresh=fresh, orig=self._text(region["s"], region["e"])
        )
        return self._sess

    def begin_edit(self, ident: str, *, select_all: bool = False, fresh: bool = False) -> bool:
        """Put the caret in span `ident` (optionally selecting it) and open a session."""
        region = self._find_region(ident)
        if region is None:
            return False
        self._sess = None
        self._start(region, fresh=fresh)
        cur = QtGui.QTextCursor(self.document())
        if select_all:
            cur.setPosition(region["s"])
            cur.setPosition(region["e"], QtGui.QTextCursor.KeepAnchor)
        else:
            cur.setPosition(region["e"])
        self.setTextCursor(cur)
        self.setFocus()
        self.ensureCursorVisible()
        return True

    def end_session(self, *, commit: bool = True) -> None:
        sess, self._sess = self._sess, None
        if sess is None:
            return
        text = self._text(sess["s"], sess["e"])
        self.edit_finished.emit(
            sess["id"], text, text != sess["orig"], bool(sess["fresh"]), commit
        )

    def _end_later(self) -> None:
        QtCore.QTimer.singleShot(0, self._maybe_end)

    def _maybe_end(self) -> None:
        sess = self._sess
        if sess is None:
            return
        pos = self.textCursor().position()
        if not (sess["s"] <= pos <= sess["e"]) or not self.hasFocus():
            self.end_session(commit=True)

    def _on_cursor_moved(self) -> None:
        sess = self._sess
        if sess is not None and not (sess["s"] <= self.textCursor().position() <= sess["e"]):
            self._end_later()

    def _on_contents_change(self, pos: int, removed: int, added: int) -> None:
        sess = self._sess
        if sess and sess["s"] <= pos and pos + removed <= sess["e"]:
            sess["e"] += added - removed

    def focusOutEvent(self, event) -> None:  # noqa: N802 -- Qt's name
        super().focusOutEvent(event)
        if event.reason() != QtCore.Qt.PopupFocusReason and self._sess:
            self._end_later()

    # --------------------------------------------------------------- input --

    def _refuse(self, why: str) -> None:
        self.refused.emit(why)

    def _insert(self, text: str) -> bool:
        """Insert plain `text` at the caret, inside the span only."""
        region = self._region_for_selection()
        if region is None:
            self._refuse("Only text you wrote can be edited here.")
            return False
        if not self._sess:
            self._start(region)
        if not region["multi"]:
            text = text.replace("\n", " ").replace(_LINE_SEP, " ")
        else:
            text = text.replace("\r\n", "\n").replace("\n", _LINE_SEP)
        text = rb.clean_text(text)
        cur = self.textCursor()
        cur.insertText(text, self._sess["fmt"])
        self.setTextCursor(cur)
        return True

    def _delete(self, forward: bool) -> None:
        region = self._region_for_selection()
        if region is None:
            self._refuse("Only text you wrote can be edited here.")
            return
        a, b = self._selection()
        if a == b:
            if (forward and b >= region["e"]) or (not forward and a <= region["s"]):
                return
            a, b = (a, b + 1) if forward else (a - 1, b)
        if b - a >= region["e"] - region["s"]:
            self._refuse("Can't empty it — right-click ▸ Delete to remove it.")
            return
        if not self._sess:
            self._start(region)
        cur = QtGui.QTextCursor(self.document())
        cur.setPosition(a)
        cur.setPosition(b, QtGui.QTextCursor.KeepAnchor)
        cur.removeSelectedText()
        self.setTextCursor(cur)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        key, mods = event.key(), event.modifiers()
        ctrl = bool(mods & (QtCore.Qt.ControlModifier | QtCore.Qt.MetaModifier))

        if key == QtCore.Qt.Key_Escape:
            if self._sess:
                self.end_session(commit=False)
            return
        if event.matches(QtGui.QKeySequence.Copy) or event.matches(QtGui.QKeySequence.SelectAll):
            super().keyPressEvent(event)
            return
        if key in _NAV_KEYS:
            super().keyPressEvent(event)
            return
        if event.matches(QtGui.QKeySequence.Undo) or event.matches(QtGui.QKeySequence.Redo):
            if self._sess:
                super().keyPressEvent(event)
            return
        if key in _ENTER:
            region = self._region_for_selection()
            if region is None:
                return
            if ctrl or not region["multi"]:
                if self._sess:
                    self.end_session(commit=True)
                return
            self._insert("\n")
            return
        if event.matches(QtGui.QKeySequence.Paste):
            text = QtWidgets.QApplication.clipboard().text()
            if text:
                self._insert(text)
            return
        if event.matches(QtGui.QKeySequence.Cut):
            region = self._region_for_selection()
            a, b = self._selection()
            if region and a != b and b - a < region["e"] - region["s"]:
                QtWidgets.QApplication.clipboard().setText(self._text(a, b))
                self._delete(forward=True)
            return
        if ctrl:
            return  # every other Ctrl/Cmd chord could edit in ways we can't bound
        if key in (QtCore.Qt.Key_Backspace, QtCore.Qt.Key_Delete):
            if mods & QtCore.Qt.AltModifier:
                return  # word-deletes could run past the span's edge
            self._delete(forward=key == QtCore.Qt.Key_Delete)
            return
        text = event.text()
        if text and text.isprintable():
            self._insert(text)

    def inputMethodEvent(self, event) -> None:  # noqa: N802
        # Composed input (dead keys, IME) goes through Qt's own path, so only
        # let it start where typing is allowed.
        if self._region_for_selection() is None:
            event.ignore()
            return
        if not self._sess:
            self._start(self._region_for_selection())
        super().inputMethodEvent(event)

    def insertFromMimeData(self, source: QtCore.QMimeData) -> None:  # noqa: N802
        if source.hasText():
            self._insert(source.text())

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        event.ignore()

    dragMoveEvent = dragEnterEvent  # noqa: N815
    dropEvent = dragEnterEvent  # noqa: N815

    # ---------------------------------------------------------------- mouse --

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        super().mouseMoveEvent(event)
        if self.anchorAt(event.pos()):
            return  # keep Qt's pointing hand over a control or figure link
        over = self.region_at(self.cursorForPosition(event.pos()).position())
        self.viewport().setCursor(
            QtCore.Qt.IBeamCursor if over else QtCore.Qt.ArrowCursor
        )

    def entry_at(self, position: int) -> str:
        """Id of the report entry whose block `position` falls in.

        Every entry's heading line carries its ``bpilot:<verb>/<id>`` control
        links, so the nearest one at or above the position names the entry.
        """
        block = self.document().findBlock(position)
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                href = it.fragment().charFormat().anchorHref()
                if href.startswith(f"{rb.CONTROL_SCHEME}:") and "/" in href:
                    return href.rpartition("/")[2]
                it += 1
            block = block.previous()
        return ""

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:  # noqa: N802
        position = self.cursorForPosition(event.pos()).position()
        self.menu_requested.emit(self.entry_at(position), event.globalPos())
