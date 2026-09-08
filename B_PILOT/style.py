"""Switchable themes + layout helpers for the Qt plan-runner GUI.

Three built-in themes (Light, Dark, Slate) share one modern, flat QSS
template (:func:`stylesheet`) and differ only in their color tokens (see
:class:`Theme` / :data:`THEMES`). The checkmark/arrow SVG helpers are
copied in so this GUI does not depend on the midas package.

:func:`apply_theme` resolves the chosen theme and rebinds every existing
module-level color constant (``BG``, ``ACCENT``, ``MUTED``, ...) to that
theme's values, so the many call sites across ``B_PILOT/`` that already read
``style.MUTED`` / ``style.ACCENT`` / etc. pick up the active theme without
any change. It must run once at startup, before any widget is built (see
``config`` key ``"theme"``) — not live-updatable mid-session, same
constraint as :func:`set_scale`.
"""
# ruff: noqa: E501  (the QSS block below has long, readable one-line rules)
from __future__ import annotations

import atexit
import contextlib
import os
import tempfile
from dataclasses import dataclass, field

from PyQt5 import QtCore
from PyQt5 import QtWidgets

# ── Theme registry ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Theme:
    """One named color palette. Every hardcoded color anywhere in B_PILOT/
    (the base QSS below, plus the small "shadow palettes" in
    mode_buttons.py/run_controls.py/queue_panel.py/config_dialog.py) should
    trace back to a field here so switching themes actually changes it."""

    key: str
    label: str
    # Base surfaces / text.
    bg: str          # window background
    panel: str       # raised card background (QGroupBox, toolbar)
    input_bg: str    # text fields, combo boxes, lists
    input_fg: str
    text: str        # primary text
    muted: str       # secondary/hint text
    border: str
    hover: str       # generic hover border/outline
    alt_row_bg: str  # alternating list/table rows
    tooltip_bg: str
    groupbox_title: str
    # Accent + semantic status.
    accent: str
    accent_d: str    # darker accent (primary-button gradient/border)
    error: str
    success: str
    warning: str
    # Command-preview syntax colors.
    cmd_import: str  # the "from ... import ..." line
    cmd_re: str      # the "RE(...)" line
    # Buttons (flat, no gradient).
    button_bg: str
    button_hover_bg: str
    button_pressed_bg: str
    button_disabled_bg: str
    button_disabled_border: str
    disabled_text: str
    primary_hover_border: str
    primary_disabled_bg: str
    primary_disabled_border: str
    # Scrollbars / splitter / lists.
    scrollbar_bg: str
    scrollbar_handle: str
    scrollbar_handle_hover: str
    splitter: str
    list_hover_bg: str
    # Queue status colors (queue_panel.py's per-item status column).
    status_waiting: str
    status_running: str
    status_done: str
    status_error: str
    # Scan-block category colors (config_dialog.py's "Scan blocks" tab).
    scan_block_colors: dict[str, str] = field(default_factory=dict)


