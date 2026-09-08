"""B-PILOT chat dock: a natural-language front end over `autopilot.pipeline`.

Embedded via a guarded import -- see `B_PILOT/autopilot_bridge.py` in B-PILOT.
The LLM call runs on a persistent background thread, mirroring
`B_PILOT/viewer.py`'s `_CatalogWorker` (the one background-worker pattern in
this codebase) -- it must never block the Qt event loop.

Transcript bubbles and the input composer (`_ComposerBox`) reuse B-PILOT's
own palette (`B_PILOT/style.py`) via the same `ensure_bpilot_on_path()`
convention already used by `device_catalog.py`/`plan_context.py`, so the dock
looks like part of the same application rather than a bolted-on widget.
Font size, bubble/panel colors, model, temperature, and the debug raw-output
toggle are all user-configurable via the gear button -> `settings_dialog`,
persisted through `autopilot.settings`.
"""
from __future__ import annotations

import json
import queue
import threading
from html import escape

from PyQt5 import QtCore, QtGui, QtWidgets

from .. import interaction_history
from .. import pipeline
from .. import plan_context
from .. import settings
from .._bpilot_path import ensure_bpilot_on_path
from ..llm_client import ArgoClient
from . import themes
from .settings_dialog import AutoPilotSettingsDialog

ensure_bpilot_on_path()

from B_PILOT import config as bpilot_config  # noqa: E402
from B_PILOT import report_builder as bpilot_report_builder  # noqa: E402
from B_PILOT import report_store as bpilot_report_store  # noqa: E402
from B_PILOT import style as bpilot_style  # noqa: E402


class _ChatWorker(QtCore.QObject):
    """Single persistent background thread that runs the plan-builder pipeline."""

    result_ready = QtCore.pyqtSignal(object)  # pipeline.PlanResult

    def __init__(self, initial_settings: dict) -> None:
        super().__init__()
        self.client: ArgoClient
        self.temperature: float | None
        self._history: list[dict] = []
        # Groups every turn (and later "opened in form" outcome) of one
        # chat-dock session for interaction logging -- regenerated in
        # reset_conversation() alongside the dropped history, since "New
        # Chat" starts a fresh conversation for logging purposes too.
        self.conversation_id = interaction_history.new_conversation_id()
        self.reconfigure(
            model=initial_settings["model"],
            base_url=initial_settings["argo_base_url"],
            api_key=initial_settings["argo_api_key"],
            temperature=initial_settings["temperature"],
        )
        self._queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def reconfigure(self, *, model: str, base_url: str, api_key: str, temperature: float | None) -> None:
        """Rebuild the Argo client from fresh settings (blank -> env var / built-in default).

        Only ever called from the GUI thread; `_run` (background thread) just
        reads `self.client`/`self.temperature` per request, so a request that's
        already in flight when settings change simply finishes under the old
        configuration -- no lock needed for that.
        """
        self.client = ArgoClient(base_url=base_url or None, api_key=api_key or None, model=model or None)
        self.temperature = temperature

    def reset_conversation(self) -> None:
        """Drop the running conversation history -- same thread-safety
        reasoning as `reconfigure`: a simple reference reassignment, safe to
        call from the GUI thread even while `_run` is mid-request (that
        request just finishes under the old history)."""
        self._history = []
        self.conversation_id = interaction_history.new_conversation_id()

    def _run(self) -> None:
        while True:
            request = self._queue.get()
            try:
                result, self._history = pipeline.converse(
                    request,
                    history=self._history,
                    client=self.client,
                    temperature=self.temperature,
                    conversation_id=self.conversation_id,
                )
            except Exception as exc:  # noqa: BLE001 -- never let the worker thread die silently
                result = pipeline.PlanResult(
                    ok=False, message=f"Unexpected error: {exc}", model=self.client.model
                )
            self.result_ready.emit(result)

    def submit(self, request: str) -> None:
        self._queue.put(request)


