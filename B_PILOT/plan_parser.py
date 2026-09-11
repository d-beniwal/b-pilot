"""Docstring-driven plan parser (AST only — never imports the plan module).

Pure stdlib, no Qt.

**Plan detection (MPE):** MPE plans are plain generator functions with no
``@plan`` decorator, so a plan is any top-level generator function — or, when the
module declares ``__all__``, the names it lists.  Detection descends the function
body but not into nested ``def``s, so a plan whose work lives in a decorated
nested ``inner()`` (``return (yield from inner())``) is still found.

**Parameter form:** each detected plan should document its arguments in this
NumPy-style ``Parameters`` grammar so the GUI can build a form::

    Parameters
    ----------
    <name> : <dtype>[ [<units>]]
        <short name> :: <long description>

* dtype in {str, int, float, bool, choice{a, b, ...}, positions, device{cat},
  device_list{cat}, block{cat}, block_list{cat}, code}
* ``block_list{cat}`` is the list form of ``block`` -- a multi-select over the
  same ``plan_building_blocks`` catalog, for arguments that take several
  building blocks at once (``suspenders``, ``pseudo_suspenders``). Unlike
  ``block`` it may be left empty, which omits the argument.
* ``code`` is a raw Python expression, emitted unquoted -- for arguments that
  are neither scalars nor catalog-enumerable (a dict such as ``md``, a list of
  device objects). Validated as a parseable expression, never executed.
* ``device{motor:whole}`` (category ``motor`` only): the plan wants the bare
  multi-axis device itself, never a resolved ``motor.axis`` -- for plans that
  index sub-axes internally (e.g. ``sms.y``). Omitting ``:whole`` is the
  default and means "this field must resolve to exactly one axis."
* units optional, e.g. [deg], [mm], [s], [1/deg]
* body split on the first ' :: ' -> short label / long tooltip
* default + required come from the signature (no default => required;
  a None default => optional, blank omits the argument)
* args not listed in Parameters (e.g. md, scalers, suspenders) are hidden

Importing a plan module would pull in ophyd devices / ``oregistry`` and attempt
EPICS connections, so this reads the file with the ``ast`` module only.
"""

import ast
import json
import os
import re
from collections import namedtuple

from . import paths as _paths

# ── Paths ──────────────────────────────────────────────────────────────────────
# All path anchors live in :mod:`B_PILOT.paths` (derived from the GUI's own
# location, so they stay correct across machines).  USER_DIR points at the real
# MPE plan directory (``instrument/plans/``); which of its files actually show
# up as rows in the plan-runner's file browser is controlled by the
# ``visible_plan_files`` setting in :mod:`config` (edited via the Configuration
# dialog's Plan visibility card), not by this module.

# SRC_DIR is the root the generated "from <module> import <plan>" line is
# resolved against (module = path relative to SRC_DIR).  With SRC_DIR =
# BLUESKY_ROOT, instrument/plans/foo.py -> "instrument.plans.foo".
SRC_DIR = _paths.IMPORT_ROOT
USER_DIR = _paths.PLANS_DIR

# File in USER_DIR checked by default on startup.
DEFAULT_PLAN_FILE = "scans_stationary_gui_testing.py"

# Directory name (anywhere under USER_DIR) whose ``.json`` files are read as
# scan presets rather than ignored.  Restricting by directory keeps an
# unrelated ``.json`` sitting in the plans tree out of the file browser --
# see :func:`scan_user_dir` / :func:`find_preset_specs`.
PRESETS_DIRNAME = "presets"


# ── Docstring / signature parser (AST only — never imports the plan module) ────

# One parsed argument.  default/required/blank_omits come from the SIGNATURE;
# dtype/units/short/long/choices/category come from the DOCSTRING.
#
# ``category`` is only meaningful for the device dtypes and ``block``/
# ``block_list``: for device/device_list it names the device group (e.g.
# "area_detector", "scaler"); for block/block_list it names which of the
# profile's `plan_building_blocks` lists (plan_opener/per_step/plan_closer/
# suspender/pseudo_suspender) the GUI should offer.
#
# ``motor_whole`` is only meaningful for dtype=="device", category=="motor"
# (set via the ``:whole`` typespec modifier, e.g. ``device{motor:whole}``):
# True means the plan wants the bare multi-axis device, so the GUI/AutoPILOT
# must never force/offer an axis choice for this field.
ParamSpec = namedtuple(
    "ParamSpec",
    "name dtype units short long default required choices blank_omits category motor_whole",
    defaults=(None, False),  # category defaults to None, motor_whole to False
)