THEMES: dict[str, Theme] = {
    "light": Theme(
        key="light",
        label="Light",
        bg="#eef0f2",
        panel="#ffffff",
        input_bg="#ffffff",
        input_fg="#1a1a1a",
        text="#1f2328",
        muted="#66707a",
        border="#d7dbdf",
        hover="#aeb4ba",
        alt_row_bg="#f5f6f7",
        tooltip_bg="#fffbe6",
        groupbox_title="#3a3f45",
        accent="#ff7800",
        accent_d="#c85e00",
        error="#c62828",
        success="#2e7d32",
        warning="#e69500",
        cmd_import="#1565c0",
        cmd_re="#2e7d32",
        button_bg="#f7f8f9",
        button_hover_bg="#eef0f2",
        button_pressed_bg="#e2e5e8",
        button_disabled_bg="#f0f1f2",
        button_disabled_border="#dde0e3",
        disabled_text="#9aa1a8",
        primary_hover_border="#7a3a00",
        primary_disabled_bg="#e4e6e8",
        primary_disabled_border="#d0d3d6",
        scrollbar_bg="#e6e8ea",
        scrollbar_handle="#c3c8cd",
        scrollbar_handle_hover="#a8aeb4",
        splitter="#d7dbdf",
        list_hover_bg="#fdeee0",
        status_waiting="#e69500",
        status_running="#2e7d32",
        status_done="#c62828",
        status_error="#7b1fa2",
        scan_block_colors={
            "plan_opener": "#2e7d32",
            "per_step": "#1565c0",
            "plan_closer": "#c85e00",
            "suspender": "#6a1b9a",
            "pseudo_suspender": "#8d6e00",
        },
    ),
    "dark": Theme(
        key="dark",
        label="Dark",
        bg="#1b1e23",
        panel="#22262c",
        input_bg="#2a2f37",
        input_fg="#e6e9ec",
        text="#e6e9ec",
        muted="#9099a3",
        border="#3a3f47",
        hover="#4a5058",
        alt_row_bg="#20242a",
        tooltip_bg="#3a3520",
        groupbox_title="#ffb877",
        accent="#ff8c3a",
        accent_d="#d97a2e",
        error="#ff5252",
        success="#4caf50",
        warning="#ffb74d",
        cmd_import="#64b5f6",
        cmd_re="#81c784",
        button_bg="#2a2f37",
        button_hover_bg="#333944",
        button_pressed_bg="#3d4550",
        button_disabled_bg="#23262b",
        button_disabled_border="#33373d",
        disabled_text="#5c626a",
        primary_hover_border="#ffa85c",
        primary_disabled_bg="#33373d",
        primary_disabled_border="#3a3f47",
        scrollbar_bg="#20242a",
        scrollbar_handle="#3e444c",
        scrollbar_handle_hover="#4e555f",
        splitter="#3a3f47",
        list_hover_bg="#332a1e",
        status_waiting="#ffb74d",
        status_running="#66bb6a",
        status_done="#ef5350",
        status_error="#ba68c8",
        scan_block_colors={
            "plan_opener": "#66bb6a",
            "per_step": "#64b5f6",
            "plan_closer": "#ffa85c",
            "suspender": "#ba68c8",
            "pseudo_suspender": "#d4b106",
        },
    ),
    "slate": Theme(
        key="slate",
        label="Slate",
        bg="#232a33",
        panel="#2b3440",
        input_bg="#1f252d",
        input_fg="#dde3ea",
        text="#dde3ea",
        muted="#8793a1",
        border="#3c4654",
        hover="#4c5866",
        alt_row_bg="#28303a",
        tooltip_bg="#33404f",
        groupbox_title="#8fc4ff",
        accent="#4fa8ff",
        accent_d="#2f7fcf",
        error="#ff6b6b",
        success="#4fd18b",
        warning="#ffcc66",
        cmd_import="#7ec8ff",
        cmd_re="#7de3a8",
        button_bg="#2b3440",
        button_hover_bg="#344052",
        button_pressed_bg="#3c4a5e",
        button_disabled_bg="#252d36",
        button_disabled_border="#37404c",
        disabled_text="#5e6a78",
        primary_hover_border="#7ec8ff",
        primary_disabled_bg="#2f3946",
        primary_disabled_border="#3c4654",
        scrollbar_bg="#28303a",
        scrollbar_handle="#3f4a58",
        scrollbar_handle_hover="#51606f",
        splitter="#3c4654",
        list_hover_bg="#2e3d4d",
        status_waiting="#ffcc66",
        status_running="#4fd18b",
        status_done="#ff6b6b",
        status_error="#c792ea",
        scan_block_colors={
            "plan_opener": "#4fd18b",
            "per_step": "#4fa8ff",
            "plan_closer": "#ffcc66",
            "suspender": "#c792ea",
            "pseudo_suspender": "#e0c341",
        },
    ),
}

THEME_CHOICES: list[tuple[str, str]] = [(t.key, t.label) for t in THEMES.values()]

DEFAULT_THEME_KEY = "light"


def resolve(theme_key: str | None) -> Theme:
    """Look up a theme by key, falling back to the default for an unknown/missing key."""
    return THEMES.get(theme_key or "", THEMES[DEFAULT_THEME_KEY])


# Fixed, theme-independent color for the toolbar profile switcher. Deliberately
# NOT the shared ACCENT token: ACCENT is orange in the Light and Dark themes
# (it's this app's primary-button color), which made the switcher's fill and
# its dropdown's selection highlight both read as orange -- confusing next to
# unrelated warning/attention coloring elsewhere. This one control keeps a
# fixed blue across all three themes instead.
PROFILE_SWITCHER_BG = "#2f6fed"
PROFILE_SWITCHER_BG_HOVER = "#2358c4"


# Fixed-width font stack. Naming real per-platform families (rather than the
# generic "monospace") lets Qt resolve immediately. Theme-independent.
MONO_FAMILIES = ["Menlo", "Consolas", "DejaVu Sans Mono", "Courier New"]
MONO_CSS = ", ".join(f'"{f}"' if " " in f else f for f in MONO_FAMILIES)

