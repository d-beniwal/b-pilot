"""The Experiment Report dock -- a live lab record, side by side with the run.

A ``QDockWidget`` rather than another tab in the console tab bar, for one
reason: the point of this panel is to be readable *while* a plan is running,
which a tab that hides the console cannot do. Docking also gets floating onto
a second monitor for free, which is how a beamline desk is actually laid out.

The structure mirrors ``AutoPILOT/autopilot/gui/chat_panel.py``'s
``ChatDockWidget`` -- the app's only other dock, and the pattern
``main_window.py`` already knows how to host (allowed areas, ribbon tab,
menu toggle, config-backed visibility).

**What updates it.** Plan runs are not pushed here by the GUI; they are derived
from ``history.jsonl`` by :mod:`report_builder`. That indirection is what makes
the panel show plans dispatched by the *detached queue runner*, and plans that
ran while the GUI was closed, with no extra plumbing. The panel just polls the
two source files for a size/mtime change (:func:`report_store.source_sizes`)
and rebuilds when one of them moved.
"""
from __future__ import annotations

import os
import time

from PyQt5 import QtCore
from PyQt5 import QtGui
from PyQt5 import QtWidgets

from . import config
from . import report_builder
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
        self._sources: tuple[int, float] = (-1, -1.0)

        body = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(body)

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._subject = QtWidgets.QLabel("No experiment")
        self._subject.setStyleSheet(f"color:{S.MUTED};")
        header.addWidget(self._subject)
        header.addStretch(1)
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
        self._view.setPlaceholderText(
            "The experiment report builds itself as you run plans.\n\n"
            "Run notes from the plan form land here automatically. Use the "
            "buttons below to add a note, a section heading, or a snapshot of "
            "live beamline readings."
        )
        layout.addWidget(self._view, 1)

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
        buttons.addWidget(self._button("✎ Note", "Add a free note at this point in the report.", self._on_note))
        buttons.addWidget(
            self._button("§ Heading", "Start a new titled section in the report.", self._on_heading)
        )
        buttons.addStretch(1)
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
        self._subject.setToolTip(rs.markdown_path(self._beamline, self._experiment))
        self.refresh()
        self._timer.start()

    def stop(self) -> None:
        """Stop following (the report on disk is untouched)."""
        self._timer.stop()

    def _poll(self) -> None:
        if not (self._beamline and self._experiment):
            return
        sources = rs.source_sizes(self._beamline, self._experiment)
        if sources != self._sources:
            self.refresh()

    def refresh(self) -> None:
        """Rebuild ``report.md`` from its two sources and re-render the view."""
        if not (self._beamline and self._experiment):
            return
        from . import experiment_history as eh

        self._sources = rs.source_sizes(self._beamline, self._experiment)
        markdown = report_builder.render_markdown(
            eh.read_entries(self._beamline, self._experiment),
            rs.read_events(self._beamline, self._experiment),
            experiment=self._experiment,
            beamline=self._beamline,
            title=config.get("report_title") or "",
        )
        rs.write_markdown(self._beamline, self._experiment, markdown)

        bar = self._view.verticalScrollBar()
        at_end = self._follow.isChecked()
        previous = bar.value()
        self._view.setHtml(report_render.to_html(markdown))
        bar.setValue(bar.maximum() if at_end else min(previous, bar.maximum()))

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

    def _on_note(self) -> None:
        if not self._require_experiment():
            return
        text, ok = QtWidgets.QInputDialog.getMultiLineText(self, "Add note", "Note:")
        if ok and text.strip():
            rs.append_event(self._beamline, self._experiment, rs.NOTE, text=text.strip())
            self.refresh()

    def _on_heading(self) -> None:
        if not self._require_experiment():
            return
        text, ok = QtWidgets.QInputDialog.getText(self, "New section", "Section title:")
        if ok and text.strip():
            rs.append_event(self._beamline, self._experiment, rs.HEADING, title=text.strip())
            self.refresh()

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
        rs.append_event(
            self._beamline,
            self._experiment,
            rs.SNAPSHOT,
            title=dlg.captured_title(),
            rows=rows,
        )
        self.refresh()

    def _on_export(self) -> None:
        """Save a standalone copy -- Markdown, or self-contained HTML."""
        if not self._require_experiment():
            return
        default = os.path.join(
            os.path.expanduser("~"),
            f"report_{self._experiment}_{time.strftime('%Y%m%d')}.md".replace(" ", "_"),
        )
        path, selected = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export report", default, "Markdown (*.md);;HTML (*.html)"
        )
        if not path:
            return
        markdown = rs.read_markdown(self._beamline, self._experiment)
        wants_html = selected.startswith("HTML") or path.lower().endswith(".html")
        if wants_html and not path.lower().endswith(".html"):
            path += ".html"
        try:
            with open(path, "w", encoding="utf-8") as fh:
                if wants_html:
                    fh.write(
                        "<!doctype html><meta charset='utf-8'>"
                        f"<title>{self._experiment}</title>"
                        f"<body style='background:{S.PANEL}; margin:24px; "
                        f"font-family:sans-serif;'>{report_render.to_html(markdown)}</body>"
                    )
                else:
                    fh.write(markdown)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "Export failed", str(exc))
            return
        QtWidgets.QMessageBox.information(self, "Report exported", f"Saved to:\n{path}")

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        """Hiding the dock stops the poll; the report on disk is unaffected."""
        self.stop()
        super().closeEvent(event)
