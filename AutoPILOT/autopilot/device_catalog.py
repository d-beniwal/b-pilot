"""Per-profile device lookup, reusing B-PILOT's Qt-free device discovery.

Wraps ``B_PILOT.device_discovery.scan()`` directly (rather than
``B_PILOT.device_source.get_catalog()``) because the plan renderer needs each
device's ``source_file`` to build the right relative import
(``from ..devices.<beamline>_devices.<file> import <name>``), which
``get_catalog()`` discards after filtering.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from ._bpilot_path import ensure_bpilot_on_path

ensure_bpilot_on_path()

from B_PILOT import axis_discovery as bpilot_axis_discovery  # noqa: E402
from B_PILOT import config as bpilot_config  # noqa: E402
from B_PILOT import device_discovery as bpilot_device_discovery  # noqa: E402
from B_PILOT import device_source as bpilot_device_source  # noqa: E402
from B_PILOT import paths as bpilot_paths  # noqa: E402


@dataclass(frozen=True)
class DeviceCatalog:
    """Devices visible for the active (or a named) profile, grouped by category."""

    beamline: str
    by_category: dict[str, list[str]]
    # device name -> dotted module relative to instrument/plans/, or None for a
    # BITS/YAML-sourced device (see import_line_for): those have no importable
    # Python symbol -- apsbits's make_devices() injects them as kernel globals
    # directly, so no import line is needed or possible.
    import_module_by_name: dict[str, str | None]
    axes: dict[str, list[str]]  # motor device name -> scannable axis names (see B_PILOT/axis_discovery.py)

    def names_for(self, category: str) -> list[str]:
        return list(self.by_category.get(category, []))

    def import_line_for(self, name: str) -> str:
        """A ``from ..x.y import name`` line resolvable from a file inside
        instrument/plans/, or ``""`` if `name` is already a kernel global
        (a BITS/YAML-sourced device -- see :data:`import_module_by_name`)."""
        if name not in self.import_module_by_name:
            raise KeyError(f"Unknown device {name!r} -- not in this catalog")
        module = self.import_module_by_name[name]
        return f"from ..{module} import {name}" if module is not None else ""

    def axes_for(self, name: str) -> list[str]:
        """Scannable axis names for motor device `name` (empty if it has none).

        An empty list means the device is itself settable (e.g. a bare
        ``EpicsMotor``) -- callers then use the bare device name rather than
        ``device.axis``. Mirrors ``B_PILOT/device_source.py``'s
        ``DeviceCatalog.axes_for``.
        """
        return list(self.axes.get(name, []))


def _module_relative_to_project(source_file: str) -> str:
    """``.../instrument/devices/s20ide_devices/s20ide_motors.py`` -> ``devices.s20ide_devices.s20ide_motors``."""
    rel = os.path.relpath(source_file, bpilot_paths.BLUESKY_ROOT)
    dotted = os.path.splitext(rel)[0].replace(os.sep, ".")
    return dotted.removeprefix("instrument.")


def load(profile: str | None = None) -> DeviceCatalog:
    """Build a :class:`DeviceCatalog` for `profile` (default: the active profile).

    Applies the same ``device_selection`` visibility filter the GUI's Devices
    tab uses, so AutoPILOT only ever offers devices a human has chosen to show.
    """
    values = bpilot_config.profile_values(profile) if profile else bpilot_config.as_dict()
    beamline = values.get("beamline") or (profile or "")
    search_paths = values.get("device_search_paths") or []
    selection = values.get("device_selection") or {}

    resolved_paths = [bpilot_device_source.resolve_path(p) for p in search_paths]
    by_category: dict[str, list[str]] = {}
    import_module_by_name: dict[str, str | None] = {}
    for device in bpilot_device_discovery.scan(resolved_paths):
        cat_selection = selection.get(device.category, {})
        if not cat_selection.get(device.name, True):  # unseen names default shown
            continue
        by_category.setdefault(device.category, []).append(device.name)
        if device.source_file.lower().endswith((".yml", ".yaml")):
            # BITS/Guarneri-YAML device: no importable Python symbol exists --
            # apsbits's make_devices() injects it as a kernel global directly.
            import_module_by_name[device.name] = None
        else:
            import_module_by_name[device.name] = _module_relative_to_project(device.source_file)

    # Axes are a structural property of the same source files -- scanned
    # fresh here (never persisted), same AST-only guarantee as the device
    # scan above. Mirrors B_PILOT/device_source.py's get_catalog().
    axes = bpilot_axis_discovery.scan(resolved_paths)

    return DeviceCatalog(
        beamline=beamline,
        by_category=by_category,
        import_module_by_name=import_module_by_name,
        axes=axes,
    )