# ── UI font family (independent of color theme — any theme x any font) ──────
# Each value is a QSS font-family fallback chain: Qt tries each name in order
# and falls back to the generic keyword at the end if none are installed, so
# an aspirational family (e.g. "Inter") is harmless on a machine without it.
FONT_STACKS: dict[str, str] = {
    "system": "",  # don't set font-family at all — inherit the OS/Qt default
    "sans": '"Helvetica Neue", "Segoe UI", "Inter", "DejaVu Sans", Arial, sans-serif',
    "serif": 'Georgia, "Times New Roman", "DejaVu Serif", serif',
    "rounded": '"Avenir Next", "Century Gothic", Verdana, "DejaVu Sans", sans-serif',
    "mono_ui": MONO_CSS,  # whole UI in the fixed-width font, not just code boxes
}
FONT_LABELS: dict[str, str] = {
    "system": "System Default",
    "sans": "Modern Sans",
    "serif": "Classic Serif",
    "rounded": "Rounded Sans",
    "mono_ui": "Monospace (whole UI)",
}
FONT_CHOICES: list[tuple[str, str]] = [(k, FONT_LABELS[k]) for k in FONT_STACKS]

DEFAULT_FONT_KEY = "system"


def resolve_font(font_key: str | None) -> str:
    """Look up a font stack's CSS by key, falling back to the default for an
    unknown/missing key. Returns the font-family CSS value (may be "")."""
    return FONT_STACKS.get(font_key or "", FONT_STACKS[DEFAULT_FONT_KEY])


FONT_FAMILY_CSS = resolve_font(DEFAULT_FONT_KEY)


def _rebind_globals(theme: Theme) -> None:
    """Rebind every module-level color constant to `theme`'s values.

    Lets every existing ``style.ATTR``/``S.ATTR`` call site across B_PILOT/
    keep working unchanged when the active theme changes — they read the
    module attribute at call time, and widget construction always happens
    after :func:`apply_theme` has run (see the module docstring).
    """
    global CURRENT_THEME
    global BG, PANEL, INPUT_BG, INPUT_FG, TEXT, MUTED, BORDER, HOVER
    global ALT_ROW_BG, TOOLTIP_BG, GROUPBOX_TITLE
    global ACCENT, ACCENT_D, ERROR, SUCCESS, WARNING
    global CMD_IMPORT, CMD_RE
    global BUTTON_BG, BUTTON_HOVER_BG, BUTTON_PRESSED_BG
    global BUTTON_DISABLED_BG, BUTTON_DISABLED_BORDER, DISABLED_TEXT
    global PRIMARY_HOVER_BORDER, PRIMARY_DISABLED_BG, PRIMARY_DISABLED_BORDER
    global SCROLLBAR_BG, SCROLLBAR_HANDLE, SCROLLBAR_HANDLE_HOVER
    global SPLITTER, LIST_HOVER_BG
    global STATUS_WAITING, STATUS_RUNNING, STATUS_DONE, STATUS_ERROR
    global SCAN_BLOCK_COLORS

    CURRENT_THEME = theme
    BG, PANEL, INPUT_BG, INPUT_FG = theme.bg, theme.panel, theme.input_bg, theme.input_fg
    TEXT, MUTED, BORDER, HOVER = theme.text, theme.muted, theme.border, theme.hover
    ALT_ROW_BG, TOOLTIP_BG, GROUPBOX_TITLE = theme.alt_row_bg, theme.tooltip_bg, theme.groupbox_title
    ACCENT, ACCENT_D, ERROR = theme.accent, theme.accent_d, theme.error
    SUCCESS, WARNING = theme.success, theme.warning
    CMD_IMPORT, CMD_RE = theme.cmd_import, theme.cmd_re
    BUTTON_BG, BUTTON_HOVER_BG, BUTTON_PRESSED_BG = (
        theme.button_bg, theme.button_hover_bg, theme.button_pressed_bg,
    )
    BUTTON_DISABLED_BG, BUTTON_DISABLED_BORDER = theme.button_disabled_bg, theme.button_disabled_border
    DISABLED_TEXT = theme.disabled_text
    PRIMARY_HOVER_BORDER = theme.primary_hover_border
    PRIMARY_DISABLED_BG, PRIMARY_DISABLED_BORDER = (
        theme.primary_disabled_bg, theme.primary_disabled_border,
    )
    SCROLLBAR_BG, SCROLLBAR_HANDLE, SCROLLBAR_HANDLE_HOVER = (
        theme.scrollbar_bg, theme.scrollbar_handle, theme.scrollbar_handle_hover,
    )
    SPLITTER, LIST_HOVER_BG = theme.splitter, theme.list_hover_bg
    STATUS_WAITING, STATUS_RUNNING, STATUS_DONE, STATUS_ERROR = (
        theme.status_waiting, theme.status_running, theme.status_done, theme.status_error,
    )
    SCAN_BLOCK_COLORS = dict(theme.scan_block_colors)


