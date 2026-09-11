"""Build an Anthropic tool schema from a template's ParamSpecs, and
independently re-validate whatever the model returns against those same
ParamSpecs.

Never trust the LLM's tool-call output as pre-validated just because it
matched the JSON schema -- schemas can't express "this device name must
actually exist in the active profile's catalog," so that check happens here,
by hand, against `device_catalog.DeviceCatalog` (and, for building blocks,
against the profile's `plan_building_blocks` dict from `plan_catalog.py`).

Three dtypes need more than a plain scalar/array JSON type:

* ``block`` -- a plan-building-block function reference (plan_opener/
  per_step/plan_closer/suspender/pseudo_suspender). Enum-restricted to the
  profile's `blocks[category]` list, and -- unlike an ordinary optional
  field -- always forced required: B-PILOT's own GUI treats a blank block
  field as having no working fallback (see B_PILOT/plan_runner.py's
  `_field_error`/`_parse_params`), regardless of what the real function
  signature's own default says.
* ``device``/``device_list`` with ``category == "motor"`` -- in this
  codebase a motor is almost never itself settable (see
  B_PILOT/axis_discovery.py's module docstring): a plan needs ``motor.axis``,
  not the bare device name. The device name field stays a plain string (or
  list of strings) exactly like any other device field; a sibling
  ``<name>_axis`` (or ``<name>_axes`` for device_list) field lets the model
  supply the axis when the chosen motor has more than one. Mirrors
  B_PILOT/skeleton_widgets.py's `MotorAxisPicker` three-way logic (0 axes:
  bare name; 1 axis: auto-resolve; >1: axis required). Exception:
  ``device{motor:whole}`` (``spec.motor_whole``) means the plan wants the
  bare multi-axis device itself (it indexes sub-axes internally) -- no
  ``<name>_axis`` field is offered and the device name passes straight
  through, unresolved.
* ``axes`` -- only present when `template.skeleton` is set (the six
  scan_skeletons.py plans, whose motor(s)/position(s) are a bare `*args`
  that never becomes a ParamSpec at all -- see B_PILOT/plan_parser.py's
  `SKELETON_SHAPES`). An array of per-motor rows, shaped by
  `template.skeleton`'s shape, validated into `clean["__axes__"]` for
  `plan_renderer.render_command` to flatten into positional tokens.
"""
from __future__ import annotations

from .device_catalog import DeviceCatalog
from .plan_context import Template

_JSON_TYPE = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    # `code` is a raw Python expression the GUI emits unquoted; over a JSON
    # tool schema it can only travel as its source text.
    "code": "string",
}
# Any dtype with no richer handling above degrades to a string field rather
# than raising: a KeyError here would take out the whole tool schema for one
# unrecognized parameter (`positions` would already do this today).
_JSON_TYPE_FALLBACK = "string"

_MOTOR_AXIS_DESCRIPTION = (
    "Axis name for the chosen motor (e.g. 'x'), when it has more than one. "
    "Leave unset for a motor with zero or one axis -- those resolve "
    "automatically."
)


def _axes_item_schema(shape: str, motor_names: list[str]) -> dict:
    props = {
        "motor": {"type": "string", "enum": motor_names},
        "axis": {"type": "string", "description": _MOTOR_AXIS_DESCRIPTION},
    }
    required = ["motor"]
    if shape in ("list", "list_grid"):
        props["positions"] = {
            "type": "array",
            "items": {"type": "number"},
            "description": "Position values for this motor.",
        }
        required.append("positions")
    else:
        props["start"] = {"type": "number", "description": "Start position (or delta, for a relative scan)."}
        props["stop"] = {"type": "number", "description": "Stop position (or delta, for a relative scan)."}
        required += ["start", "stop"]
        if shape == "step_grid":
            props["nsteps"] = {"type": "integer", "description": "Number of steps for this motor."}
            required.append("nsteps")
    return {"type": "object", "properties": props, "required": required}


