"""Shared dtype-aware parameter-form widget building, validation, and parsing.

Extracted out of :class:`B_PILOT.plan_runner.PlanRunnerPanel` so any panel that
needs a docstring-driven parameter form (the plan runner's ordinary — i.e.
non-skeleton — fields, and the compact popups in :mod:`B_PILOT.switchto_popup`
/ :mod:`B_PILOT.contacq_popup`) can build one from a plain ``list[ParamSpec]``
without duplicating the per-dtype widget logic. :func:`build_grid` lays
params out in a vertical :class:`QGridLayout`; :func:`build_row` lays them
out left-to-right in a :class:`QHBoxLayout` for single-row popups. Callers
own their own layout and widgets dict; this module never clears a layout or
holds per-panel state itself.
"""
from __future__ import annotations

import ast

from PyQt5 import QtCore
from PyQt5 import QtGui
from PyQt5 import QtWidgets

from . import config
from . import device_source
from . import style as S
from .plan_parser import _NODEFAULT
from .plan_parser import ParamSpec
from .plan_parser import RawCode
from .skeleton_widgets import MotorAxisListWidget
from .skeleton_widgets import MotorAxisPicker


def label_text(spec: ParamSpec) -> str:
    label = spec.short or spec.name
    if spec.units:
        label += f"  ({spec.units})"
    if spec.required:
        label += "  ★"
    return label


def tooltip(spec: ParamSpec) -> str:
    """Rich hover hint: name, dtype/units, required-ness, description."""
    head = f"{spec.name} : {spec.dtype}"
    if spec.units:
        head += f" [{spec.units}]"
    lines = [head, "required" if spec.required else "optional"]
    detail = spec.long or (spec.short if spec.short != spec.name else "")
    if detail:
        lines += ["", detail]
    return "\n".join(lines)