# Sane defaults so anything importing style.py before apply_theme() runs
# (e.g. a standalone script) still sees a fully-populated light theme.
_rebind_globals(THEMES[DEFAULT_THEME_KEY])

# ── UI scale ────────────────────────────────────────────────────────────────
# Single multiplier applied to every hard-coded font/widget/window pixel size
# (see the ``config`` "ui_scale" key). Set once at startup via set_scale()
# before any widgets are built — not live-updatable mid-session.
SCALE = 1.0


def set_scale(factor: float) -> None:
    """Set the global UI scale multiplier used by :func:`px`."""
    global SCALE
    SCALE = factor if factor and factor > 0 else 1.0


def px(n: int | float) -> int:
    """Scale a pixel literal by the current :data:`SCALE`."""
    return round(n * SCALE)


@contextlib.contextmanager
def temporary_theme(theme_key: str, *, scale: float | None = None):
    """Render one thing under a different palette, then put everything back.

    For output that leaves the screen. A PDF is printed onto white paper, but
    every color in this app comes from the *session's* theme -- exporting from
    a dark session would put near-white text on a white page. The report's
    exporter wraps its render in ``temporary_theme("light", scale=1.0)``, which
    also pins the UI-scale multiplier so a document's type size does not depend
    on how large the operator likes their widgets.

    Only safe around code that reads these globals and draws immediately, which
    is exactly what :mod:`report_render` does. Never hold a widget across it --
    widget stylesheets are resolved once at construction, so they would keep
    whatever palette happened to be bound at the time.
    """
    previous_theme, previous_scale = CURRENT_THEME, SCALE
    _rebind_globals(resolve(theme_key))
    if scale is not None:
        set_scale(scale)
    try:
        yield
    finally:
        _rebind_globals(previous_theme)
        set_scale(previous_scale)


def darken(hex_color: str, factor: int = 130) -> str:
    """Darken a "#rrggbb" color (`factor` > 100 = darker; Qt's own scale).

    Lets "shadow palette" files (mode buttons, stop button, ...) derive
    hover/pressed variants from a single theme token instead of hardcoding
    their own hex shades that would go stale under a different theme.
    """
    from PyQt5 import QtGui

    return QtGui.QColor(hex_color).darker(factor).name()


def lighten(hex_color: str, factor: int = 130) -> str:
    """Lighten a "#rrggbb" color (`factor` > 100 = lighter; Qt's own scale)."""
    from PyQt5 import QtGui

    return QtGui.QColor(hex_color).lighter(factor).name()


# ── SVG glyphs for QSS sub-controls ────────────────────────────────────────────

def _make_checkmark_svg() -> str:
    """White tick SVG → temp file.  Returns forward-slash path for Qt QSS."""
    svg = (
        b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 14 14'>"
        b"<polyline points='2,7 5.5,11 12,3' stroke='white' stroke-width='2.2'"
        b" fill='none' stroke-linecap='round' stroke-linejoin='round'/>"
        b"</svg>"
    )
    f = tempfile.NamedTemporaryFile(suffix=".svg", delete=False)
    f.write(svg)
    f.close()
    atexit.register(os.unlink, f.name)
    return f.name.replace("\\", "/")


def _make_arrow_svg(direction: str = "down", color: str = "#444444") -> str:
    """Small filled triangle arrow → temp file, for spinbox/combo sub-controls."""
    pts = "2,7 8,7 5,2" if direction == "up" else "2,3 8,3 5,8"
    svg = (
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'>"
        f"<polygon points='{pts}' fill='{color}'/></svg>"
    ).encode()
    f = tempfile.NamedTemporaryFile(suffix=".svg", delete=False)
    f.write(svg)
    f.close()
    atexit.register(os.unlink, f.name)
    return f.name.replace("\\", "/")


