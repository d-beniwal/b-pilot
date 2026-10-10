"""Static-analysis device discovery: find device names without importing them.

Replaces the hand-maintained ``device_manifest.yml``.  A beamline's device
names come straight from source, in one of two formats depending on project
layout (see :mod:`B_PILOT.paths`' ``BLUESKY_LAYOUT``):

* **MPE** (``"mpe"`` layout) — each device module's own ``__all__`` list (the
  same list `instrument/collection`-style imports rely on) is read with
  :mod:`ast`, never executed or imported — no ophyd, no EPICS, no hardware.
  Category (``area_detector`` / ``scaler`` / ``motor`` / ``slit`` /
  ``shutter`` / ...) is inferred primarily from the **source filename**:
  beamline device directories under ``instrument/devices/`` already follow a
  one-file-per-type convention (``<bl>_motors.py``, ``<bl>_scalers.py``,
  ``<bl>_area_detectors.py``, ...), which — unlike the device classes
  themselves — is consistent across every device (motors and area detectors
  are built through shared factory functions whose instantiated class name
  carries no category information; see ``.context/DECISIONS.md`` for the
  trace that established this). A small class-name keyword table is
  consulted only when a file's name doesn't match any known suffix.
* **BITS** (``"bits"`` layout, e.g. ``3idc-bits``) — devices are declared in
  Guarneri-style YAML (``configs/devices.yml`` / ``devices_aps_only.yml``),
  consumed at kernel startup by ``apsbits``'s ``make_devices()``. The YAML is
  parsed with :mod:`yaml` (``safe_load`` only — never executed/imported) and
  each entry's ``name:`` becomes a device; category is inferred from the
  entry's top-level **creator dotted path** (``apstools.devices.ad_creator``,
  ``ScalerCH``, ``mb_creator``, ...).

Categories are not a fixed enum: whatever string a match produces becomes a
valid ``device{<category>}`` group, so a new beamline (of either layout) is
onboarded by adding a filename suffix or a creator keyword, not by writing
code. :func:`scan` runs both scanners over whatever files it finds, so a
profile's ``device_search_paths`` may point at either format (or both).
"""
from __future__ import annotations

import ast
import os
from typing import NamedTuple

# Filename substring -> category. Checked first, against the file's basename
# (case-insensitive). Extend this for a new beamline's naming convention.
CATEGORY_FILENAME_SUFFIXES: dict[str, str] = {
    "_area_detectors": "area_detector",
    "_scalers": "scaler",
    "_motors": "motor",
    "_slits": "slit",
    "_shutters": "shutter",
    "_multidet": "multi_detector",
}

# Class/constructor-name substring -> category. Fallback, only consulted when
# the filename matches none of the suffixes above.
CATEGORY_CLASS_KEYWORDS: dict[str, str] = {
    "Motor": "motor",
    "Scaler": "scaler",
    "Shutter": "shutter",
    "Slit": "slit",
    "AreaDetector": "area_detector",
}

# Guarneri-YAML creator dotted-path substring -> category (case-insensitive).
# A BITS instrument's devices.yml has no filename-per-type convention -- the
# creator key (e.g. "apstools.devices.ad_creator", "ophyd.scaler.ScalerCH",
# "apstools.devices.motor_factory.mb_creator") is the only categorization
# signal available, so it plays the role CATEGORY_FILENAME_SUFFIXES plays
# for MPE. Order doesn't matter: the keywords are mutually exclusive
# substrings of the creator paths this codebase actually uses.
CATEGORY_CREATOR_KEYWORDS: dict[str, str] = {
    "ad_creator": "area_detector",
    "shutter": "shutter",
    "scaler": "scaler",
    "motor": "motor",
    "slit": "slit",
}

UNCATEGORIZED = "other"


class DiscoveredDevice(NamedTuple):
    """One `__all__`-exported device name found by :func:`scan`."""

    name: str
    category: str
    source_file: str
    class_name: str | None


