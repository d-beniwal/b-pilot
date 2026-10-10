"""Static-analysis axis discovery: find each motor DEVICE's scannable axes.

In the MPE codebase a "motor" (anything in a ``<bl>_motors.py`` file) is almost
never a directly-scannable object -- it is a multi-axis **device**.  ``lens1E``
is an ``NAxesDevice``/``LensDevice`` instance whose scannable things are
sub-components: ``lens1E.x``, ``lens1E.y``, ...  Even a single-axis device
(``ic1E`` = ``XDevice`` with axes ``['x']``) is not itself settable -- a plan
must be given ``ic1E.x``.  A plan therefore needs ``motor.axis``, not the bare
device name.

This module recovers the axis list for every ``__all__``-exported device found
by :mod:`device_discovery`, using :mod:`ast` only -- no ophyd, no EPICS, no
import (identical guarantee to :mod:`device_discovery` /
:mod:`scan_building_discovery`).  Axis lists are statically recoverable from
three source shapes:

1. **Factory** -- ``XDevice = make_n_axes_device("X", ['x'], MPEMotor)`` /
   ``LensDevice = make_n_axes_device("Lens", ['x','y','z','th','phi','chi'], ...)``.
   The axes are the factory's 2nd positional list literal (or an ``axes=`` kw).
   The generic factory classes live in ``instrument/devices/generic_motors.py``
   (one directory *above* a profile's ``device_search_paths``, so it is never in
   the scan set) and are pulled in via ``from ..generic_motors import *`` --
   :func:`_generic_map_for` follows that relative import.  Some factory classes
   are also defined locally in a device file (``D2Device = make_n_axes_device(
   "D2", ['z','arc'], MPEMotor)``); a local definition wins over the generic one.
2. **Class attribute** -- ``class ZondaHexapod(Device): axes = ['x','y',...]``.
3. **Motor components on a plain Device with no ``axes``** --
   ``class AttenuatorDevice(Device): rz = Fcpt(MPEMotor, ...)`` (also
   ``FoilDevice``, ``HEM``).  The axes are the class-body ``Component``/``Fcpt``
   attribute names whose first argument is a motor class (name ending in
   ``Motor`` or ``EpicsMotor``), in source order.

A device whose constructor resolves to none of these (e.g. a bare
``cork = MPEMotor(...)`` leaf, which is already settable) simply has no axes and
is omitted from the result -- callers then emit the bare device name.

For a **BITS** instrument (e.g. ``3idc-bits``), a multi-axis motor is instead
declared in Guarneri-style YAML via ``apstools.devices.motor_factory.mb_creator``,
whose ``motors:`` mapping's keys *are* the axis names (e.g. ``sample_stage``'s
``motors: {xprime: ..., base_y: ..., zprime: ..., omega: ...}`` -> axes
``['xprime', 'base_y', 'zprime', 'omega']``) -- no AST pattern-matching needed,
just a YAML read (see :func:`_scan_yaml_file`).
"""
from __future__ import annotations

import ast
import os

from . import device_discovery as _dd

# Ophyd component-constructor names that wrap a single motor as one axis.
_COMPONENT_CALLS = {"Component", "Cpt", "FormattedComponent", "Fcpt"}


def _is_motor_class_name(name: str | None) -> bool:
    """True if `name` looks like a settable motor class (EpicsMotor / *Motor)."""
    return bool(name) and (name == "EpicsMotor" or name.endswith("Motor"))


def _call_func_name(call: ast.Call) -> str | None:
    """Constructor/function name of a call's ``func`` (``Name`` or ``Attribute``)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _str_list_literal(node: ast.expr) -> list[str] | None:
    """The list/tuple of string literals in `node`, or None if it isn't one."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    out: list[str] = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            out.append(elt.value)
        else:
            return None
    return out


def _factory_axes(call: ast.Call) -> list[str] | None:
    """axes list from a ``make_n_axes_device(name, axes, ...)`` call, else None."""
    if _call_func_name(call) != "make_n_axes_device":
        return None
    if len(call.args) >= 2:
        axes = _str_list_literal(call.args[1])
        if axes is not None:
            return axes
    for kw in call.keywords:
        if kw.arg == "axes":
            axes = _str_list_literal(kw.value)
            if axes is not None:
                return axes
    return None