def stylesheet(
    theme: Theme,
    checkmark_svg: str,
    up_arrow_svg: str = "",
    down_arrow_svg: str = "",
    font_family: str = "",
) -> str:
    """Return the full application QSS for `theme`, scaled by :data:`SCALE`.

    `font_family` is a QSS font-family value (see :data:`FONT_STACKS`); an
    empty string omits the rule entirely so the OS/Qt default font is used.
    """
    t = theme
    font_rule = f"font-family: {font_family};" if font_family else ""
    return f"""
    QWidget {{ color: {t.text}; font-size: {px(12)}px; {font_rule} }}
    QMainWindow, QScrollArea, QSplitter {{ background: {t.bg}; }}
    QScrollArea {{ border: none; }}
    QToolTip {{
        background: {t.tooltip_bg}; color: {t.text}; border: 1px solid {t.border};
        border-radius: {px(4)}px; padding: {px(4)}px {px(6)}px;
    }}

    /* ── Context menus ─────────────────────────────────────────── */
    QMenu {{ background: {t.panel}; color: {t.text}; border: 1px solid {t.border}; border-radius: {px(6)}px; }}
    QMenu::item {{ padding: {px(5)}px {px(22)}px; background: transparent; border-radius: {px(4)}px; }}
    QMenu::item:selected {{ background: {t.accent}; color: white; }}
    QMenu::item:disabled {{ color: {t.muted}; }}
    QMenu::separator {{ height: {px(1)}px; background: {t.border}; margin: {px(4)}px {px(6)}px; }}
    QMenuBar {{ background: {t.bg}; color: {t.text}; }}
    QMenuBar::item:selected {{ background: {t.accent}; color: white; border-radius: {px(4)}px; }}

    /* ── Top toolbar ───────────────────────────────────────────── */
    QFrame#toolbar {{ background: {t.panel}; border-bottom: 1px solid {t.border}; }}

    /* ── Section cards ─────────────────────────────────────────── */
    QGroupBox {{
        border: 1px solid {t.border};
        border-radius: {px(8)}px;
        margin-top: {px(10)}px;
        padding: {px(8)}px {px(6)}px {px(6)}px {px(6)}px;
        background: {t.panel};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: {px(10)}px;
        padding: 0 {px(5)}px;
        color: {t.groupbox_title};
        font-weight: 600;
    }}
    QGroupBox::indicator {{ width: {px(14)}px; height: {px(14)}px; }}

    /* ── Buttons (flat, modern) ───────────────────────────────── */
    QPushButton {{
        color: {t.text};
        background: {t.button_bg};
        border: 1px solid {t.border};
        border-radius: {px(6)}px;
        padding: {px(5)}px {px(12)}px;
        min-height: {px(18)}px;
    }}
    QPushButton:hover {{ background: {t.button_hover_bg}; border-color: {t.hover}; }}
    QPushButton:pressed {{ background: {t.button_pressed_bg}; }}
    QPushButton:disabled {{ color: {t.disabled_text}; background: {t.button_disabled_bg}; border-color: {t.button_disabled_border}; }}
    QPushButton#primary {{
        color: white; font-weight: 600;
        background: {t.accent};
        border: 1px solid {t.accent_d};
    }}
    QPushButton#primary:hover {{ background: {t.accent_d}; border-color: {t.primary_hover_border}; }}
    QPushButton#primary:pressed {{ background: {t.accent_d}; }}
    QPushButton#primary:disabled {{ background: {t.primary_disabled_bg}; color: {t.disabled_text}; border-color: {t.primary_disabled_border}; }}

    /* Toolbar profile switcher: same accent-filled "primary action" look as
       QPushButton#primary above, just larger -- this is the highest-traffic
       control in the toolbar (it drives device/plan discovery), so it needs
       to read as unmissable next to the plain "Bluesky dir" field. */
    QComboBox#profileSwitcher {{
        color: white; font-weight: 700; font-size: {px(15)}px;
        background: {PROFILE_SWITCHER_BG};
        border: 2px solid {PROFILE_SWITCHER_BG_HOVER};
        border-radius: {px(6)}px;
        padding: {px(4)}px {px(10)}px;
        min-height: {px(22)}px;
    }}
    QComboBox#profileSwitcher:hover {{ background: {PROFILE_SWITCHER_BG_HOVER}; }}
    QComboBox#profileSwitcher:disabled {{
        background: {t.primary_disabled_bg}; color: {t.disabled_text}; border-color: {t.primary_disabled_border};
    }}
    /* Popup item colors for this combo are enforced by ContrastItemDelegate
       (see below) instead of a QAbstractItemView stylesheet rule -- Qt's
       item-view text rendering picks up a faint tint derived from the
       current QPalette.Highlight color for non-selected rows, which a
       stylesheet selector cannot override. The delegate sets the palette
       directly at paint time, which does take effect. */

    /* ── Inputs ───────────────────────────────────────────────── */
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
        background: {t.input_bg}; color: {t.input_fg};
        border: 1px solid {t.border}; border-radius: {px(6)}px;
        selection-background-color: {t.accent}; selection-color: white;
        min-height: {px(18)}px; padding: {px(2)}px {px(6)}px;
    }}
    QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border: {px(2)}px solid {t.accent};
    }}
    QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
        background: {t.button_disabled_bg}; color: {t.disabled_text};
    }}
    /* Invalid fields (datatype / required check) get a red border. */
    QLineEdit[invalid="true"], QPlainTextEdit[invalid="true"], QComboBox[invalid="true"] {{
        border: {px(2)}px solid {t.error};
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding; subcontrol-position: center right;
        width: {px(20)}px; border-left: 1px solid {t.border};
    }}
    QComboBox::down-arrow {{ image: url({down_arrow_svg}); width: {px(9)}px; height: {px(9)}px; }}
    QComboBox QAbstractItemView {{
        background: {t.input_bg}; color: {t.input_fg};
        selection-background-color: {t.accent}; selection-color: white;
        border: 1px solid {t.border}; border-radius: {px(6)}px; outline: 0;
    }}
    QComboBox:disabled {{ background: {t.button_disabled_bg}; }}

    /* Monospace boxes (command display, positions, notes). */
    QPlainTextEdit#mono, QTextEdit#mono {{
        background: {t.input_bg}; color: {t.input_fg}; font-family: {MONO_CSS};
    }}

    /* ── Checkboxes / radios (accent when on) ──────────────────── */
    QCheckBox, QRadioButton {{ color: {t.text}; spacing: {px(6)}px; background: transparent; }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: {px(15)}px; height: {px(15)}px;
        border: 1px solid {t.hover}; background: {t.input_bg};
    }}
    QCheckBox::indicator {{ border-radius: {px(4)}px; }}
    QRadioButton::indicator {{ border-radius: {px(8)}px; }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {t.accent}; }}
    QCheckBox::indicator:checked {{
        background: {t.accent}; border-color: {t.accent}; image: url({checkmark_svg});
    }}
    QRadioButton::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; }}

    /* ── Item views (lists, trees, file dialogs) ──────────────── */
    QTreeView, QListView, QListWidget, QColumnView, QTableView {{
        background: {t.input_bg}; color: {t.input_fg};
        alternate-background-color: {t.alt_row_bg};
        selection-background-color: {t.accent}; selection-color: white;
        border: 1px solid {t.border}; border-radius: {px(6)}px; outline: 0;
    }}
    QListWidget::item:hover, QTreeView::item:hover {{ background: {t.list_hover_bg}; }}
    QListWidget::item:selected, QTreeView::item:selected {{
        background: {t.accent}; color: white;
    }}
    QHeaderView::section {{
        background: {t.panel}; color: {t.muted}; border: none;
        border-bottom: 1px solid {t.border}; padding: {px(4)}px {px(6)}px;
    }}

    /* ── Scrollbars / splitter / status bar ───────────────────── */
    QScrollBar:vertical {{ background: {t.scrollbar_bg}; width: {px(11)}px; margin: 0; border: none; }}
    QScrollBar::handle:vertical {{ background: {t.scrollbar_handle}; border-radius: {px(5)}px; min-height: {px(24)}px; }}
    QScrollBar::handle:vertical:hover {{ background: {t.scrollbar_handle_hover}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
    QScrollBar:horizontal {{ background: {t.scrollbar_bg}; height: {px(11)}px; margin: 0; border: none; }}
    QScrollBar::handle:horizontal {{ background: {t.scrollbar_handle}; border-radius: {px(5)}px; min-width: {px(24)}px; }}
    QScrollBar::handle:horizontal:hover {{ background: {t.scrollbar_handle_hover}; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
    QSplitter::handle {{ background: {t.splitter}; }}
    QSplitter::handle:hover {{ background: {t.hover}; }}
    QMainWindow::separator {{ background: {t.splitter}; width: {px(10)}px; height: {px(10)}px; }}
    QMainWindow::separator:hover {{ background: {t.hover}; }}
    QStatusBar {{ color: {t.muted}; }}
    """