_NODEFAULT = object()  # sentinel: signature arg with no default (=> required)

# dtypes the form knows how to render.  ``device`` = one device object,
# ``device_list`` = a list of device objects; both emit UNQUOTED names (see
# RawCode) and take an optional ``{category}`` filter. ``block`` = one
# scan_skeletons.py building-block function reference (plan_opener/per_step/
# plan_closer) -- also emits UNQUOTED names, but its dropdown is populated
# from the active profile's `plan_building_blocks` catalog (see
# scan_building_discovery.py) rather than device_source's device catalog, and
# it takes a required ``{category}`` naming which of that catalog's lists
# (plan_opener/per_step/plan_closer) to offer.
# ``code`` = a free-text **Python expression**, emitted verbatim (RawCode) like
# the device/block dtypes rather than through ``repr()``.  It is the escape
# hatch for arguments whose value is neither a scalar nor something the
# catalogs can enumerate -- a dict (``md``), a list of live device objects
# (``flyscan``'s ``detectors``) -- which otherwise could not be offered in the
# form at all, since a ``str`` field would quote them into the wrong type.
# The field is validated as a parseable expression, so a typo is caught before
# dispatch instead of at the kernel.
_KNOWN_DTYPES = {
    "str", "int", "float", "bool", "choice", "positions", "device", "device_list",
    "block", "block_list", "code",
}

# ``instrument/plans/scan_skeletons.py``'s six generic scan plans all take their
# motor(s)/position(s) through a bare ``*args`` -- something `_signature()` (below)
# can never turn into a `ParamSpec`, no matter what the docstring says.  Rather than
# build a general vararg-inference mechanism for a shape that appears nowhere else in
# the codebase, this is an explicit, hand-maintained allowlist: plan name -> (shape,
# relative).  `shape`'s *args token-count-per-row (2/2/3/4) matches
# `scan_skeletons.check_num_args(args, N)` exactly, and drives which fields
# `B_PILOT/skeleton_widgets.MotorRowsWidget` renders per motor row:
#   "list"      -> motor, [p1, p2, ...]           (one explicit position list)
#   "list_grid" -> motor, [p1, p2, ...]            (grid/outer-product version)
#   "step"      -> motor, start, stop              (shared `nsteps` kwarg elsewhere)
#   "step_grid" -> motor, start, stop, nsteps       (nsteps inline per motor)
# `relative` only changes field labels in the widget (start/stop -> deltas) -- the
# token shape is identical, since `mpe_rel_scan`/`mpe_rel_grid_scan` are thin wrappers
# around `mpe_step_scan`/`mpe_step_grid_scan` with the same `*args` shape.
SKELETON_SHAPES: dict[str, tuple[str, bool]] = {
    "mpe_list_scan":      ("list", False),
    "mpe_list_grid_scan": ("list_grid", False),
    "mpe_step_scan":      ("step", False),
    "mpe_step_grid_scan": ("step_grid", False),
    "mpe_rel_scan":       ("step", True),
    "mpe_rel_grid_scan":  ("step_grid", True),
}


class RawCode(str):
    """A string that must be emitted **verbatim (unquoted)** in generated code.

    Device-typed fields resolve to real objects in the running session, so the
    command must read ``expose(det=pg6, scalers=[tc32E])`` — bare names — not
    ``det='pg6'``.  The command builder emits ``RawCode`` values as-is and
    everything else through ``repr()``.  Being a ``str`` subclass, it also
    displays and validates like normal text.
    """

    __slots__ = ()


def _literal(node: ast.AST):
    """Best-effort literal value of a default node (no code execution)."""
    try:
        return ast.literal_eval(node)
    except Exception:  # noqa: BLE001
        try:
            return ast.unparse(node)  # py3.9+
        except Exception:  # noqa: BLE001
            return None