class _ComposerBox(QtWidgets.QTextEdit):
    """Multiline input that grows with its content, up to a capped height.

    Enter sends the message (common chat-UI convention); Shift+Enter inserts
    a newline instead. Built on `QTextEdit` rather than `QPlainTextEdit` --
    with `setAcceptRichText(False)` it behaves like a plain-text box, but its
    `QTextDocument` reports a reliable `document().size()` once bound to the
    viewport width via `setTextWidth()`, which `QPlainTextEdit`'s lazier
    block layout does not.
    """

    send_requested = QtCore.pyqtSignal()

    _MIN_LINES = 1
    _MAX_LINES = 6

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setPlaceholderText("Describe a scan... (Enter to send, Shift+Enter for a new line)")
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.textChanged.connect(self._resize_to_fit)
        self._resize_to_fit()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter) and not (
            event.modifiers() & QtCore.Qt.ShiftModifier
        ):
            self.send_requested.emit()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._resize_to_fit()

    def set_font_size(self, px: int) -> None:
        font = self.font()
        font.setPixelSize(px)
        self.setFont(font)
        self._resize_to_fit()

    def _resize_to_fit(self) -> None:
        self.document().setTextWidth(self.viewport().width())
        line_height = self.fontMetrics().lineSpacing()
        padding = 2 * self.frameWidth() + self.contentsMargins().top() + self.contentsMargins().bottom()
        padding += bpilot_style.px(8)  # room for the stylesheet's own field padding
        min_height = line_height * self._MIN_LINES + padding
        max_height = line_height * self._MAX_LINES + padding
        content_height = int(self.document().size().height()) + padding
        new_height = max(min_height, min(content_height, max_height))
        self.setFixedHeight(new_height)
        at_max = content_height > max_height
        self.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarAsNeeded if at_max else QtCore.Qt.ScrollBarAlwaysOff
        )


# Qt's rich-text engine has no flexbox and ignores CSS `max-width` on
# <table>/<div> (verified empirically), so a percentage-only bubble width
# grows with the dock and stops wrapping once the dock is wide enough to fit
# a whole message on one line. Capping at a literal pixel width keeps bubbles
# at a comfortable reading width regardless of dock/window size.
_BUBBLE_MAX_WIDTH_PX = 640


def _html_style_font_family(font_family: str) -> str:
    """`bpilot_style.MONO_CSS`/theme font stacks quote multi-word font names
    with `"` (correct CSS, and fine when used in a QSS stylesheet), but here
    they get spliced into an HTML `style="..."` attribute -- the embedded `"`
    closes that attribute early and corrupts the rest of the tag. Verified
    this silently drops `white-space:pre-wrap` on the debug `<pre>` block,
    so it falls back to `<pre>`'s true default (no wrap at all) -- the actual
    cause of "raw: {...}" rendering as one unbroken line. Single quotes are
    equally valid CSS for a quoted font name and don't collide with the
    surrounding double-quoted HTML attribute.
    """
    return font_family.replace('"', "'")


def _bubble_html(
    header: str,
    body_html: str,
    *,
    align: str,
    bg: str,
    border: str,
    fg: str,
    muted: str,
    font_px: int,
    container_width: int,
    font_family: str = "inherit",
) -> str:
    """One chat bubble as a fixed-width table (Qt rich text has no
    flexbox/border-radius, so alignment + background are faked this way).

    `border`/`muted` come from the active theme (not always B-PILOT's own
    palette) so a bubble's frame and caption stay legible against whichever
    theme's background/text colors are in use. `container_width` is the
    transcript viewport's current width in pixels, used to size the bubble to
    78% of it, capped at `_BUBBLE_MAX_WIDTH_PX` so wide/floating docks don't
    turn messages into one long unwrapped line.
    """
    header_px = max(8, font_px - 2)
    font_rule = "" if font_family == "inherit" else f"font-family:{_html_style_font_family(font_family)};"
    bubble_px = max(200, min(int(container_width * 0.78), _BUBBLE_MAX_WIDTH_PX))
    return (
        f'<table align="{align}" width="{bubble_px}" cellpadding="8" cellspacing="0" style="margin:4px 0;">'
        f"<tr><td style=\"background-color:{bg}; border:1px solid {border};\">"
        f'<div style="color:{muted}; font-size:{header_px}px; font-weight:bold; {font_rule}">{header}</div>'
        f'<div style="color:{fg}; font-size:{font_px}px; {font_rule}">{body_html}</div>'
        f"</td></tr></table>"
    )