# ── Layout helpers ──────────────────────────────────────────────────────────────

class LabelRight(QtWidgets.QLabel):
    """Right-aligned, vertically-centred label (Dioptas form style)."""

    def __init__(self, text="", parent=None):
        """Create the label and set right / vertical-centre alignment."""
        super().__init__(text, parent)
        self.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)


def hline() -> QtWidgets.QFrame:
    """A thin horizontal separator line in the border colour."""
    f = QtWidgets.QFrame()
    f.setFrameShape(QtWidgets.QFrame.HLine)
    f.setFrameShadow(QtWidgets.QFrame.Plain)
    f.setFixedHeight(px(1))
    f.setStyleSheet(f"background:{BORDER}; border:none;")
    return f


def make_card(title: str) -> QtWidgets.QGroupBox:
    """Return a styled section card (QGroupBox) with a tight QVBoxLayout body.

    Use ``card.body`` to add widgets/layouts.
    """
    gb = QtWidgets.QGroupBox(title)
    body = QtWidgets.QVBoxLayout(gb)
    body.setContentsMargins(8, 6, 8, 6)
    body.setSpacing(5)
    gb.body = body          # type: ignore[attr-defined]
    return gb


def primary_btn(text: str) -> QtWidgets.QPushButton:
    """A prominent accent action button."""
    b = QtWidgets.QPushButton(text)
    b.setObjectName("primary")
    b.setMinimumHeight(px(32))
    return b