def _make_field_widget(spec: ParamSpec, on_change):
    """Build the dtype-appropriate input widget for `spec`, wired to `on_change`.

    Shared by :func:`build_grid` (vertical grid, one row per param) and
    :func:`build_row` (horizontal single row) so both layouts get identical
    per-dtype behavior from one place. Does not attach the tooltip — callers
    do that once they also have the label widget.
    """
    if spec.dtype == "positions":
        widget = QtWidgets.QPlainTextEdit()
        widget.setObjectName("mono")
        widget.setFixedHeight(S.px(90))
        widget.setPlaceholderText("100, 0, 50\n150, 0, 50")
        widget.textChanged.connect(on_change)
    elif spec.dtype == "bool":
        widget = QtWidgets.QCheckBox()
        widget.setChecked(bool(spec.default))
        widget.toggled.connect(on_change)
    elif spec.dtype == "choice":
        widget = S.NoScrollComboBox()
        opts = spec.choices or (
            [str(spec.default)] if spec.default is not None else []
        )
        widget.addItems(opts)
        if spec.default is not None and str(spec.default) in opts:
            widget.setCurrentText(str(spec.default))
        widget.currentTextChanged.connect(on_change)
    elif spec.dtype == "device" and spec.category == "motor" and not spec.motor_whole:
        # A motor is a multi-axis device -> pick motor + axis; the
        # generated token is `motor.axis` (see MotorAxisPicker). Skipped for
        # `device{motor:whole}` fields, which fall through to the plain
        # `device` branch below (bare device, no axis resolution).
        widget = MotorAxisPicker(
            allow_blank=spec.blank_omits or not spec.required
        )
        if spec.default not in (None, _NODEFAULT):
            widget.set_from(str(spec.default), None)
        widget.changed.connect(on_change)
    elif spec.dtype == "device":
        # One device object -> dropdown of names for this category.
        widget = S.NoScrollComboBox()
        names = device_source.get_catalog().names_for(spec.category)
        # An optional device (None default / not required) gets a blank
        # entry meaning "omit the arg -> plan uses its default device".
        if spec.blank_omits or not spec.required:
            widget.addItem("")
        widget.addItems(names)
        if spec.default not in (None, _NODEFAULT) and str(spec.default) in names:
            widget.setCurrentText(str(spec.default))
        widget.currentTextChanged.connect(on_change)
    elif spec.dtype == "device_list" and spec.category == "motor":
        # List of motor axes -> a repeatable motor+axis picker per item
        # (a flat multi-select can't attach a per-item axis choice).
        widget = MotorAxisListWidget()
        widget.changed.connect(on_change)
    elif spec.dtype == "device_list":
        # List of device objects -> multi-select of names for the category.
        widget = QtWidgets.QListWidget()
        widget.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection
        )
        widget.addItems(device_source.get_catalog().names_for(spec.category))
        widget.setFixedHeight(S.px(90))
        widget.itemSelectionChanged.connect(on_change)
    elif spec.dtype == "block_list":
        # List of building-block references (suspenders / pseudo_suspenders)
        # -> multi-select over the same `plan_building_blocks` catalog the
        # single-valued `block` dtype uses. Unlike `block` this one may be
        # left empty: an empty suspender list is the plan's own default and
        # a perfectly ordinary scan.
        widget = QtWidgets.QListWidget()
        widget.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection
        )
        widget.addItems(
            (config.get("plan_building_blocks") or {}).get(spec.category) or []
        )
        widget.setFixedHeight(S.px(90))
        widget.itemSelectionChanged.connect(on_change)
    elif spec.dtype == "block":
        # scan_skeletons.py building-block function reference (plan_opener/
        # per_step/plan_closer) -> dropdown of names from the active
        # profile's discovered `plan_building_blocks` catalog. Unlike
        # `device`, never offer a blank entry: a missing per_step breaks
        # the plan at run time (no sensible "omit" fallback), so this dtype
        # always requires a real selection (see `field_error`).
        widget = S.NoScrollComboBox()
        names = (config.get("plan_building_blocks") or {}).get(spec.category) or []
        widget.addItems(names)
        if spec.default not in (None, _NODEFAULT) and str(spec.default) in names:
            widget.setCurrentText(str(spec.default))
        widget.currentTextChanged.connect(on_change)
    elif spec.dtype == "code":
        # Raw Python expression, emitted unquoted (see plan_parser._KNOWN_DTYPES).
        # Unlike every other dtype this prefills even a ``None`` default: for a
        # code field ``None`` is itself meaningful, editable source text, and
        # showing it is the difference between "the default is None" and "this
        # field is blank". Clearing the box still omits the argument entirely
        # when `blank_omits` (a None default), so the code default is reachable.
        widget = QtWidgets.QLineEdit()
        widget.setObjectName("mono")
        if spec.default is not _NODEFAULT:
            widget.setText(_code_default_text(spec.default))
        widget.setPlaceholderText("Python expression, e.g. {'sample': 'A'}")
        widget.textChanged.connect(on_change)
    else:  # str / int / float / unknown -> line edit
        widget = QtWidgets.QLineEdit()
        # Datatype enforcement: numeric fields reject non-numeric input.
        if spec.dtype == "int":
            widget.setValidator(QtGui.QIntValidator())
        elif spec.dtype == "float":
            v = QtGui.QDoubleValidator()
            v.setNotation(QtGui.QDoubleValidator.StandardNotation)
            v.setLocale(QtCore.QLocale.c())
            widget.setValidator(v)
        if spec.default not in (None, _NODEFAULT):
            widget.setText(str(spec.default))
        widget.textChanged.connect(on_change)
    return widget


def build_grid(
    grid: QtWidgets.QGridLayout,
    params: list[ParamSpec],
    on_change,
    row_offset: int = 0,
) -> dict[str, tuple[ParamSpec, object]]:
    """Populate `grid` with one label+widget row per `ParamSpec`, starting at
    `row_offset`. Returns ``{name: (spec, widget)}``; the caller merges this
    into its own widgets dict. Does not clear the grid — the caller is
    responsible for that (skeleton forms place other widgets in the rows
    above `row_offset`)."""
    widgets: dict[str, tuple[ParamSpec, object]] = {}
    for row, spec in enumerate(params, start=row_offset):
        tip = tooltip(spec)
        lbl = S.LabelRight(label_text(spec))
        lbl.setWordWrap(True)
        S.HoverTip(lbl, tip)
        grid.addWidget(lbl, row, 0)

        widget = _make_field_widget(spec, on_change)
        S.HoverTip(widget, tip)
        grid.addWidget(widget, row, 1)
        widgets[spec.name] = (spec, widget)

    grid.setRowStretch(len(params) + row_offset, 1)
    return widgets


def build_row(
    layout: QtWidgets.QHBoxLayout,
    params: list[ParamSpec],
    on_change,
) -> dict[str, tuple[ParamSpec, object]]:
    """Populate `layout` left-to-right with one label-above-widget column per
    `ParamSpec`. Returns ``{name: (spec, widget)}``, same shape as
    :func:`build_grid`, so :func:`field_error`/:func:`parse_values` work
    unchanged against either layout. Used by the compact single-row popups
    (switch-to shortcuts, cont_acq start) instead of the plan runner's
    vertical grid."""
    widgets: dict[str, tuple[ParamSpec, object]] = {}
    for spec in params:
        tip = tooltip(spec)
        lbl = S.LabelRight(label_text(spec))
        lbl.setWordWrap(True)
        S.HoverTip(lbl, tip)

        widget = _make_field_widget(spec, on_change)
        S.HoverTip(widget, tip)

        col = QtWidgets.QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(lbl)
        col.addWidget(widget)
        layout.addLayout(col)
        widgets[spec.name] = (spec, widget)

    return widgets


