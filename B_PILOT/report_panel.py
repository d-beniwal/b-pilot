"""The Experiment Report dock -- a live lab record, side by side with the run.

A ``QDockWidget`` rather than another tab in the console tab bar, for one
reason: the point of this panel is to be readable *while* a plan is running,
which a tab that hides the console cannot do. Docking also gets floating onto
a second monitor for free, which is how a beamline desk is actually laid out.

The structure mirrors ``AutoPILOT/autopilot/gui/chat_panel.py``'s
``ChatDockWidget`` -- the app's only other dock, and the pattern
``main_window.py`` already knows how to host (allowed areas, ribbon tab,
menu toggle, config-backed visibility).

**What updates it.** Plan runs are not pushed here by the GUI; they are folded
out of ``history.jsonl`` and reconciled into the report's master file by
:func:`report_builder.collect`. That indirection is what makes the panel show
plans dispatched by the *detached queue runner*, and plans that ran while the
GUI was closed, with no extra plumbing. The panel just polls both files for a
size change (:func:`report_store.source_state`) and rebuilds when one moved.

Nothing rendered is written to disk. ``report.jsonl`` is the master and the
only thing B-PILOT keeps; Markdown and HTML exist only in this view and in
whatever the Export button writes where the user asks for it.
"""
from __future__ import annotations

import os
import time

from PyQt5 import QtCore
from PyQt5 import QtGui
from PyQt5 import QtWidgets

from . import config
from . import report_builder
from . import report_images as ri
from . import report_organize as ro
from . import report_render
from . import report_store as rs
from . import style as S
from .snapshot_dialog import SnapshotDialog

_POLL_MS = 1000