def status_button_qss(bg: str) -> str:
    """QSS for a QPushButton whose background is a solid status color, with
    matching hover/pressed states and the standard disabled look. Shared by
    ContAcqButton and the toolbar Attach button."""
    border = darken(bg, 130)
    hover = lighten(bg, 112)
    pressed = darken(bg, 112)
    return (
        f"QPushButton{{background:{bg};color:white;font-weight:bold;"
        f"border:{px(1)}px solid {border};border-radius:{px(6)}px;"
        f"padding:{px(5)}px {px(12)}px;}}"
        f"QPushButton:hover{{background:{hover};border-color:{border};}}"
        f"QPushButton:pressed{{background:{pressed};}}"
        f"QPushButton:disabled{{background:{BUTTON_DISABLED_BG};"
        f"color:{DISABLED_TEXT};border-color:{BUTTON_DISABLED_BORDER};}}"
    )


class _GripHandle(QtWidgets.QSplitterHandle):
    """Splitter handle that paints a small 3-dot grip so it reads as
    draggable instead of blending into the adjacent card border."""

    def paintEvent(self, event) -> None:  # noqa: N802
        """Draw the default handle, then overlay 3 dots along its centerline."""
        super().paintEvent(event)
        from PyQt5 import QtGui

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor(MUTED))
        cx, cy = self.width() / 2.0, self.height() / 2.0
        r = max(1.0, px(1))
        gap = px(4)
        for off in (-gap, 0, gap):
            center = (
                QtCore.QPointF(cx, cy + off)
                if self.orientation() == QtCore.Qt.Horizontal
                else QtCore.QPointF(cx + off, cy)
            )
            painter.drawEllipse(center, r, r)


class Splitter(QtWidgets.QSplitter):
    """QSplitter whose handles paint a grip (see :class:`_GripHandle`).

    Use together with :func:`configure_splitter` at every splitter
    construction site so drag behavior is wide, visible, and consistent
    across the app.
    """

    def createHandle(self) -> QtWidgets.QSplitterHandle:  # noqa: N802
        """Return a grip-painted handle instead of Qt's plain one."""
        return _GripHandle(self.orientation(), self)


def configure_splitter(splitter: QtWidgets.QSplitter) -> None:
    """Apply the shared, comfortable drag settings to `splitter`.

    A wide-enough hit target, live (opaque) resize feedback, and no
    accidental collapse-to-zero from a slightly-off drag — call this right
    after constructing any :class:`Splitter` in the app.
    """
    splitter.setHandleWidth(px(10))
    splitter.setOpaqueResize(True)
    splitter.setChildrenCollapsible(False)


class NoScrollComboBox(QtWidgets.QComboBox):
    """QComboBox that ignores mouse-wheel scrolls so the selection never changes
    by accident.

    The ignored event propagates to the parent (the scroll panel scrolls
    instead), and only clicks / keyboard change the value.  The drop-down popup
    still scrolls normally while it is open.
    """

    def wheelEvent(self, e) -> None:  # noqa: N802
        """Ignore the wheel so it scrolls the panel, not the selection."""
        e.ignore()


class ContrastItemDelegate(QtWidgets.QStyledItemDelegate):
    """Item delegate that forces readable colors on every popup row.

    Qt's item-view painting derives a faint tint for non-selected rows' text
    from the current ``QPalette.Highlight`` color -- a stylesheet
    ``QAbstractItemView`` selector cannot override this (it's resolved from
    the palette at paint time, not from CSS), so under a themed app palette
    it can render as near-invisible text. Overriding ``initStyleOption`` to
    set the palette colors directly on each row's paint option is the one
    approach that reliably takes effect.
    """

    def __init__(self, bg: str, fg: str, highlight_bg: str, highlight_fg: str, parent=None) -> None:
        """Fix `bg`/`fg` for normal rows and `highlight_bg`/`highlight_fg` for the selected row."""
        super().__init__(parent)
        self._bg = bg
        self._fg = fg
        self._highlight_bg = highlight_bg
        self._highlight_fg = highlight_fg

    def initStyleOption(self, option, index) -> None:  # noqa: N802
        """Force the paint-time palette so text stays readable regardless of the app theme."""
        super().initStyleOption(option, index)
        from PyQt5 import QtGui

        option.palette.setColor(QtGui.QPalette.Base, QtGui.QColor(self._bg))
        option.palette.setColor(QtGui.QPalette.Text, QtGui.QColor(self._fg))
        option.palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(self._highlight_bg))
        option.palette.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(self._highlight_fg))