def _classdef_axes(node: ast.ClassDef) -> list[str] | None:
    """Axes for a class: explicit ``axes = [...]`` attr, else motor components."""
    # shape 2: a non-empty class-body ``axes = [...]`` wins outright.
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "axes":
                    axes = _str_list_literal(stmt.value)
                    if axes:
                        return axes
    # shape 3: motor Component/Fcpt attributes, in source order.
    motor_attrs: list[str] = []
    for stmt in node.body:
        if not (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Call)
        ):
            continue
        call = stmt.value
        if _call_func_name(call) not in _COMPONENT_CALLS or not call.args:
            continue
        first = call.args[0]
        first_name = first.id if isinstance(first, ast.Name) else (
            first.attr if isinstance(first, ast.Attribute) else None
        )
        if _is_motor_class_name(first_name):
            motor_attrs.append(stmt.targets[0].id)
    return motor_attrs or None


def _class_axes_in_file(tree: ast.Module) -> dict[str, list[str]]:
    """{class-or-factory-alias name: [axis,...]} for one parsed module.

    Keyed by the **assignment target / class name**, never the factory's string
    ``name`` argument -- a beamline may write ``LensEDevice =
    make_n_axes_device("LensDevice", [...])`` where the string shadows an
    imported class; the binding that matters is ``LensEDevice``.
    """
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
        ):
            axes = _factory_axes(node.value)
            if axes is not None:
                out[node.targets[0].id] = axes
        elif isinstance(node, ast.ClassDef):
            axes = _classdef_axes(node)
            if axes:
                out[node.name] = axes
    return out


def _parse_file(path: str) -> ast.Module | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError):
        return None


def _generic_map_for(
    device_file: str, tree: ast.Module, cache: dict[str, dict[str, list[str]]]
) -> dict[str, list[str]]:
    """class->axes map of any ``generic_motors`` module a device file imports.

    Resolves ``from ..generic_motors import *`` (or ``... import MPEMotor``)
    relative to `device_file` via the ``ImportFrom.level`` count, parses that
    module once (cached per resolved path), and returns its axis-bearing
    classes (``XDevice``, ``LensDevice``, ``FoilDevice``, ...).
    """
    merged: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        mod = node.module or ""
        if mod.split(".")[-1] != "generic_motors" or not node.level:
            continue  # only relative imports occur in practice
        base = os.path.dirname(device_file)
        for _ in range(node.level - 1):
            base = os.path.dirname(base)
        path = os.path.normpath(os.path.join(base, *mod.split(".")) + ".py")
        if path not in cache:
            gtree = _parse_file(path)
            cache[path] = _class_axes_in_file(gtree) if gtree is not None else {}
        merged.update(cache[path])
    return merged


def _scan_yaml_file(path: str) -> dict[str, list[str]]:
    """{device_name: [axis,...]} from an ``mb_creator``-style ``motors:`` map."""
    import yaml

    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}

    result: dict[str, list[str]] = {}
    for entries in data.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            motors = entry.get("motors")
            if isinstance(name, str) and isinstance(motors, dict) and motors:
                result[name] = list(motors.keys())
    return result


def scan(paths: list[str]) -> dict[str, list[str]]:
    """{device_name: [axis,...]} for every axis-bearing device under `paths`.

    Devices that resolve to no axes (bare settable motors, unrecognised
    constructors) are omitted.  First-wins dedupe across files, matching
    :func:`device_discovery.scan`'s ordering so the two stay in sync.
    """
    result: dict[str, list[str]] = {}
    seen: set[str] = set()
    generic_cache: dict[str, dict[str, list[str]]] = {}
    for file_path in _dd._iter_py_files(paths):
        tree = _parse_file(file_path)
        if tree is None:
            continue
        merged = {**_generic_map_for(file_path, tree, generic_cache),
                  **_class_axes_in_file(tree)}
        for device in _dd._scan_file(file_path):
            if device.name in seen:
                continue
            seen.add(device.name)
            axes = merged.get(device.class_name or "")
            if axes:
                result[device.name] = list(axes)
    for file_path in _dd._iter_yaml_files(paths):
        for name, axes in _scan_yaml_file(file_path).items():
            if name in seen:
                continue
            seen.add(name)
            if axes:
                result[name] = list(axes)
    return result