def _module_all(tree) -> list[str] | None:
    """Return the names listed in a module-level ``__all__``, or None if absent.

    MPE plan modules gate their public plan names with an explicit ``__all__``
    (there is no ``@plan`` decorator).  When present we treat it as the list of
    plans; otherwise we fall back to "every top-level generator function".
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    val = _literal(node.value)
                    if isinstance(val, (list, tuple)):
                        return [str(x) for x in val]
    return None


def _is_generator(node) -> bool:
    """True if `node`'s own body contains a ``yield`` / ``yield from``.

    Descends the function body but NOT into nested ``def``/``lambda`` scopes, so
    a plan whose real work lives in a decorated nested ``inner()`` (a common MPE
    pattern) is still detected via its top-level ``yield from inner()``.
    """
    found = False

    def visit(n) -> None:
        nonlocal found
        for child in ast.iter_child_nodes(n):
            if found:
                return
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue  # a separate scope — its yields don't make `node` a generator
            if isinstance(child, (ast.Yield, ast.YieldFrom)):
                found = True
                return
            visit(child)

    visit(node)
    return found


def _signature(node) -> list[tuple[str, object]]:
    """Ordered (name, default-or-_NODEFAULT) for every argument of `node`."""
    a = node.args
    out: list[tuple[str, object]] = []

    positional = list(getattr(a, "posonlyargs", [])) + list(a.args)
    defaults = list(a.defaults)
    n, nd = len(positional), len(defaults)
    for i, arg in enumerate(positional):
        if i >= n - nd:
            out.append((arg.arg, _literal(defaults[i - (n - nd)])))
        else:
            out.append((arg.arg, _NODEFAULT))

    for arg, dnode in zip(a.kwonlyargs, a.kw_defaults, strict=False):
        out.append((arg.arg, _NODEFAULT if dnode is None else _literal(dnode)))

    return out


def _first_paragraph(doc: str) -> str:
    """Docstring summary: first paragraph, whitespace-collapsed."""
    lines: list[str] = []
    for line in doc.strip().splitlines():
        if not line.strip():
            break
        lines.append(line.strip())
    return " ".join(lines)


def _parse_parameters(doc: str) -> dict[str, dict]:
    """Parse the NumPy ``Parameters`` section into {name: {typespec, body}}.

    Returns {} when the docstring has no Parameters section.
    """
    lines = doc.splitlines()

    # locate the "Parameters" title + dashed underline
    start = None
    for i in range(len(lines) - 1):
        under = lines[i + 1].strip()
        if lines[i].strip() == "Parameters" and under and set(under) == {"-"}:
            start = i + 2
            break
    if start is None:
        return {}

    # collect body lines until the next section (dashed header) or an
    # ``Example::``-style block at column 0
    body: list[str] = []
    for j in range(start, len(lines)):
        line = lines[j]
        nxt = lines[j + 1].strip() if j + 1 < len(lines) else ""
        if line.strip() and nxt and set(nxt) == {"-"}:
            break  # this line is the title of the next section
        if line and not line[0].isspace() and line.strip().endswith("::"):
            break  # e.g. "Example::"
        body.append(line)

    # split body into per-argument entries (header at col 0, body indented)
    entries: dict[str, dict] = {}
    cur: dict | None = None
    for line in body:
        if line and not line[0].isspace():
            m = re.match(r"^(\w+)\s*:\s*(.+?)\s*$", line)
            if m:
                cur = {"typespec": m.group(2), "body": []}
                entries[m.group(1)] = cur
            else:
                cur = None  # a col-0 line that is not "name : type"
        elif cur is not None and line.strip():
            cur["body"].append(line.strip())
    return entries


def _parse_typespec(typespec: str) -> tuple[str, str, list[str], str | None, bool]:
    """Parse a typespec into (dtype, units, choices, category, motor_whole).

    Examples::

        'float [deg]'          -> ('float', 'deg', [], None, False)
        'choice{a, b}'         -> ('choice', '', ['a', 'b'], None, False)
        'device{area_detector}'-> ('device', '', [], 'area_detector', False)
        'device_list{scaler}'  -> ('device_list', '', [], 'scaler', False)
        'device'               -> ('device', '', [], None, False)
        'block{plan_opener}'   -> ('block', '', [], 'plan_opener', False)
        'block_list{suspender}'-> ('block_list', '', [], 'suspender', False)
        'device{motor:whole}'  -> ('device', '', [], 'motor', True)
    """
    units = ""
    m = re.search(r"\[([^\]]*)\]\s*$", typespec)
    if m:
        units = m.group(1).strip()
        typespec = typespec[: m.start()].strip()

    dtype = typespec.strip()
    choices: list[str] = []
    category: str | None = None
    motor_whole = False
    # Brace payload after the dtype keyword: choice{a,b} | device{cat} |
    # device_list{cat} | block{cat}.  choice -> comma list; device*/block ->
    # single category, optionally suffixed ``:whole`` (meaningful only for
    # device{motor:whole} -- caller/validator flags any other use as misuse).
    bm = re.match(r"(choice|device_list|device|block_list|block)\s*\{(.*)\}$", dtype)
    if bm:
        dtype = bm.group(1)
        payload = bm.group(2)
        if dtype == "choice":
            choices = [c.strip() for c in payload.split(",") if c.strip()]
        else:
            wm = re.match(r"(.*):whole$", payload.strip())
            if wm:
                motor_whole = True
                payload = wm.group(1)
            category = payload.strip() or None
    return dtype, units, choices, category, motor_whole


def _parse_body(body_lines: list[str]) -> tuple[str, str]:
    """Join body lines, split on the first ' :: ' into (short, long)."""
    text = " ".join(body_lines).strip()
    if "::" in text:
        short, long = text.split("::", 1)
        return short.strip(), long.strip()
    return text, ""


def find_plan_specs(filepath: str, src_dir: str | None = None) -> dict[str, dict]:
    """Return ``{plan_name: spec}`` for a plan file — Python module or preset.

    Dispatches on extension so every caller gets both kinds without branching:
    a ``.py`` file is AST-parsed by :func:`_find_python_plan_specs`, a ``.json``
    preset is resolved by :func:`find_preset_specs` against the skeleton plan it
    names.  Both return the same spec shape, so the form-building code
    downstream cannot tell them apart.

    `src_dir` is the import root a preset's ``parent_module`` is resolved
    against; ignored for ``.py`` files, and defaults to :data:`SRC_DIR`.
    """
    if filepath.endswith(".json"):
        return find_preset_specs(filepath, src_dir)
    return _find_python_plan_specs(filepath)


def _find_python_plan_specs(filepath: str) -> dict[str, dict]:
    """AST-parse a .py file; return {plan_name: {summary, params, documented}}.

    ``params`` is an ordered list of :class:`ParamSpec` (signature order,
    documented args only).  Never imports the module.
    """
    try:
        with open(filepath, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=filepath)
    except (SyntaxError, OSError):
        return {}

    all_names = _module_all(tree)

    specs: dict[str, dict] = {}
    for node in tree.body:  # top-level functions only (skip nested inner() defs)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Plan detection (MPE): if the module declares __all__, its listed names
        # are the plans; otherwise treat every top-level generator function as a
        # plan.  Never expose private (_-prefixed) helpers.
        if node.name.startswith("_"):
            continue
        if all_names is not None:
            if node.name not in all_names:
                continue
        elif not _is_generator(node):
            continue

        doc = ast.get_docstring(node) or ""
        doc_meta = _parse_parameters(doc)

        params: list[ParamSpec] = []
        for name, default in _signature(node):
            if name not in doc_meta:
                continue  # undocumented (e.g. md) => hidden
            dtype, units, choices, category, motor_whole = _parse_typespec(
                doc_meta[name]["typespec"]
            )
            short, long = _parse_body(doc_meta[name]["body"])
            required = default is _NODEFAULT
            blank_omits = (not required) and default is None
            params.append(
                ParamSpec(
                    name=name,
                    dtype=dtype,
                    units=units,
                    short=short or name,
                    long=long,
                    default=default,
                    required=required,
                    choices=choices,
                    blank_omits=blank_omits,
                    category=category,
                    motor_whole=motor_whole,
                )
            )

        specs[node.name] = {
            "summary": _first_paragraph(doc),
            "params": params,
            "documented": bool(doc_meta),
            # (shape, relative) for one of scan_skeletons.py's six plans, else None.
            "skeleton": SKELETON_SHAPES.get(node.name),
            # Cheap staleness guard: True with skeleton=None flags a plan that takes
            # bare *args but isn't in SKELETON_SHAPES (e.g. a future 7th skeleton) --
            # nothing acts on this yet, but it's a greppable signal instead of a
            # silent "just falls back to the generic form, indistinguishable from any
            # other undocumented plan."
            "has_varargs": node.args.vararg is not None,
        }
    return specs


# ── Scan presets (JSON) ───────────────────────────────────────────────────────
# A preset is NOT a plan.  It is a set of pre-filled values for one of the
# `scan_skeletons.py` plans, stored as JSON in the mpe_bluesky repo
# (`instrument/plans/presets/<beamline>/*.json`; see that folder's README.md for
# the schema).  Selecting one in the plan runner builds the *parent plan's*
# ordinary form and fills it in — what gets dispatched is a plain
# `mpe_step_grid_scan(...)` call, so nothing here reaches the RunEngine or needs
# registering with the queueserver.
#
# `find_preset_specs` therefore returns the parent plan's own spec (same
# `ParamSpec` list, same `skeleton` shape) with two extra keys:
#   "preset"   -- the parsed JSON, applied to the widgets by
#                 `plan_runner.PlanRunnerPanel._apply_preset`
#   "dispatch" -- {"plan", "module"} of the parent, so the generated import and
#                 `RE(...)` lines name the skeleton rather than the preset.

# Keys a preset must carry to be usable at all.
_PRESET_REQUIRED_KEYS = ("name", "parent_plan", "parent_module")


def load_preset(filepath: str) -> dict | None:
    """Parse a preset ``.json``, or None if it is unreadable or malformed.

    Only structural validation happens here — that the file is a JSON object
    carrying the keys needed to resolve its parent plan.  Checking the *values*
    against the real skeleton signature is the job of the preset validator that
    ships beside the presets themselves
    (``instrument/plans/presets/validate_presets.py``).
    """
    try:
        with open(filepath, encoding="utf-8") as fh:
            preset = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(preset, dict):
        return None
    if any(not isinstance(preset.get(k), str) for k in _PRESET_REQUIRED_KEYS):
        return None
    return preset


def find_preset_specs(filepath: str, src_dir: str | None = None) -> dict[str, dict]:
    """``{preset_name: spec}`` for one preset file — ``{}`` if unusable.

    The returned spec is the parent plan's, so the plan runner's existing
    skeleton/motor-rows/param-form machinery renders it unchanged.
    """
    preset = load_preset(filepath)
    if preset is None:
        return {}

    root = src_dir or SRC_DIR
    parent_path = os.path.join(root, *preset["parent_module"].split(".")) + ".py"
    parent_specs = _find_python_plan_specs(parent_path)
    parent = parent_specs.get(preset["parent_plan"])
    if parent is None:
        return {}  # stale preset: its parent plan was renamed or removed

    summary = preset.get("summary") or parent["summary"]
    label = preset.get("label")
    return {
        preset["name"]: {
            **parent,
            "summary": f"{label} — {summary}" if label else summary,
            "preset": preset,
            "dispatch": {
                "plan": preset["parent_plan"],
                "module": preset["parent_module"],
            },
        }
    }


# ── Raw signature/docstring access (for docstring-authoring assistance) ────────
# Unlike `find_plan_specs`, these two functions don't filter or interpret a
# docstring against the grammar -- they hand back the raw material (every
# argument, the verbatim docstring text) so a caller (e.g. AutoPILOT) can
# *draft* a compliant docstring for a plan that doesn't have one yet, and
# check a draft before presenting it.


def find_plan_functions_raw(filepath: str) -> list[dict]:
    """AST-parse a .py file; return raw per-plan-function material.

    Same plan-detection rule as :func:`find_plan_specs` (``__all__`` if
    present, else every top-level generator function, skip ``_``-prefixed
    names), but returns *every* signature argument (not just documented
    ones) plus the verbatim existing docstring, so a caller can draft a
    replacement rather than only parse a compliant one. Never imports the
    module. Returns ``[]`` on syntax/OS error.
    """
    try:
        with open(filepath, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=filepath)
    except (SyntaxError, OSError):
        return []

    all_names = _module_all(tree)

    functions: list[dict] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue
        if all_names is not None:
            if node.name not in all_names:
                continue
        elif not _is_generator(node):
            continue

        doc = ast.get_docstring(node)
        doc_meta = _parse_parameters(doc or "")

        args = [
            {
                "name": name,
                "default": None if default is _NODEFAULT else default,
                "required": default is _NODEFAULT,
            }
            for name, default in _signature(node)
        ]

        functions.append(
            {
                "name": node.name,
                "args": args,
                "docstring": doc,
                "documented": bool(doc_meta),
            }
        )
    return functions


def validate_docstring_text(docstring: str, arg_names: list[str] | None = None) -> dict:
    """Check a drafted docstring against the Parameters grammar.

    Parses `docstring` on its own (no file/AST involved) and reports concrete
    problems: an unknown dtype, a documented parameter name not present in
    `arg_names` (when given -- catches a stale/hallucinated docstring that
    documents an argument the function doesn't actually take), and a body
    missing the required ``' :: '`` short/long split. Does not require a
    Parameters section to be present (an empty/undocumented docstring simply
    reports no documented params, not an error).
    """
    doc_meta = _parse_parameters(docstring)
    errors: list[str] = []
    documented_params: dict[str, dict] = {}

    for name, entry in doc_meta.items():
        dtype, units, choices, category, motor_whole = _parse_typespec(entry["typespec"])
        short, long = _parse_body(entry["body"])
        if dtype not in _KNOWN_DTYPES:
            errors.append(f"parameter '{name}': unknown dtype '{dtype}'")
        if motor_whole and not (dtype == "device" and category == "motor"):
            errors.append(f"parameter '{name}': ':whole' is only valid on device{{motor:whole}}")
        if arg_names is not None and name not in arg_names:
            errors.append(f"parameter '{name}': documented but not in the function's real signature {arg_names}")
        if not long:
            errors.append(f"parameter '{name}': body is missing the required ' :: ' short/long split")
        documented_params[name] = {
            "dtype": dtype,
            "units": units,
            "choices": choices,
            "category": category,
            "motor_whole": motor_whole,
            "short": short,
            "long": long,
        }

    return {
        "has_parameters_section": bool(doc_meta),
        "documented_params": documented_params,
        "errors": errors,
    }


# ── File-browser utilities ────────────────────────────────────────────────────


def file_to_module(filepath: str, src_dir: str | None = None) -> str:
    """Module path for the generated import line (relative to `src_dir`).

    `src_dir` is the import root the module path is resolved against; when None
    it falls back to :data:`SRC_DIR`.  e.g. with root ``gui/``,
    ``test_plans/test_file.py`` -> ``test_plans.test_file``.
    """
    root = src_dir or SRC_DIR
    rel = os.path.relpath(filepath, root)
    return rel.replace(os.sep, ".").removesuffix(".py")


def file_defines_function(filepath: str, name: str) -> bool:
    """True if `filepath` has a top-level ``def <name>`` / ``async def <name>``.

    Unlike :func:`find_plan_specs`/:func:`find_plan_functions_raw`, this
    ignores the generator/``__all__`` plan-detection rule entirely -- for a
    callable like ``abort_cleanup`` that is deliberately excluded from a
    shortcuts module's ``__all__`` (so it never shows up as a `switch_to_*`
    plan) but still needs to be located and imported by name.
    """
    try:
        with open(filepath, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=filepath)
    except (SyntaxError, OSError):
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in tree.body
    )


def scan_user_dir(user_dir: str, _depth: int = 0, _in_presets: bool = False) -> list[tuple]:
    """Recursive scan; returns (display_name, kind, abs_path, depth).

    ``depth`` counts directory levels below ``user_dir`` (0 for top-level
    entries, 1 for one directory deep, etc. — used by the GUI to indent).
    Recurses to unlimited depth so plans nested in sub-sub-directories (e.g.
    a per-beamline plans dir's own ``user_plans/`` folder) are found too.

    ``.py`` files are listed everywhere; ``.json`` files only inside a
    :data:`PRESETS_DIRNAME` directory (or below one), so an unrelated JSON
    sitting in the plans tree never shows up as a selectable plan file.
    """
    rows: list[tuple] = []
    try:
        entries = sorted(
            os.scandir(user_dir),
            key=lambda e: (not e.is_dir(), e.name.lower()),
        )
    except OSError:
        return rows
    for entry in entries:
        if entry.name.startswith("__"):
            continue
        if entry.is_dir():
            rows.append((entry.name + "/", "dir", entry.path, _depth))
            rows.extend(scan_user_dir(
                entry.path, _depth + 1,
                _in_presets or entry.name == PRESETS_DIRNAME,
            ))
        elif entry.is_file() and (
            entry.name.endswith(".py")
            or (_in_presets and entry.name.endswith(".json"))
        ):
            rows.append((entry.name, "file", entry.path, _depth))
    return rows