class HoverTip(QtCore.QObject):
    """Custom hover tooltip (like the tk GUI's ``_Tooltip``).

    Shows a small frameless popup immediately when the mouse enters `widget`,
    so it does not depend on the native ``QToolTip`` mechanism (which can be
    flaky under an application stylesheet).  Parented to `widget`, so it lives
    exactly as long as the widget.
    """

    def __init__(self, widget: QtWidgets.QWidget, text: str) -> None:
        """Install an event filter on `widget` to show `text` on hover."""
        super().__init__(widget)
        self._w = widget
        self._text = text
        self._tip: QtWidgets.QLabel | None = None
        widget.setMouseTracking(True)
        widget.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Show the popup on Enter; hide it on Leave / hide / focus-out."""
        et = event.type()
        if et == QtCore.QEvent.Enter:
            self._show()
        elif et in (
            QtCore.QEvent.Leave,
            QtCore.QEvent.Hide,
            QtCore.QEvent.FocusOut,
            QtCore.QEvent.Wheel,
        ):
            self._hide()
        return False

    def _show(self) -> None:
        if self._tip is not None or not self._text:
            return
        pos = self._w.mapToGlobal(QtCore.QPoint(0, self._w.height() + 4))
        tip = QtWidgets.QLabel(self._text, None, QtCore.Qt.ToolTip)
        tip.setWordWrap(True)
        tip.setMaximumWidth(px(440))
        tip.setStyleSheet(
            f"background: {TOOLTIP_BG}; color: {TEXT}; "
            f"border: 1px solid {BORDER}; border-radius: 4px; padding: 5px;"
        )
        tip.move(pos)
        tip.show()
        self._tip = tip

    def _hide(self) -> None:
        if self._tip is not None:
            self._tip.hide()
            self._tip.deleteLater()
            self._tip = None


def clamp_popup_to_window(
    anchor: QtWidgets.QWidget, popup: QtWidgets.QWidget
) -> QtCore.QPoint:
    """Global position for `popup`, anchored below `anchor`, that never spills
    past the right (or left) edge of `anchor`'s top-level window.

    ``Qt.Popup`` widgets are otherwise placed at a fixed offset from the
    anchor regardless of their own width, so a wide popup opened near the
    right edge of the main window extends outside it. Calls
    ``popup.adjustSize()`` first so its sizeHint reflects the fully-built
    layout.
    """
    popup.adjustSize()
    window = anchor.window()
    win_left = window.mapToGlobal(QtCore.QPoint(0, 0)).x()
    win_right = win_left + window.width()
    anchor_pos = anchor.mapToGlobal(QtCore.QPoint(0, anchor.height()))
    x = min(anchor_pos.x(), win_right - popup.width())
    x = max(x, win_left)
    return QtCore.QPoint(x, anchor_pos.y())


def mark_invalid(widget: QtWidgets.QWidget, invalid: bool) -> None:
    """Toggle the red ``invalid`` border on a field and repolish it."""
    if widget.property("invalid") == invalid:
        return
    widget.setProperty("invalid", invalid)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def apply_theme(
    app: QtWidgets.QApplication,
    theme_key: str | None = None,
    font_key: str | None = None,
) -> None:
    """Set Fusion + the chosen theme's palette + font + application stylesheet on `app`."""
    from PyQt5 import QtGui

    global FONT_FAMILY_CSS

    theme = resolve(theme_key)
    _rebind_globals(theme)
    FONT_FAMILY_CSS = resolve_font(font_key)

    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    for role, col in [
        (QtGui.QPalette.Window,          theme.bg),
        (QtGui.QPalette.WindowText,      theme.text),
        (QtGui.QPalette.Base,            theme.input_bg),
        (QtGui.QPalette.AlternateBase,   theme.alt_row_bg),
        (QtGui.QPalette.Text,            theme.input_fg),
        (QtGui.QPalette.Button,          theme.button_bg),
        (QtGui.QPalette.ButtonText,      theme.text),
        (QtGui.QPalette.Highlight,       theme.accent),
        (QtGui.QPalette.HighlightedText, "#ffffff"),
        (QtGui.QPalette.ToolTipBase,     theme.tooltip_bg),
        (QtGui.QPalette.ToolTipText,     theme.text),
    ]:
        pal.setColor(role, QtGui.QColor(col))
    app.setPalette(pal)
    app.setStyleSheet(
        stylesheet(
            theme,
            _make_checkmark_svg(),
            _make_arrow_svg("up", theme.text),
            _make_arrow_svg("down", theme.text),
            FONT_FAMILY_CSS,
        )
    )