def build_tool_schema(template: Template, catalog: DeviceCatalog, blocks: dict) -> dict:
    """A ``messages.create(tools=[...])`` schema for `template`, device fields
    restricted to names the catalog actually has and block fields restricted
    to the profile's `blocks` (see `plan_catalog.building_blocks`)."""
    properties: dict[str, dict] = {}
    required: list[str] = []

    for spec in template.param_specs:
        if spec.dtype == "block":
            names = blocks.get(spec.category, [])
            properties[spec.name] = {"type": "string", "enum": names, "description": spec.long}
            required.append(spec.name)  # forced -- never optional, regardless of spec.required
            continue

        if spec.dtype == "block_list":
            # List form of `block` (suspenders / pseudo_suspenders). Unlike
            # `block` it is NOT forced required: an empty suspender list is
            # the plan's own default and an ordinary scan.
            names = blocks.get(spec.category, [])
            properties[spec.name] = {
                "type": "array",
                "items": {"type": "string", "enum": names},
                "description": spec.long,
            }
            if spec.required:
                required.append(spec.name)
            continue

        if spec.dtype in ("device", "device_list"):
            names = catalog.names_for(spec.category)
            item_schema = {"type": "string", "enum": names, "description": spec.long}
            prop = {"type": "array", "items": item_schema} if spec.dtype == "device_list" else item_schema
            if spec.units:
                prop["description"] = prop.get("description", "") + f" (units: {spec.units})"
            properties[spec.name] = prop
            if spec.required:
                required.append(spec.name)
            if spec.category == "motor":
                if spec.dtype == "device" and spec.motor_whole:
                    pass  # device{motor:whole} -- whole device wanted, no axis field
                elif spec.dtype == "device":
                    properties[f"{spec.name}_axis"] = {"type": "string", "description": _MOTOR_AXIS_DESCRIPTION}
                else:
                    properties[f"{spec.name}_axes"] = {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            f"{_MOTOR_AXIS_DESCRIPTION} Same order as {spec.name}; use an empty "
                            "string for entries that don't need one."
                        ),
                    }
            continue

        if spec.dtype == "choice":
            prop = {"type": "string", "enum": list(spec.choices or []), "description": spec.long}
        else:
            prop = {
                "type": _JSON_TYPE.get(spec.dtype, _JSON_TYPE_FALLBACK),
                "description": spec.long,
            }
        if spec.units:
            prop["description"] += f" (units: {spec.units})"
        properties[spec.name] = prop
        if spec.required:
            required.append(spec.name)

    if template.skeleton:
        shape, _relative = template.skeleton
        properties["axes"] = {
            "type": "array",
            "minItems": 1,
            "items": _axes_item_schema(shape, catalog.names_for("motor")),
            "description": (
                "One row per motor this scan moves through, in slow-to-fast "
                "(outermost-to-innermost) order."
            ),
        }
        required.append("axes")

    return {
        "name": f"propose_{template.key}_plan",
        "description": f"Propose concrete parameter values for a {template.title} ({template.description})",
        "input_schema": {"type": "object", "properties": properties, "required": required},
    }


DECLINE_TOOL_NAME = "cannot_generate_plan"


def build_decline_tool_schema() -> dict:
    """A tool the model can call instead of proposing a plan, when the request
    doesn't actually describe a scan/count or is missing details it cannot
    reasonably guess -- without this, a forced single-tool call has no way to
    express "I don't know" and will fabricate a schema-valid guess instead."""
    return {
        "name": DECLINE_TOOL_NAME,
        "description": (
            "Call this instead of proposing a plan when the request does not "
            "describe a scan/count at all, or is missing details you cannot "
            "reasonably guess (e.g. it asks for something else entirely, or "
            "names a device that isn't in the available list)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "A short, user-facing explanation of why no plan was "
                        "proposed, and what info would help."
                    ),
                }
            },
            "required": ["reason"],
        },
    }


ASK_USER_TOOL_NAME = "ask_user"


def build_ask_user_tool_schema() -> dict:
    """A tool the model can call to ask a clarifying question instead of either
    guessing or flatly declining -- for requests that are close to buildable but
    missing one resolvable detail (e.g. two similarly-named devices could both
    be what was meant)."""
    return {
        "name": ASK_USER_TOOL_NAME,
        "description": (
            "Ask the user a clarifying question instead of guessing, when the "
            "request is close to something you can build but is missing a "
            "critical, non-guessable detail. Prefer this over "
            f"`{DECLINE_TOOL_NAME}` when a single question would resolve the "
            "ambiguity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "A short, specific question for the user.",
                }
            },
            "required": ["question"],
        },
    }


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _resolve_motor_token(
    label: str, motor: str, requested_axis: str | None, catalog: DeviceCatalog, errors: list[str]
) -> str | None:
    """``motor`` or ``motor.axis`` (see B_PILOT/skeleton_widgets.py's
    `MotorAxisPicker.token()`/`.error()`, mirrored here), or None (with an
    error appended) when the axis can't be resolved."""
    axes = catalog.axes_for(motor)
    if not axes:
        return motor
    if len(axes) == 1:
        return f"{motor}.{axes[0]}"
    if requested_axis in axes:
        return f"{motor}.{requested_axis}"
    if requested_axis:
        errors.append(f"{label}: {requested_axis!r} is not a valid axis of {motor} (choices: {axes})")
    else:
        errors.append(f"{label}: motor {motor} has multiple axes {axes}; specify an axis")
    return None


def _validate_axes_item(shape: str, item: object, index: int, catalog: DeviceCatalog, errors: list[str]) -> dict | None:
    label = f"axes[{index}]"
    if not isinstance(item, dict):
        errors.append(f"{label}: expected an object")
        return None

    motor = item.get("motor")
    if motor not in catalog.names_for("motor"):
        errors.append(f"{label}: {motor!r} is not a known motor device")
        return None

    token = _resolve_motor_token(label, motor, item.get("axis"), catalog, errors)
    if token is None:
        return None
    row = {"motor": token}

    if shape in ("list", "list_grid"):
        positions = item.get("positions")
        if not positions:
            errors.append(f"{label}: 'positions' is required")
            return None
        row["positions"] = list(positions)
    else:
        start, stop = item.get("start"), item.get("stop")
        if start is None or stop is None:
            errors.append(f"{label}: 'start'/'stop' required")
            return None
        row["start"] = float(start)
        row["stop"] = float(stop)
        if shape == "step_grid":
            nsteps = item.get("nsteps")
            if nsteps is None:
                errors.append(f"{label}: 'nsteps' required")
                return None
            row["nsteps"] = int(nsteps)
    return row