def _category_for(filename: str, class_name: str | None) -> str:
    lower = filename.lower()
    for suffix, category in CATEGORY_FILENAME_SUFFIXES.items():
        if suffix in lower:
            return category
    if class_name:
        for keyword, category in CATEGORY_CLASS_KEYWORDS.items():
            if keyword in class_name:
                return category
    return UNCATEGORIZED


def _category_for_creator(creator: str) -> str:
    lower = creator.lower()
    for keyword, category in CATEGORY_CREATOR_KEYWORDS.items():
        if keyword in lower:
            return category
    return UNCATEGORIZED


def _call_name(node: ast.expr) -> str | None:
    """Best-effort single-hop constructor name for an assignment's RHS."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _scan_file(path: str) -> list[DiscoveredDevice]:
    try:
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=path)
    except (OSError, SyntaxError):
        return []

    exported: list[str] = []
    assignments: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t for t in node.targets if isinstance(t, ast.Name)]
        if len(targets) == 1 and targets[0].id == "__all__":
            if isinstance(node.value, (ast.List, ast.Tuple)):
                exported.extend(
                    elt.value
                    for elt in node.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                )
            continue
        for target in targets:
            assignments.setdefault(target.id, node.value)

    filename = os.path.basename(path)
    devices: list[DiscoveredDevice] = []
    for name in exported:
        class_name = _call_name(assignments.get(name)) if name in assignments else None
        devices.append(
            DiscoveredDevice(
                name=name,
                category=_category_for(filename, class_name),
                source_file=path,
                class_name=class_name,
            )
        )
    return devices


def _scan_yaml_file(path: str) -> list[DiscoveredDevice]:
    """Guarneri-style ``devices.yml`` entries -> DiscoveredDevice (never imports)."""
    import yaml

    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []

    devices: list[DiscoveredDevice] = []
    for creator, entries in data.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            # A factory like apsbits's predefined_device takes a per-entry
            # `creator:` naming the actual simulated/real device class (e.g.
            # {creator: ophyd.sim.motor, name: sim_motor}) -- that's more
            # specific than the generic top-level factory key, so prefer it.
            effective_creator = entry.get("creator") if isinstance(entry.get("creator"), str) else str(creator)
            devices.append(
                DiscoveredDevice(
                    name=name,
                    category=_category_for_creator(effective_creator),
                    source_file=path,
                    class_name=effective_creator,
                )
            )
    return devices


def _iter_files(paths: list[str], extensions: tuple[str, ...]) -> list[str]:
    """Shallow walk (top level + one subfolder deep), matching plan_parser's scan."""
    files: list[str] = []
    for base in paths:
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name.lower())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("__"):
                continue
            if entry.is_file() and entry.name.lower().endswith(extensions):
                files.append(entry.path)
            elif entry.is_dir():
                try:
                    for sub in sorted(os.scandir(entry.path), key=lambda e: e.name.lower()):
                        if (
                            sub.is_file()
                            and sub.name.lower().endswith(extensions)
                            and not sub.name.startswith("__")
                        ):
                            files.append(sub.path)
                except OSError:
                    pass
    return files


def _iter_py_files(paths: list[str]) -> list[str]:
    return _iter_files(paths, (".py",))


def _iter_yaml_files(paths: list[str]) -> list[str]:
    return _iter_files(paths, (".yml", ".yaml"))


def scan(paths: list[str]) -> list[DiscoveredDevice]:
    """Discover every device under `paths` (never imports).

    Merges MPE's ``__all__``-exported Python devices with BITS's
    Guarneri-YAML entries -- a profile's search paths may contain either
    format (or both). First occurrence wins on a name collision.
    """
    devices: list[DiscoveredDevice] = []
    seen: set[str] = set()
    for file_path in _iter_py_files(paths):
        for device in _scan_file(file_path):
            if device.name in seen:
                continue
            seen.add(device.name)
            devices.append(device)
    for file_path in _iter_yaml_files(paths):
        for device in _scan_yaml_file(file_path):
            if device.name in seen:
                continue
            seen.add(device.name)
            devices.append(device)
    return devices