def clear_layout(layout) -> None:
    """Recursively delete every widget/child-layout `layout` holds.

    `build_row` nests each param's label+widget in its own child
    `QVBoxLayout`, so a plain `takeAt(0).widget()` loop (as `build_grid`'s
    callers use for their flat grid) misses those — this walks child layouts
    too. Callers of `build_row` use this instead when rebuilding the row
    (e.g. on a shortcut-picker dropdown change).
    """
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            clear_layout(child)


def field_error(spec: ParamSpec, widget) -> str | None:
    """Return an error string for `widget`, or None when it is acceptable."""
    short = spec.short or spec.name
    if spec.dtype == "positions":
        raw = widget.toPlainText().strip()
        if not raw:
            return f"{short}: required" if spec.required else None
        for i, line in enumerate(raw.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                parts = [float(x.strip()) for x in line.split(",")]
            except ValueError:
                return f"{short} line {i}: non-numeric value"
            if len(parts) != 3:
                return f"{short} line {i}: expected 3 values, got {len(parts)}"
        return None
    if spec.dtype == "bool":
        return None
    if spec.dtype == "choice":
        if not widget.currentText().strip() and spec.required:
            return f"{short}: required"
        return None
    if spec.dtype == "device" and spec.category == "motor" and not spec.motor_whole:
        if not widget.motor():
            return f"{short}: required" if spec.required else None
        return widget.error()  # motor chosen -> may still need an axis
    if spec.dtype == "device":
        if not widget.currentText().strip() and spec.required:
            return f"{short}: required"
        return None
    if spec.dtype == "device_list" and spec.category == "motor":
        errs = widget.errors()
        if errs:
            return "; ".join(errs)
        if not widget.tokens() and spec.required:
            return f"{short}: required"
        return None
    if spec.dtype == "device_list":
        if not widget.selectedItems() and spec.required:
            return f"{short}: required"
        return None
    if spec.dtype == "block_list":
        if not widget.selectedItems() and spec.required:
            return f"{short}: required"
        return None
    if spec.dtype == "block":
        # Always required, regardless of the signature's default (see
        # `build_grid`'s "block" branch) — a blank plan_opener/per_step/
        # plan_closer has no working fallback.
        if not widget.currentText().strip():
            return f"{short}: required"
        return None

    # str / int / float / code / unknown -> line edit
    raw = widget.text().strip()
    if not raw:
        if spec.required and not spec.blank_omits:
            return f"{short}: required"
        return None
    if spec.dtype == "code":
        # Parse-only: catches an unbalanced brace or a stray comma before the
        # command is ever dispatched. Never evaluated -- a bare device name is
        # a perfectly valid expression here and has no value in this process.
        try:
            ast.parse(raw, mode="eval")
        except SyntaxError as exc:
            return f"{short}: not a valid Python expression ({exc.msg})"
        return None
    if spec.dtype == "float":
        try:
            float(raw)
        except ValueError:
            return f"{short}: not a valid number"
    elif spec.dtype == "int":
        try:
            int(raw)
        except ValueError:
            return f"{short}: not a valid integer"
    return None


def _code_default_text(default) -> str:
    """Source text to prefill a ``code`` field with, for a signature default.

    `plan_parser._literal` hands back a real Python value when the default is
    a literal, and the *unparsed source text* when it is not (a module
    constant, a call). Both need to render back to something that reads and
    re-parses as the expression the author wrote -- so a string default that
    came from `ast.unparse` must NOT be quoted again.
    """
    if isinstance(default, str):
        # Already source text (e.g. "_CONSUMER_TICK_DEFAULT") unless it round
        # trips as a literal string, in which case it needs its quotes back.
        try:
            ast.literal_eval(default)
        except (ValueError, SyntaxError):
            return default
        return repr(default)
    return repr(default)


def _read_number(spec, widget, values, errors, short, caster, kind) -> None:
    raw = widget.text().strip()
    if raw:
        try:
            values[spec.name] = caster(raw)
        except ValueError:
            errors.append(f"{short}: not a valid {kind}")
    elif spec.blank_omits:
        pass
    elif spec.required:
        errors.append(f"{short}: required")
    elif spec.default not in (None, _NODEFAULT):
        values[spec.name] = spec.default


def parse_values(
    params: list[ParamSpec], widgets: dict[str, tuple[ParamSpec, object]]
) -> tuple[dict, list[str]]:
    """Extract `{name: value}` from `widgets` (as built by :func:`build_grid`),
    plus a list of validation error strings. `value` is a raw Python value
    (str/int/float/bool/list of tuples) or a :class:`RawCode` for
    device/block refs, which :func:`B_PILOT.command_builder.make_re_line`
    emits unquoted."""
    values: dict = {}
    errors: list[str] = []

    for spec in params:
        widget = widgets[spec.name][1]
        short = spec.short or spec.name

        if spec.dtype == "positions":
            raw = widget.toPlainText().strip()
            if not raw:
                if spec.required:
                    errors.append(f"{short}: required")
                continue
            triples = []
            for i, line in enumerate(raw.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    parts = [float(x.strip()) for x in line.split(",")]
                    if len(parts) != 3:
                        raise ValueError(f"expected 3 values, got {len(parts)}")
                    triples.append(tuple(parts))
                except ValueError as exc:
                    errors.append(f"{short} line {i}: {exc}")
            if triples:
                values[spec.name] = triples
        elif spec.dtype == "bool":
            values[spec.name] = widget.isChecked()
        elif spec.dtype == "choice":
            val = widget.currentText().strip()
            if val:
                values[spec.name] = val
            elif spec.default not in (None, _NODEFAULT):
                values[spec.name] = spec.default
        elif spec.dtype == "device" and spec.category == "motor" and not spec.motor_whole:
            # Motor -> `motor.axis` (RawCode, emitted unquoted).
            if not widget.motor():
                if spec.required:
                    errors.append(f"{short}: required")
                # else: blank -> omit the arg (plan uses its default)
            else:
                err = widget.error()  # e.g. axis still needed
                if err:
                    errors.append(f"{short}: {err}")
                else:
                    values[spec.name] = RawCode(widget.token())
        elif spec.dtype == "device":
            # RawCode -> emitted unquoted (a real object, not a string).
            val = widget.currentText().strip()
            if val:
                values[spec.name] = RawCode(val)
            elif spec.required:
                errors.append(f"{short}: required")
            # else: blank -> omit the arg (plan uses its default device)
        elif spec.dtype == "device_list" and spec.category == "motor":
            # List of `motor.axis` refs (RawCode, emitted unquoted).
            row_errors = widget.errors()
            for err in row_errors:
                errors.append(f"{short}: {err}")
            tokens = widget.tokens()
            if tokens and not row_errors:
                values[spec.name] = RawCode("[" + ", ".join(tokens) + "]")
            elif not tokens and spec.required:
                errors.append(f"{short}: required")
            # else: empty -> omit the arg (plan uses its default, e.g. [])
        elif spec.dtype == "device_list":
            names = [it.text() for it in widget.selectedItems()]
            if names:
                values[spec.name] = RawCode("[" + ", ".join(names) + "]")
            elif spec.required:
                errors.append(f"{short}: required")
            # else: empty -> omit the arg (plan uses its default, e.g. [])
        elif spec.dtype == "block_list":
            # List of function references (RawCode, emitted unquoted).
            names = [it.text() for it in widget.selectedItems()]
            if names:
                values[spec.name] = RawCode("[" + ", ".join(names) + "]")
            elif spec.required:
                errors.append(f"{short}: required")
            # else: empty -> omit the arg (plan uses its default, [])
        elif spec.dtype == "block":
            # RawCode -> emitted unquoted (a real function reference).
            # Always required (see `field_error`) — never omitted.
            val = widget.currentText().strip()
            if val:
                values[spec.name] = RawCode(val)
            else:
                errors.append(f"{short}: required")
        elif spec.dtype == "float":
            _read_number(spec, widget, values, errors, short, float, "number")
        elif spec.dtype == "int":
            _read_number(spec, widget, values, errors, short, int, "integer")
        elif spec.dtype == "code":
            # RawCode -> emitted verbatim, never repr()'d (it is an expression,
            # not a string value).
            raw = widget.text().strip()
            if raw:
                values[spec.name] = RawCode(raw)
            elif spec.blank_omits:
                pass
            elif spec.required:
                errors.append(f"{short}: required")
        else:  # str / unknown -> text
            raw = widget.text().strip()
            if raw:
                values[spec.name] = raw
            elif spec.blank_omits:
                pass
            elif spec.required:
                errors.append(f"{short}: required")
            elif spec.default not in (None, _NODEFAULT):
                values[spec.name] = spec.default

    return values, errors