def validate(template: Template, raw: dict, catalog: DeviceCatalog, blocks: dict) -> dict:
    """Return a clean kwargs dict (plus, for skeleton templates,
    ``clean["__axes__"]``), or raise :class:`ValidationError`."""
    errors: list[str] = []
    clean: dict = {}

    for spec in template.param_specs:
        if spec.dtype == "block":
            valid = blocks.get(spec.category, [])
            value = raw.get(spec.name)
            if not value:
                errors.append(f"{spec.name}: required (choose one of {valid})")
            elif value not in valid:
                errors.append(f"{spec.name}: {value!r} is not a known {spec.category} building block (choices: {valid})")
            else:
                clean[spec.name] = value
            continue

        if spec.dtype == "block_list":
            valid = blocks.get(spec.category, [])
            value = raw.get(spec.name)
            if spec.name not in raw or value in (None, "", []):
                clean[spec.name] = spec.default
                continue
            if not isinstance(value, (list, tuple)):
                errors.append(f"{spec.name}: expected a list of {spec.category} names")
                continue
            bad = [v for v in value if v not in valid]
            if bad:
                errors.append(
                    f"{spec.name}: unknown {spec.category} building block(s) {bad} (choices: {valid})"
                )
            else:
                clean[spec.name] = list(value)
            continue

        if spec.dtype == "device" and spec.category == "motor":
            value = raw.get(spec.name)
            if value in (None, ""):
                if spec.required:
                    errors.append(f"{spec.name}: required field missing")
                else:
                    clean[spec.name] = spec.default
                continue
            if value not in catalog.names_for(spec.category):
                errors.append(f"{spec.name}: {value!r} is not a known {spec.category} device")
                continue
            if spec.motor_whole:
                # device{motor:whole} -- the plan wants the bare device, never
                # a resolved axis; skip _resolve_motor_token entirely.
                clean[spec.name] = value
                continue
            token = _resolve_motor_token(spec.name, value, raw.get(f"{spec.name}_axis"), catalog, errors)
            if token is not None:
                clean[spec.name] = token
            continue

        if spec.dtype == "device_list" and spec.category == "motor":
            value = raw.get(spec.name)
            if spec.name not in raw or value in (None, ""):
                clean[spec.name] = spec.default
                continue
            valid_names = catalog.names_for(spec.category)
            axes_list = raw.get(f"{spec.name}_axes") or []
            tokens: list[str] = []
            for i, v in enumerate(value):
                if v not in valid_names:
                    errors.append(f"{spec.name}[{i}]: {v!r} is not a known {spec.category} device")
                    continue
                requested = axes_list[i] if i < len(axes_list) else None
                token = _resolve_motor_token(f"{spec.name}[{i}]", v, requested, catalog, errors)
                if token is not None:
                    tokens.append(token)
            clean[spec.name] = tokens
            continue

        present = spec.name in raw and raw[spec.name] not in (None, "")
        if not present:
            if spec.required:
                errors.append(f"{spec.name}: required field missing")
            else:
                clean[spec.name] = spec.default
            continue

        value = raw[spec.name]
        if spec.dtype == "device":
            valid_names = catalog.names_for(spec.category)
            if value not in valid_names:
                errors.append(f"{spec.name}: {value!r} is not a known {spec.category} device")
            clean[spec.name] = value
        elif spec.dtype == "device_list":
            valid_names = catalog.names_for(spec.category)
            bad = [v for v in value if v not in valid_names]
            if bad:
                errors.append(f"{spec.name}: unknown {spec.category} device(s) {bad}")
            clean[spec.name] = list(value)
        elif spec.dtype == "choice":
            if value not in (spec.choices or []):
                errors.append(f"{spec.name}: {value!r} not in {spec.choices}")
            clean[spec.name] = value
        elif spec.dtype == "int":
            try:
                clean[spec.name] = int(value)
            except (TypeError, ValueError):
                errors.append(f"{spec.name}: {value!r} is not an int")
        elif spec.dtype == "float":
            try:
                clean[spec.name] = float(value)
            except (TypeError, ValueError):
                errors.append(f"{spec.name}: {value!r} is not a float")
        elif spec.dtype == "bool":
            clean[spec.name] = bool(value)
        else:  # str
            clean[spec.name] = str(value)

    if template.skeleton:
        shape, _relative = template.skeleton
        raw_axes = raw.get("axes") or []
        if not raw_axes:
            errors.append("axes: at least one motor row is required")
        rows = []
        for i, item in enumerate(raw_axes):
            row = _validate_axes_item(shape, item, i, catalog, errors)
            if row is not None:
                rows.append(row)
        clean["__axes__"] = rows

    if errors:
        raise ValidationError(errors)
    return clean