class ReportDockWidget(QtWidgets.QDockWidget):
    """Live, rendered experiment report with one-click note/snapshot capture."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__("Experiment Report", parent)
        self.setObjectName("ReportDock")
        self.setTitleBarWidget(self._build_title_bar())

        self._beamline: str | None = None
        self._experiment: str | None = None
        self._console = None          # set by main_window via set_console()
        self._console_ready = False
        self._sources: tuple[int, int] = (-1, -1)
        # Last resolved entry list. Everything that acts on a specific block --
        # the inline controls, the Arrange list, the placement pickers -- works
        # from this rather than re-reading the file, so a click can never act
        # on an ordering the user is not currently looking at.
        self._entries: list[dict] = []
        self._arrange_signature: tuple = ()

        body = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(body)

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._subject = QtWidgets.QLabel("No experiment")
        self._subject.setStyleSheet(f"color:{S.MUTED};")
        header.addWidget(self._subject)
        header.addStretch(1)
        self._show_hidden = QtWidgets.QCheckBox("Show hidden")
        self._show_hidden.setToolTip(
            "Bring hidden entries back into view, marked and with the reason "
            "they were suppressed.\nHiding never deletes anything — every entry "
            "stays in the report file on disk.\nExports always leave hidden "
            "entries out."
        )
        self._show_hidden.toggled.connect(self.refresh)
        header.addWidget(self._show_hidden)
        self._follow = QtWidgets.QCheckBox("Follow")
        self._follow.setChecked(True)
        self._follow.setToolTip(
            "Keep the newest entry in view as the report grows.\n"
            "Uncheck to read back through earlier entries undisturbed."
        )
        header.addWidget(self._follow)
        layout.addLayout(header)

        self._view = QtWidgets.QTextBrowser()
        self._view.setOpenExternalLinks(False)
        # Figures are wrapped in a link to their own file so a click opens them
        # full size. `setOpenLinks(False)` is what stops QTextBrowser from
        # instead *navigating* to that file and replacing the whole report with
        # it, which is its default and has no back button here.
        self._view.setOpenLinks(False)
        self._view.anchorClicked.connect(self._on_anchor)
        self._view.setPlaceholderText(
            "The experiment report builds itself as you run plans.\n\n"
            "Run notes from the plan form land here automatically. Use the "
            "buttons below to add a note, a figure, a section heading, or a "
            "snapshot of live beamline readings."
        )
        layout.addWidget(self._view, 1)

        # Hidden until asked for: most of the time the report is something to
        # read, and the arranging tools would only crowd it.
        self._arrange = ro.ArrangeList()
        self._arrange.setVisible(False)
        self._arrange.moved.connect(self._on_arrange_moved)
        self._arrange.visibility_changed.connect(self._on_arrange_visibility)
        layout.addWidget(self._arrange)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        self._snapshot_btn = self._button(
            "📸 Snapshot",
            "Capture live beamline readings into the report.\n"
            "Reads values from the running kernel only -- never from EPICS directly.",
            self._on_snapshot,
        )
        self._snapshot_btn.setEnabled(False)
        buttons.addWidget(self._snapshot_btn)
        buttons.addWidget(self._image_button())
        buttons.addWidget(self._button("✎ Note", "Add a free note at this point in the report.", self._on_note))
        buttons.addWidget(
            self._button("§ Heading", "Start a new titled section in the report.", self._on_heading)
        )
        buttons.addStretch(1)
        self._arrange_btn = QtWidgets.QPushButton("⇅ Arrange")
        self._arrange_btn.setCheckable(True)
        self._arrange_btn.setToolTip(
            "Show the entry list: drag entries to reorder the report, "
            "untick one to hide it.\nCapture timestamps are never changed."
        )
        self._arrange_btn.toggled.connect(self._on_arrange_toggled)
        buttons.addWidget(self._arrange_btn)
        buttons.addWidget(
            self._button("⟳ Rebuild", "Re-read the kernel history and rebuild the report now.", self.refresh)
        )
        buttons.addWidget(self._button("⤓ Export", "Save a standalone copy of this report.", self._on_export))
        layout.addLayout(buttons)

        self.setWidget(body)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._poll)

    # ---------------------------------------------------------------- setup --

    @staticmethod
    def _button(text: str, tip: str, slot) -> QtWidgets.QPushButton:
        btn = QtWidgets.QPushButton(text)
        btn.setToolTip(tip)
        btn.clicked.connect(slot)
        return btn

    def _image_button(self) -> QtWidgets.QPushButton:
        """The figure button: a menu, because there are two ways to get pixels.

        Pasting is listed first and bound to Ctrl+V because it is the path that
        works everywhere -- the user grabs a region with their own OS shortcut
        and the clipboard carries it here, with no screen-capture permission
        for B-PILOT to be denied and nothing to break under Wayland.
        """
        btn = QtWidgets.QPushButton("🖼 Figure")
        btn.setToolTip(
            "Add a figure to the report.\n"
            "Take a screenshot with your usual shortcut, then paste it here."
        )
        menu = QtWidgets.QMenu(btn)
        paste = menu.addAction("Paste from clipboard\tCtrl+V")
        paste.triggered.connect(self._on_paste_image)
        attach = menu.addAction("Attach image file…")
        attach.triggered.connect(self._on_attach_image)
        btn.setMenu(menu)

        # Scoped to the dock so it cannot steal Ctrl+V from the console or any
        # text field elsewhere in the window.
        shortcut = QtWidgets.QShortcut(QtGui.QKeySequence.Paste, self)
        shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self._on_paste_image)
        return btn

    def _build_title_bar(self) -> QtWidgets.QWidget:
        """Slim custom title bar with a float/dock toggle, as the chat dock does.

        Same reason as there: the native float button's redock behaviour is not
        reliable across window managers, so this calls ``setFloating()`` itself.
        """
        bar = QtWidgets.QWidget()
        bar.setObjectName("ReportTitleBar")
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(8, 3, 3, 3)
        lay.setSpacing(0)

        label = QtWidgets.QLabel("Experiment Report")
        label.setObjectName("ReportTitleLabel")
        label.setStyleSheet(f"font-weight:bold; color:{S.TEXT};")
        lay.addWidget(label)
        lay.addStretch(1)

        icon_size = QtCore.QSize(S.px(12), S.px(12))
        btn_size = S.px(18)

        self._dock_toggle_btn = QtWidgets.QToolButton()
        self._dock_toggle_btn.setAutoRaise(True)
        self._dock_toggle_btn.setFixedSize(btn_size, btn_size)
        self._dock_toggle_btn.setIconSize(icon_size)
        self._dock_toggle_btn.setIcon(
            self.style().standardIcon(QtWidgets.QStyle.SP_TitleBarNormalButton)
        )
        self._dock_toggle_btn.clicked.connect(lambda: self.setFloating(not self.isFloating()))
        lay.addWidget(self._dock_toggle_btn)

        close_btn = QtWidgets.QToolButton()
        close_btn.setAutoRaise(True)
        close_btn.setFixedSize(btn_size, btn_size)
        close_btn.setIconSize(icon_size)
        close_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_TitleBarCloseButton))
        close_btn.setToolTip("Close (hide) this panel.")
        close_btn.clicked.connect(self.close)
        lay.addWidget(close_btn)

        self.topLevelChanged.connect(self._update_dock_toggle_button)
        self._update_dock_toggle_button()
        return bar

    def _update_dock_toggle_button(self, *_args) -> None:
        self._dock_toggle_btn.setToolTip(
            "Dock this panel back into the main window."
            if self.isFloating()
            else "Undock this panel into a floating window."
        )

    def set_console(self, console) -> None:
        """Give the panel the kernel console it should read snapshots from."""
        self._console = console

    def set_console_ready(self, ready: bool) -> None:
        """Enable snapshot capture only while a kernel is actually up.

        Same contract as ``ModeButtonBar``/``ContAcqButton`` -- ``main_window``
        calls this from ``_on_console_started`` and ``_reset_console_ui``.
        """
        self._console_ready = bool(ready)
        self._snapshot_btn.setEnabled(self._console_ready)
        self._snapshot_btn.setToolTip(
            "Capture live beamline readings into the report."
            if self._console_ready
            else "Launch or attach to a kernel first -- readings come from the running kernel."
        )

    # ----------------------------------------------------------- lifecycle --

    def load(self, beamline: str | None, experiment: str | None) -> None:
        """Point the panel at one experiment's report and start following it."""
        self._beamline = beamline or None
        self._experiment = experiment or None
        self._sources = (-1, -1.0)
        if not (self._beamline and self._experiment):
            self.stop()
            self._subject.setText("No experiment")
            self._view.clear()
            return
        self._subject.setText(f"{self._experiment} · {self._beamline}")
        self._subject.setToolTip(rs.report_path(self._beamline, self._experiment))
        self.refresh()
        self._timer.start()

    def stop(self) -> None:
        """Stop following (the report on disk is untouched)."""
        self._timer.stop()

    def _poll(self) -> None:
        if not (self._beamline and self._experiment):
            return
        sources = rs.source_state(self._beamline, self._experiment)
        if sources != self._sources:
            self.refresh()

    @staticmethod
    def _exclusions() -> list:
        """Plan names the active profile keeps out of the report.

        Read fresh on every collect rather than cached, so a change in
        Configuration -> Reports takes effect on the next poll with nothing to
        rebuild -- excluded runs are still recorded, only suppressed.
        """
        return config.get("report_excluded_plans") or []

    def _collect(self, *, persist: bool = True) -> list[dict]:
        return report_builder.collect(
            self._beamline,
            self._experiment,
            persist=persist,
            exclude=self._exclusions(),
        )

    def _markdown(self, entries: list[dict], *, controls: bool, show_hidden: bool) -> str:
        return report_builder.render_markdown(
            entries,
            experiment=self._experiment,
            beamline=self._beamline,
            title=config.get("report_title") or "",
            show_hidden=show_hidden,
            controls=controls,
        )

    def refresh(self) -> None:
        """Reconcile new runs into ``report.jsonl`` and re-render the view."""
        if not (self._beamline and self._experiment):
            return
        # Read the source state BEFORE collecting: collect() appends the run
        # entries it reconciles, so sampling afterwards would store a size the
        # next poll immediately disagrees with and rebuild on every tick.
        self._sources = rs.source_state(self._beamline, self._experiment)
        self._entries = self._collect()
        markdown = self._markdown(
            self._entries, controls=True, show_hidden=self._show_hidden.isChecked()
        )
        self._sources = rs.source_state(self._beamline, self._experiment)

        bar = self._view.verticalScrollBar()
        at_end = self._follow.isChecked()
        previous = bar.value()
        self._view.setHtml(
            report_render.to_html(markdown, base_dir=self._base_dir())
        )
        bar.setValue(bar.maximum() if at_end else min(previous, bar.maximum()))
        self._sync_arrange()

    def _sync_arrange(self) -> None:
        """Refill the Arrange list, but only when it would actually differ.

        The panel polls every second. Rebuilding a ``QListWidget`` on each tick
        would cancel a drag mid-gesture and drop the selection, so the rows are
        only rebuilt when their content or order has genuinely moved.
        """
        # `isVisibleTo`, not `isVisible`: the question is whether the user has
        # switched the list on, and `isVisible` is also false whenever the dock
        # as a whole is closed or floating behind something.
        if not self._arrange.isVisibleTo(self):
            return
        # Always every entry, hidden included: the list is how a hidden entry
        # is found and brought back, so filtering it there would be a trap.
        signature = ro.signature(self._entries)
        if signature == self._arrange_signature:
            return
        self._arrange_signature = signature
        self._arrange.populate(self._entries)

    def _base_dir(self) -> str:
        """Experiment folder that stored figure paths are relative to."""
        if not (self._beamline and self._experiment):
            return ""
        return ri.base_dir(self._beamline, self._experiment)

    def _on_anchor(self, url: QtCore.QUrl) -> None:
        """A click in the rendered report.

        Exactly two things are actionable: one of the report's own control
        links (``bpilot:hide/<id>``), and a figure, which opens full size in the
        desktop's image viewer. Anything else -- a link in a note someone
        typed -- is deliberately inert; the panel sets
        ``setOpenLinks(False)``, so nothing navigates on its own.
        """
        if url.scheme() == report_builder.CONTROL_SCHEME:
            verb, _, identity = url.path().partition("/")
            self._apply_control(verb, identity)
            return
        if url.isLocalFile() and self._is_own_figure(url.toLocalFile()):
            QtGui.QDesktopServices.openUrl(url)

    def _is_own_figure(self, path: str) -> bool:
        """Whether `path` is a figure this experiment actually stores.

        Belt and braces behind ``report_render._link_html``, which already
        refuses to make anything but a control link clickable. Handing a path
        to ``QDesktopServices`` opens it with whatever the desktop has
        registered, so the one place that does it should confirm the file came
        from us rather than from text that happened to reach the document.
        """
        if not (self._beamline and self._experiment):
            return False
        figures = os.path.realpath(ri.figures_dir(self._beamline, self._experiment))
        return os.path.realpath(path).startswith(figures + os.sep)

    # ---------------------------------------------------------- arranging --

    def _write_edits(self, edits: list[dict]) -> None:
        """Persist overlay entries and re-render. No-op for an empty list."""
        if not edits or not (self._beamline and self._experiment):
            return
        if not rs.append_edits(self._beamline, self._experiment, edits):
            QtWidgets.QMessageBox.warning(
                self,
                "Report not updated",
                "The change could not be written to the report file:\n"
                + rs.report_path(self._beamline, self._experiment),
            )
        self.refresh()

    def _apply_control(self, verb: str, identity: str) -> None:
        """One of the inline ``[hide] [↑] [↓]`` links."""
        if not identity:
            return
        if verb in ("hide", "show"):
            self._write_edits([{"target": identity, "hidden": verb == "hide"}])
            return
        if verb in ("up", "down"):
            self._write_edits(
                report_builder.plan_step(
                    self._entries, identity, -1 if verb == "up" else 1
                )
            )

    def _on_arrange_toggled(self, shown: bool) -> None:
        self._arrange.setVisible(shown)
        if shown:
            # Force a fill: the signature is unchanged from when the list was
            # last populated, but the rows were thrown away by `clear`.
            self._arrange_signature = ()
            self._sync_arrange()

    def _on_arrange_moved(self, moved_id: str, after_id: str) -> None:
        """A drag landed. Compute the position override and record it."""
        self._write_edits(report_builder.plan_move(self._entries, moved_id, after_id))

    def _on_arrange_visibility(self, identity: str, visible: bool) -> None:
        self._write_edits([{"target": identity, "hidden": not visible}])

    # -------------------------------------------------------------- actions --

    def add_note(self, text: str, command: str = "") -> None:
        """Record a note. `command` ties it to the run it was typed for.

        Called by ``main_window`` with the plan form's Run notes, which until
        now were baked into the command's ``md={'notes': ...}`` and then thrown
        away on the GUI side.
        """
        if not (text and self._beamline and self._experiment):
            return
        rs.append_event(self._beamline, self._experiment, rs.NOTE, text=text, title=command)
        self.refresh()

    def add_agent_block(self, text: str, title: str = "") -> None:
        """Record a block a person accepted from AutoPILOT (always labelled)."""
        if not (text and self._beamline and self._experiment):
            return
        rs.append_event(self._beamline, self._experiment, rs.AGENT, text=text, title=title)
        self.refresh()

    def _require_experiment(self) -> bool:
        if self._beamline and self._experiment:
            return True
        QtWidgets.QMessageBox.information(
            self,
            "No experiment",
            "Launch or attach to a kernel first -- the report is filed under "
            "the experiment name that session is running as.",
        )
        return False

    def _append_placed(self, kind: str, after_id: str, **fields) -> dict | None:
        """Append a new entry, filed after `after_id` (``""`` = at the end).

        The placement is a ``pos`` override and nothing else: the entry's ``ts``
        still records when it was actually captured, which is the property that
        makes this safe to offer at all. Filing at the end needs no override,
        so the common case leaves the record exactly as it was.
        """
        position, prerequisites = report_builder.plan_insert(self._entries, after_id)
        if prerequisites:
            rs.append_edits(self._beamline, self._experiment, prerequisites)
        if position is not None:
            fields["pos"] = position
        event = rs.append_event(self._beamline, self._experiment, kind, **fields)
        self.refresh()
        return event

    def _ask_entry(self, title: str, prompt: str, *, multiline: bool = True):
        """Run an :class:`report_organize.EntryDialog`; ``None`` if cancelled.

        The placement always starts at "At the end (now)". Filing at the end is
        overwhelmingly the common case, and it is the one choice that is never
        wrong -- an entry can be dragged afterwards, whereas a dialog that
        quietly pre-aimed somewhere else (at whatever happened to be selected
        in the Arrange list, say) files things in the wrong place for anyone
        who does not read the combo before pressing OK.
        """
        dlg = ro.EntryDialog(
            title, prompt, self._entries, multiline=multiline, parent=self
        )
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return dlg

    def _on_note(self) -> None:
        if not self._require_experiment():
            return
        dlg = self._ask_entry("Add note", "Note:")
        if dlg is None or not dlg.text():
            return
        self._append_placed(rs.NOTE, dlg.after_id(), text=dlg.text())

    def _on_heading(self) -> None:
        if not self._require_experiment():
            return
        dlg = self._ask_entry("New section", "Section title:", multiline=False)
        if dlg is None or not dlg.text():
            return
        self._append_placed(rs.HEADING, dlg.after_id(), title=dlg.text())

    def _on_snapshot(self) -> None:
        if not self._require_experiment():
            return
        if self._console is None or not self._console_ready:
            QtWidgets.QMessageBox.information(
                self,
                "No kernel",
                "Snapshots read their values from the running kernel. "
                "Launch or attach to a kernel first.",
            )
            return
        dlg = SnapshotDialog(self._console, self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        rows = dlg.captured_rows()
        if not rows:
            return
        # Filed at the end, deliberately: a snapshot is a reading of the
        # beamline *right now*, so unlike a note or a figure there is no
        # earlier moment it could belong to. It can still be dragged later.
        self._append_placed(
            rs.SNAPSHOT, "", title=dlg.captured_title(), rows=rows
        )

    def _on_paste_image(self) -> None:
        if not self._require_experiment():
            return
        image = ri.from_clipboard()
        if image is None:
            QtWidgets.QMessageBox.information(
                self,
                "Nothing to paste",
                "There is no image on the clipboard.\n\n"
                "Take a screenshot with your usual shortcut first — on macOS "
                "⌘⇧⌃4 copies a region, and on Linux `gnome-screenshot -a -c` "
                "or your desktop's area-capture shortcut does the same.",
            )
            return
        self._add_image(image)

    def _on_attach_image(self) -> None:
        if not self._require_experiment():
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Attach image", os.path.expanduser("~"), ri.IMAGE_FILTER
        )
        if not path:
            return
        image = ri.load_file(path)
        if image is None:
            QtWidgets.QMessageBox.warning(
                self, "Not an image", f"Could not read an image from:\n{path}"
            )
            return
        self._add_image(image)

    def _add_image(self, image) -> None:
        """Caption, place, store, record.

        Asking first means a cancel leaves no orphan file behind -- and the
        same dialog is where the figure's position is chosen, which is the
        common case for a screenshot: it is taken minutes after the scan it
        illustrates and belongs next to it, not at the bottom.
        """
        dlg = self._ask_entry("Add figure", "Caption (optional):", multiline=False)
        if dlg is None:
            return
        caption = dlg.text()
        stored = ri.store_image(self._beamline, self._experiment, image)
        if stored is None:
            QtWidgets.QMessageBox.warning(
                self,
                "Figure not saved",
                "The image could not be written into the experiment folder:\n"
                + ri.figures_dir(self._beamline, self._experiment),
            )
            return
        self._append_placed(rs.IMAGE, dlg.after_id(), title=caption, **stored)

    def _on_export(self) -> None:
        """Save a standalone copy -- PDF, Markdown, or self-contained HTML.

        "Standalone" has to hold for figures too, and each format gets there
        differently: a PDF has the pixels embedded in it by Qt's own print
        pipeline, HTML inlines them as ``data:`` URIs, and Markdown -- which
        cannot inline anything -- gets them copied into a ``<name>_figures/``
        folder beside the ``.md`` with the links rewritten. Without that last
        step the export would point back into the live session directory and
        break the moment it was mailed to anyone.

        Hidden entries and the panel's inline controls are left out of every
        format. An export is a document, not a copy of the editor.
        """
        if not self._require_experiment():
            return
        formats = ["Markdown (*.md)", "HTML (*.html)"]
        if report_render.PDF_AVAILABLE:
            formats.insert(0, "PDF (*.pdf)")
        suffix = ".pdf" if report_render.PDF_AVAILABLE else ".md"
        default = os.path.join(
            os.path.expanduser("~"),
            f"report_{self._experiment}_{time.strftime('%Y%m%d')}{suffix}".replace(" ", "_"),
        )
        path, selected = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export report", default, ";;".join(formats)
        )
        if not path:
            return

        kind = self._export_kind(path, selected)
        wanted = f".{kind}"
        if not path.lower().endswith(wanted):
            path += wanted

        markdown = self._markdown(self._collect(), controls=False, show_hidden=False)
        base = self._base_dir()
        try:
            if kind == "pdf":
                if not report_render.export_pdf(
                    markdown, base_dir=base, path=path, title=self._experiment
                ):
                    QtWidgets.QMessageBox.warning(
                        self, "Export failed", f"The PDF could not be written to:\n{path}"
                    )
                    return
            elif kind == "html":
                body = report_render.to_html(markdown, base_dir=base, embed_images=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(
                        "<!doctype html><meta charset='utf-8'>"
                        f"<title>{self._experiment}</title>"
                        f"<body style='background:{S.PANEL}; margin:24px; "
                        f"font-family:sans-serif;'>{body}</body>"
                    )
            else:
                # Copy the figures first: if that fails the links stay pointing
                # at the originals, which is recoverable, whereas writing the
                # .md first and failing here would leave a half-made export.
                markdown = ri.package_markdown(markdown, base, path)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(markdown)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "Export failed", str(exc))
            return
        QtWidgets.QMessageBox.information(self, "Report exported", f"Saved to:\n{path}")

    @staticmethod
    def _export_kind(path: str, selected: str) -> str:
        """``"pdf"`` / ``"html"`` / ``"md"``.

        An explicit extension the user typed wins over the filter dropdown --
        on some platforms the dialog reports a filter the user never touched,
        and the filename is the less ambiguous statement of intent.
        """
        lowered = path.lower()
        for kind in ("pdf", "html", "md"):
            if lowered.endswith(f".{kind}"):
                return kind
        if selected.startswith("PDF"):
            return "pdf"
        if selected.startswith("HTML"):
            return "html"
        return "md"

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        """Hiding the dock stops the poll; the report on disk is unaffected."""
        self.stop()
        super().closeEvent(event)