class ChatDockWidget(QtWidgets.QDockWidget):
    """A minimal chat window: type a scan request, get a draft plan file (or,
    for a GUI-drivable template, fill B-PILOT's own plan-runner form)."""

    def __init__(self, plan_runner, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__("AutoPILOT", parent)
        self.setObjectName("AutoPILOTChatDock")
        self.setTitleBarWidget(self._build_title_bar())

        self._plan_runner = plan_runner
        # The latest `ok` result that can be pushed into the form -- cleared
        # on a new send/New Chat so "Open in form" can never act on a stale
        # proposal from an earlier turn.
        self._pending: pipeline.PlanResult | None = None
        # The last reply's text, for "Add to report". Separate from _pending:
        # that one only holds a *plan proposal*, whereas anything the agent
        # says -- a summary, an explanation -- can be worth keeping in the
        # lab record.
        self._last_reply: str = ""
        # A structured block from propose_report_block, when the last turn
        # produced one: carries its own title, placement and (optionally) the
        # entry it rewrites. `None` means "Add to report" falls back to
        # inserting the raw reply, which is the original behaviour.
        self._report_block: dict | None = None
        # The request that produced it, used as the report block's subtitle so
        # a reader can see what the agent was answering.
        self._last_user_request: str = ""
        # "Thinking" placeholder state -- see _start_thinking()/_stop_thinking().
        self._thinking_timer: QtCore.QTimer | None = None
        self._thinking_pos: int | None = None
        self._thinking_dots = 0

        self._settings = settings.load()
        self._worker = _ChatWorker(self._settings)
        self._worker.result_ready.connect(self._on_result)

        body = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(body)

        # Single row: catalog status on the left, action buttons on the
        # right. The model name used to live here too, but now lives on the
        # title bar next to the "AutoPILOT" heading (see `_build_title_bar`).
        header_row = QtWidgets.QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        # Text/color set in `_apply_settings()`: "Catalog: <name>" normally,
        # or "Catalog override: <name>" (loud/bold) when a local testing
        # override is active (see settings_dialog.py's "Testing (local
        # only)" card) -- must stay visually loud whenever active so a
        # tester can't forget it's on.
        self._catalog_label = QtWidgets.QLabel()
        header_row.addWidget(self._catalog_label)
        header_row.addStretch(1)
        self._open_form_btn = QtWidgets.QPushButton("Open in form")
        self._open_form_btn.setObjectName("OpenInFormButton")
        self._open_form_btn.setToolTip(
            "Load the most recent proposed plan into B-PILOT's plan-runner form for review."
        )
        self._open_form_btn.setEnabled(False)
        self._open_form_btn.clicked.connect(self._on_open_in_form)
        header_row.addWidget(self._open_form_btn)
        # AutoPILOT has no tool that writes to the experiment report -- this
        # button is the only path in, so a person has always read the text
        # before it enters the lab record.
        self._add_report_btn = QtWidgets.QPushButton("Add to report")
        self._add_report_btn.setObjectName("AddToReportButton")
        self._add_report_btn.setToolTip(
            "Insert the latest reply into the experiment report, labelled as "
            "AutoPILOT-written."
        )
        self._add_report_btn.setEnabled(False)
        self._add_report_btn.clicked.connect(self._on_add_to_report)
        header_row.addWidget(self._add_report_btn)
        new_chat_btn = QtWidgets.QPushButton("New Chat")
        new_chat_btn.setToolTip("Clear the transcript and start a fresh conversation (no memory of prior turns).")
        new_chat_btn.clicked.connect(self._new_chat)
        header_row.addWidget(new_chat_btn)
        settings_btn = QtWidgets.QPushButton("⚙")
        settings_btn.setFixedWidth(bpilot_style.px(28))
        settings_btn.setToolTip("AutoPILOT settings")
        settings_btn.clicked.connect(self._open_settings)
        header_row.addWidget(settings_btn)
        layout.addLayout(header_row)

        self._transcript = QtWidgets.QTextEdit()
        self._transcript.setReadOnly(True)
        self._transcript.setPlaceholderText(
            "Ask AutoPILOT to draft a scan, or ask about past runs. Examples:\n\n"
            "• \"step scan samE from 0 to 10 mm in 21 steps, 1s exposure on pimega\"\n"
            "• \"grid scan samX 0 to 5 mm in 11 steps, samY 0 to 2 mm in 5 steps, 0.5s on eiger\"\n"
            "• \"how many expose runs aborted this week?\"\n"
            "• \"what was scan 4874?\""
        )
        layout.addWidget(self._transcript, 1)

        row = QtWidgets.QHBoxLayout()
        self._input = _ComposerBox()
        self._input.send_requested.connect(self._on_send)
        row.addWidget(self._input, 1)
        self._send_btn = QtWidgets.QPushButton("Send")
        self._send_btn.clicked.connect(self._on_send)
        row.addWidget(self._send_btn, 0, QtCore.Qt.AlignBottom)
        layout.addLayout(row)

        self.setWidget(body)
        self._apply_settings()

    def _build_title_bar(self) -> QtWidgets.QWidget:
        """Replace the native title bar with a slim custom one: "AutoPILOT"
        plus a small float/dock toggle icon next to the close (x) button.

        A custom title bar is needed (rather than the default) because the
        native float button's own redock behavior isn't reliable on every
        window manager -- this one calls `setFloating()` directly instead.
        """
        bar = QtWidgets.QWidget()
        bar.setObjectName("AutoPILOTTitleBar")
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(8, 3, 3, 3)
        lay.setSpacing(0)

        self._title_label = QtWidgets.QLabel("AutoPILOT")
        self._title_label.setObjectName("AutoPILOTTitleLabel")
        lay.addWidget(self._title_label)

        lay.addSpacing(6)
        title_sep = QtWidgets.QLabel("|")
        title_sep.setObjectName("AutoPILOTTitleSep")
        lay.addWidget(title_sep)

        lay.addSpacing(6)
        # Text/style set in `_apply_settings()` -- `self._worker` (which owns
        # the active model name) isn't constructed yet at this point in
        # `__init__`. Elided (see `_apply_settings`) so a long model name
        # can't crowd out the float/close buttons to the right.
        self._title_model_label = QtWidgets.QLabel("")
        self._title_model_label.setObjectName("AutoPILOTTitleModelLabel")
        lay.addWidget(self._title_model_label)

        lay.addStretch(1)

        icon_size = QtCore.QSize(bpilot_style.px(12), bpilot_style.px(12))
        btn_size = bpilot_style.px(18)

        self._dock_toggle_btn = QtWidgets.QToolButton()
        self._dock_toggle_btn.setAutoRaise(True)
        self._dock_toggle_btn.setFixedSize(btn_size, btn_size)
        self._dock_toggle_btn.setIconSize(icon_size)
        self._dock_toggle_btn.setIcon(
            self.style().standardIcon(QtWidgets.QStyle.SP_TitleBarNormalButton)
        )
        self._dock_toggle_btn.clicked.connect(self._on_toggle_floating)
        lay.addWidget(self._dock_toggle_btn)

        close_btn = QtWidgets.QToolButton()
        close_btn.setAutoRaise(True)
        close_btn.setFixedSize(btn_size, btn_size)
        close_btn.setIconSize(icon_size)
        close_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_TitleBarCloseButton))
        close_btn.setToolTip("Close (hide) this panel.")
        close_btn.clicked.connect(self.close)
        lay.addWidget(close_btn)

        self.topLevelChanged.connect(self._on_top_level_changed)
        self._update_dock_toggle_button()
        self._title_bar = bar
        return bar

    def _new_chat(self) -> None:
        # Stop (don't "remove-then-clear") -- _transcript.clear() below already
        # wipes the placeholder; a stale self._thinking_pos would otherwise
        # point past the now-empty document on the next tick.
        if self._thinking_timer is not None:
            self._thinking_timer.stop()
            self._thinking_timer = None
        self._thinking_pos = None
        self._worker.reset_conversation()
        self._transcript.clear()
        self._pending = None
        self._open_form_btn.setEnabled(False)
        self._last_reply = ""
        self._last_user_request = ""
        self._report_block = None
        self._sync_report_button()

    def _on_toggle_floating(self) -> None:
        self.setFloating(not self.isFloating())

    def _on_top_level_changed(self, _floating: bool) -> None:
        self._update_dock_toggle_button()

    def _update_dock_toggle_button(self) -> None:
        floating = self.isFloating()
        self._dock_toggle_btn.setToolTip(
            "Dock this panel back into the main window."
            if floating
            else "Undock this panel into a floating window."
        )

    def _open_settings(self) -> None:
        dlg = AutoPilotSettingsDialog(self._settings, self)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self._settings = dlg.values()
            settings.save(self._settings)
            self._apply_settings()

    def _apply_settings(self) -> None:
        s = self._settings
        self._theme = themes.resolve(s.get("theme"))
        self._worker.reconfigure(
            model=s["model"], base_url=s["argo_base_url"], api_key=s["argo_api_key"], temperature=s["temperature"]
        )
        # Elided so a long model name can't crowd the float/close buttons
        # off the title bar; the tooltip carries the untruncated name.
        model_text = f"Model: {self._worker.client.model}"
        metrics = QtGui.QFontMetrics(self._title_model_label.font())
        self._title_model_label.setText(
            metrics.elidedText(model_text, QtCore.Qt.ElideRight, bpilot_style.px(180))
        )
        self._title_model_label.setToolTip(model_text)

        override = s.get("databroker_catalog_override", "")
        if override:
            catalog_text = f"Catalog override: {override}"
            color, weight = self._theme.accent, "bold"
        else:
            catalog_name = bpilot_config.as_dict().get("databroker_catalog") or "(none configured)"
            catalog_text = f"Catalog: {catalog_name}"
            color, weight = self._theme.muted, "normal"
        self._catalog_label.setText(catalog_text)
        self._catalog_label.setToolTip(catalog_text)
        self._catalog_label.setStyleSheet(f"color: {color}; font-size: 10px; font-weight: {weight};")
        # Dock-scoped stylesheet: overrides B-PILOT's app-wide (light) QSS for
        # just this dock and its children -- see `themes.build_dock_stylesheet`.
        self.setStyleSheet(themes.build_dock_stylesheet(self._theme, s["font_size"]))
        self._input.set_font_size(s["font_size"])
        self._apply_theme_fonts()
        self._apply_theme_glow()

    def _apply_theme_fonts(self) -> None:
        """QSS `font-family` doesn't reliably repaint an already-constructed
        QTextDocument, so set the family directly on both text widgets too."""
        theme = self._theme
        if theme.font_family == "inherit":
            return
        first_family = theme.font_family.split(",")[0].strip().strip('"')
        for widget in (self._input, self._transcript):
            font = widget.font()
            font.setFamily(first_family)
            widget.setFont(font)

    def _apply_theme_glow(self) -> None:
        """Approximate a HUD "glowing focus" look for themes that call for
        it -- plain QSS has no box-shadow/glow, so this uses a real Qt
        graphics effect on the composer box instead."""
        if self._theme.glow:
            glow = QtWidgets.QGraphicsDropShadowEffect(self._input)
            glow.setColor(QtGui.QColor(self._theme.accent))
            glow.setBlurRadius(bpilot_style.px(18))
            glow.setOffset(0, 0)
            self._input.setGraphicsEffect(glow)
        else:
            self._input.setGraphicsEffect(None)

    def _append_user(self, text: str) -> None:
        s = self._settings
        body_html = escape(text).replace("\n", "<br>")
        html = _bubble_html(
            "You", body_html, align="right", bg=s["user_bubble_bg"], border=self._theme.border,
            fg=s["user_text_color"], muted=self._theme.muted, font_px=s["font_size"],
            container_width=self._transcript.viewport().width(), font_family=self._theme.font_family,
        )
        self._transcript.append(html)
        self._scroll_to_bottom()

    def _append_response(self, result: "pipeline.PlanResult") -> None:
        s = self._settings
        meta_bits = ["AutoPILOT"]
        if result.model:
            meta_bits.append(f"model: {escape(result.model)}")
        if result.tool_calls:
            trail = " -&gt; ".join(escape(name) for name in result.tool_calls)
            meta_bits.append(f"tools: {trail}")
        elif result.tool_name:
            meta_bits.append(f"tool: {escape(result.tool_name)}")
        if result.input_tokens is not None:
            tokens_bit = f"tokens: {result.input_tokens} in / {result.output_tokens} out"
            if result.cache_read_input_tokens:
                tokens_bit += f" (cached: {result.cache_read_input_tokens})"
            meta_bits.append(tokens_bit)
        header = " &middot; ".join(meta_bits)
        body_html = escape(result.message).replace("\n", "<br>")
        if s["show_raw_output"]:
            body_html += self._debug_block(result)
        # Hardcoded, theme-independent border for a failed turn -- none of
        # themes.py's 4 presets define an error/warning color, and this must
        # stay visually distinct regardless of the active theme so a failed
        # reply never looks identical to a real success (defense-in-depth
        # alongside the system-prompt fix telling the model not to narrate a
        # false success in the first place).
        border = "#cc4444" if not result.ok else self._theme.border
        html = _bubble_html(
            header, body_html, align="left", bg=s["assistant_bubble_bg"], border=border,
            fg=s["assistant_text_color"], muted=self._theme.muted, font_px=s["font_size"],
            container_width=self._transcript.viewport().width(), font_family=self._theme.font_family,
        )
        self._transcript.append(html)
        self._scroll_to_bottom()

    def _debug_block(self, result: "pipeline.PlanResult") -> str:
        lines = []
        if result.raw_spec is not None:
            lines.append("raw: " + json.dumps(result.raw_spec))
        if result.clean_spec is not None and result.clean_spec != result.raw_spec:
            lines.append("validated: " + json.dumps(result.clean_spec))
        if not lines:
            return ""
        text = escape("\n".join(lines))
        debug_px = max(8, self._settings["font_size"] - 2)
        theme = self._theme
        font_family = bpilot_style.MONO_CSS if theme.font_family == "inherit" else theme.font_family
        font_family = _html_style_font_family(font_family)
        return (
            f'<pre style="background-color:{theme.panel}; border:1px solid {theme.border}; '
            f'font-family:{font_family}; font-size:{debug_px}px; padding:4px; '
            f'margin-top:4px; white-space:pre-wrap;">{text}</pre>'
        )

    def _append_note(self, text: str) -> None:
        """A short muted status line -- for 'Open in form' outcomes, not a
        full chat bubble (there's no model turn attached to these)."""
        html = f'<div style="color:{self._theme.muted}; font-size:{self._settings["font_size"] - 1}px;">{escape(text)}</div>'
        self._transcript.append(html)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        scrollbar = self._transcript.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _start_thinking(self) -> None:
        """Append an animated placeholder bubble while a request is in flight.

        `self._thinking_pos` marks the document offset right before the
        placeholder is appended -- `QTextEdit.append()` always appends at the
        true document end regardless of `textCursor()`, so re-rendering (or
        finally removing) the placeholder is just "delete from that saved
        offset to the current end, then append again."
        """
        self._thinking_dots = 0
        cursor = self._transcript.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        self._thinking_pos = cursor.position()
        self._render_thinking()
        self._thinking_timer = QtCore.QTimer(self)
        self._thinking_timer.timeout.connect(self._tick_thinking)
        self._thinking_timer.start(450)

    def _tick_thinking(self) -> None:
        self._thinking_dots = (self._thinking_dots + 1) % 3
        self._render_thinking()

    def _render_thinking(self) -> None:
        self._clear_thinking_text()
        dots = "." * (1 + self._thinking_dots)
        html = (
            f'<div style="color:{self._theme.muted}; '
            f'font-size:{self._settings["font_size"] - 1}px;">AutoPILOT is thinking{dots}</div>'
        )
        self._transcript.append(html)
        self._scroll_to_bottom()

    def _clear_thinking_text(self) -> None:
        if self._thinking_pos is None:
            return
        cursor = self._transcript.textCursor()
        cursor.setPosition(self._thinking_pos)
        cursor.movePosition(QtGui.QTextCursor.End, QtGui.QTextCursor.KeepAnchor)
        cursor.removeSelectedText()

    def _stop_thinking(self) -> None:
        if self._thinking_timer is not None:
            self._thinking_timer.stop()
            self._thinking_timer = None
        self._clear_thinking_text()
        self._thinking_pos = None

    def _on_send(self) -> None:
        text = self._input.toPlainText().strip()
        if not text:
            return
        self._append_user(text)
        self._input.clear()
        self._input.setEnabled(False)
        self._send_btn.setEnabled(False)
        # A new turn in flight supersedes whatever the last turn proposed.
        self._pending = None
        self._open_form_btn.setEnabled(False)
        self._last_reply = ""
        self._report_block = None
        self._last_user_request = text
        self._sync_report_button()
        self._start_thinking()
        self._worker.submit(text)

    def _on_result(self, result: "pipeline.PlanResult") -> None:
        self._stop_thinking()
        self._input.setEnabled(True)
        self._send_btn.setEnabled(True)
        self._append_response(result)
        self._input.setFocus()
        self._pending = result if (result.ok and result.gui_command) else None
        self._open_form_btn.setEnabled(self._pending is not None)
        self._last_reply = (result.message or "").strip()
        self._report_block = result.report_block
        self._sync_report_button()

    def _sync_report_button(self) -> None:
        """Label the button for whatever is actually pending.

        A drafted block and "keep this reply" are different enough acts that
        the button should say which one it is about to do -- otherwise a user
        who asked for a rewrite has no way to tell whether the model produced
        one or just talked about it, which is exactly the false-narration
        failure the system prompt is guarding against.
        """
        block = self._report_block
        self._add_report_btn.setEnabled(bool(block or self._last_reply))
        if not block:
            self._add_report_btn.setText("Add to report")
            self._add_report_btn.setToolTip(
                "Insert the latest reply into the experiment report, labelled "
                "as AutoPILOT-written."
            )
            return
        title = block.get("title") or "drafted block"
        self._add_report_btn.setText("Add draft ✦")
        self._add_report_btn.setToolTip(
            f"Add “{title}” to the experiment report, labelled as "
            "AutoPILOT-written."
            + (
                "\nThe entry it rewrites will be hidden, not deleted."
                if block.get("replaces")
                else ""
            )
        )

    def _on_add_to_report(self) -> None:
        """Insert into the experiment report, human-gated. The only write path.

        Mirrors :meth:`_on_open_in_form`: act on what is pending, then log the
        outcome so the interaction history records that a person accepted it.
        The experiment is resolved from the history store rather than from a
        live console handle -- ``autopilot_bridge`` injects only the plan-runner
        panel, and this is not a good enough reason to widen that contract.

        Two things can be pending. A **proposal** from ``propose_report_block``
        carries its own title, a placement, and possibly an entry it rewrites;
        otherwise the raw last reply is added, which is the original behaviour
        and still the right answer for "that explanation was useful, keep it".
        """
        block = self._report_block
        text = (block or {}).get("markdown") or self._last_reply
        if not text:
            return
        beamline = bpilot_config.as_dict().get("beamline") or ""
        experiment = bpilot_report_store.current_experiment(beamline)
        if not experiment:
            self._append_note(
                "No experiment is running yet, so there is no report to add to. "
                "Launch or attach to a kernel first."
            )
            return

        title = (block or {}).get("title") or (
            f"In reply to: {self._last_user_request}" if self._last_user_request else ""
        )
        fields = {}
        entries = []
        if block:
            # Resolve the placement against the report as it stands right now,
            # not as it was when the model read it -- runs may have landed since.
            entries = bpilot_report_builder.collect(
                beamline,
                experiment,
                persist=False,
                exclude=bpilot_config.as_dict().get("report_excluded_plans") or [],
            )
            position, prerequisites = bpilot_report_builder.plan_insert(
                entries, block.get("place_after") or ""
            )
            if prerequisites:
                bpilot_report_store.append_edits(beamline, experiment, prerequisites)
            if position is not None:
                fields["pos"] = position

        if bpilot_report_store.append_event(
            beamline, experiment, bpilot_report_store.AGENT, text=text, title=title, **fields
        ) is None:
            self._append_note("Could not write to the report file.")
            return

        superseded = (block or {}).get("replaces")
        if superseded:
            # Hidden, never deleted: a rewrite the user later disagrees with
            # must be recoverable, and the original is still in report.jsonl
            # behind the panel's "Show hidden" toggle.
            known = {bpilot_report_builder.entry_id(e) for e in entries}
            if superseded in known:
                bpilot_report_store.append_edits(
                    beamline, experiment, [{"target": superseded, "hidden": True}]
                )
            else:
                self._append_note(
                    "Added, but the entry it was meant to replace no longer "
                    "exists in the report, so nothing was hidden."
                )
        interaction_history.record_outcome(
            beamline,
            conversation_id=self._worker.conversation_id,
            turn_id=self._pending.turn_id if self._pending else "",
            action="added_to_report",
        )
        # Cleared, not just greyed out: the guard at the top of this method is
        # then what makes a second insert impossible, rather than the button's
        # enabled state alone.
        self._last_reply = ""
        self._report_block = None
        self._sync_report_button()
        self._append_note(
            f"Added to the '{experiment}' report, labelled as AutoPILOT-written."
            + (" The entry it replaces is now hidden." if superseded else "")
        )

    def _on_open_in_form(self) -> None:
        if self._pending is None or not self._pending.template_key:
            return
        template = plan_context.TEMPLATES.get(self._pending.template_key)
        if template is None or not template.gui_plan_name:
            return
        if not self._plan_runner.has_plan(template.gui_plan_name):
            self._append_note(
                f"'{template.gui_plan_name}' isn't in a currently-visible file -- "
                f"check '{template.gui_plan_file}' in the file browser (left panel), "
                "then click Open in form again."
            )
            return
        self._plan_runner.load_from_command(self._pending.gui_command)
        interaction_history.record_outcome(
            bpilot_config.as_dict().get("beamline") or "",
            conversation_id=self._worker.conversation_id,
            turn_id=self._pending.turn_id,
            action="opened_in_form",
        )
        self._append_note(
            f"Opened {template.gui_plan_name} in the form -- review the fields, "
            "then Run or Add to Queue."
        )
