"""Single source of truth for every filesystem path the GUI needs.

The GUI is meant to be **portable**: it must run from wherever the
``mpe_bluesky`` project is checked out, on any machine, without hard-coded
absolute paths and without depending on the current working directory.  Every
path below is derived from *this file's own location* (``__file__``), so the GUI
always knows where it lives and can map the rest of the project relative to that
— even when the whole ``mpe_bluesky`` folder is moved between workstations.

Two anchors:

* **GUI bundle** — :data:`GUI_DIR` (the ``B_PILOT`` package) and its parent
  :data:`BUNDLE_DIR`.  Files shipped *next to* the GUI (its config, the device
  manifest, the embedded-kernel starter) live here and travel with the GUI if
  the folder is relocated.
* **Bluesky root** — :data:`BLUESKY_ROOT`, the ``mpe_bluesky`` directory that
  holds ``instrument/``, ``user/``, ``blueskyStarter.sh`` etc.  By default it
  is found by walking *up* from the GUI looking for those markers, so it stays
  correct even if the GUI is moved to a different depth inside the project.
  Beamline runtime code (``from instrument.collection import *``) resolves
  against this.  If the active profile sets a ``bluesky_root`` override (see
  :mod:`config`), that path is used instead — so B-PILOT can live *anywhere*,
  with the real ``mpe_bluesky`` checkout pointed to explicitly rather than
  found by walking up from B-PILOT's own location. No override (the default)
  means "assume B-PILOT is nested inside the project like today." If an
  override is configured but invalid, :data:`BLUESKY_ROOT_OVERRIDE_ERROR`
  records why, so the GUI can warn at startup instead of silently falling
  back (see ``main_window.py``'s startup check).

Two project **layouts** are recognized, reported as :data:`BLUESKY_LAYOUT`:

* ``"mpe"`` — the ``mpe_bluesky`` tree B-PILOT was written for
  (``instrument/`` at the root). Everything derived below keeps exactly the
  values it always had, and this is the only layout the auto-detect walk
  looks for.
* ``"bits"`` — an APS **BITS** instrument such as ``3idc-bits`` (package
  ``id3c``), whose code lives in ``src/<pkg>/`` with no ``instrument/``
  directory at all. Reached only via an explicit ``bluesky_root`` override,
  never by the walk. The src-layout package plays ``instrument/``'s role, so
  ``ICONFIG``/``PLANS_DIR``/``IMPORT_ROOT`` resolve into it instead.

Import this module everywhere instead of recomputing ``os.path.dirname(...)``
chains locally.
"""
from __future__ import annotations

import json
import os


def _abs(*parts: str) -> str:
    """Join + normalize into an absolute, canonical path."""
    return os.path.normpath(os.path.join(*parts))


# ── GUI bundle (relative to this file) ───────────────────────────────────────
GUI_DIR = os.path.dirname(os.path.abspath(__file__))   # .../<bundle>/B_PILOT
BUNDLE_DIR = os.path.dirname(GUI_DIR)                   # parent of B_PILOT (e.g. gui/)

# Files shipped alongside the GUI package — they move *with* the GUI bundle:
CONFIG_PATH = _abs(BUNDLE_DIR, "gui_config.json")  # tiny pointer: {"active_profile": name}
PROFILES_DIR = _abs(BUNDLE_DIR, "profiles")         # one JSON file per beamline profile
TEST_PLANS_DIR = _abs(BUNDLE_DIR, "test_plans")  # unused by default; kept for back-compat
# Kernel starter scripts, one per *project layout* (not per beamline): the
# three MPE profiles share `mpe.sh`, the BITS/3-ID-C profile uses
# `bits_3idc.sh`. They live in one folder so
# adding an instrument means dropping a script in here and naming it in that
# profile's `embedded_starter_script` -- see resolve_starter() below.
STARTERS_DIR = _abs(BUNDLE_DIR, "starter_scripts")
EMBEDDED_STARTER = _abs(STARTERS_DIR, "mpe.sh")

# Directory to put on sys.path so ``import B_PILOT`` works when a module is run as
# a plain script (``python B_PILOT/app.py``) rather than ``python -m B_PILOT``.
PKG_PARENT = BUNDLE_DIR


# Pre-2026-09-03 starter filenames, kept resolvable for workstations whose
# gitignored active_config.json still names one (a pull never rewrites it).
_STARTER_ALIASES = {
    "embedded_kernel_starter.sh": "mpe.sh",
    "embedded_kernel_starter_3idc.sh": "bits_3idc.sh",
}


def resolve_starter(value: str | None) -> str:
    """Resolve a profile's ``embedded_starter_script`` to an absolute path.

    Tried in order, first hit wins:

    1. The value as given (absolute, or relative to :data:`BUNDLE_DIR`) —
       so ``"starter_scripts/bits_3idc.sh"`` works, and an
       absolute path keeps behaving exactly as it always has.
    2. Its basename inside :data:`STARTERS_DIR`, mapping the pre-rename
       filenames through :data:`_STARTER_ALIASES`.

    Step 2 exists purely for **already-deployed workstations**: their
    ``active_config.json`` is gitignored per-workstation state that a pull
    never rewrites (see DECISIONS.md's 2026-09-03 5th entry), so a config
    written before the scripts moved still names the old bundle-root
    location. Rather than silently falling back to a bare ipykernel — which
    starts fine and then fails much later with no devices — we find the
    script under its new home. Returns "" for an unset value, and returns
    the step-1 candidate unchanged when nothing exists, so the caller's own
    ``os.path.exists`` check still decides.
    """
    if not value:
        return ""
    candidate = value if os.path.isabs(value) else _abs(BUNDLE_DIR, value)
    if os.path.exists(candidate):
        return candidate
    base = os.path.basename(value)
    moved = _abs(STARTERS_DIR, _STARTER_ALIASES.get(base, base))
    return moved if os.path.exists(moved) else candidate


# ── Bluesky root (an explicit profile override, else found by walking up) ───
_ROOT_MARKER_DIRS = ("instrument",)                        # must all be present
_ROOT_MARKER_FILES = ("blueskyStarter.sh", "qserver.sh")   # at least one present


def _mpe_root_error(path: str) -> str | None:
    """``None`` if ``path`` looks like the ``mpe_bluesky`` Bluesky root: an
    ``instrument/`` subdirectory plus at least one of the known root scripts.
    Otherwise a human-readable reason it doesn't qualify.

    This is the ONLY layout the auto-detect walk (:func:`_find_bluesky_root`)
    recognizes, so B-PILOT nested inside an ``mpe_bluesky`` checkout resolves
    exactly as it always has. The BITS layout below is reachable only through
    an explicit ``bluesky_root`` profile override — deliberately, since its
    markers are generic enough that a walk-up could otherwise stop at an
    unrelated ancestor directory.
    """
    if not os.path.isdir(path):
        return "the directory does not exist"
    missing_dirs = [d for d in _ROOT_MARKER_DIRS if not os.path.isdir(os.path.join(path, d))]
    if missing_dirs:
        return f"it has no {'/'.join(missing_dirs)}/ subdirectory"
    if not any(os.path.isfile(os.path.join(path, f)) for f in _ROOT_MARKER_FILES):
        return f"it has none of {', '.join(_ROOT_MARKER_FILES)}"
    return None


def _bits_package_dir(path: str) -> str | None:
    """The ``src/<pkg>/`` instrument package of an APS **BITS** checkout.

    A BITS instrument (e.g. ``3idc-bits``, package ``id3c``) has no
    ``instrument/`` directory: its code lives in a src-layout package that
    carries both ``startup.py`` (the ``from <pkg>.startup import *`` entry
    point) and ``configs/iconfig.yml``. Requiring *both* keeps this from
    matching an arbitrary src-layout Python project.

    Returns the absolute package directory, or ``None`` if ``path`` isn't a
    BITS root.
    """
    src = os.path.join(path, "src")
    if not os.path.isdir(src):
        return None
    try:
        entries = sorted(os.scandir(src), key=lambda e: e.name)
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        if os.path.isfile(os.path.join(entry.path, "startup.py")) and os.path.isfile(
            os.path.join(entry.path, "configs", "iconfig.yml")
        ):
            return entry.path
    return None


def _bits_root_error(path: str) -> str | None:
    """``None`` if ``path`` is a BITS root (see :func:`_bits_package_dir`),
    else a human-readable reason it doesn't qualify."""
    if not os.path.isdir(path):
        return "the directory does not exist"
    if _bits_package_dir(path) is None:
        return "it has no src/<package>/ holding both startup.py and configs/iconfig.yml"
    return None


def _bluesky_root_error(path: str) -> str | None:
    """``None`` if ``path`` is a Bluesky root of *either* recognized layout.

    Used to validate an explicit ``bluesky_root`` profile override, so a
    typo'd path is rejected rather than silently trusted, and the reason is
    surfaced (see :func:`_resolve_bluesky_root_override`) instead of
    swallowed. When neither layout matches, both reasons are reported — the
    user knows which layout they meant, and a single combined message avoids
    guessing wrong about their intent.
    """
    mpe_reason = _mpe_root_error(path)
    if mpe_reason is None:
        return None
    bits_reason = _bits_root_error(path)
    if bits_reason is None:
        return None
    if mpe_reason == bits_reason:      # e.g. the directory does not exist
        return mpe_reason
    return f"it is neither an mpe_bluesky root ({mpe_reason}) nor a BITS root ({bits_reason})"


def _layout_of(path: str) -> str:
    """``"mpe"`` or ``"bits"`` — which layout ``path`` is. ``mpe`` wins a tie
    (and is the fallback for a path that is neither), so every pre-existing
    deployment keeps exactly the derived paths it had before BITS support."""
    if _mpe_root_error(path) is None:
        return "mpe"
    return "bits" if _bits_root_error(path) is None else "mpe"


def _find_bluesky_root(start: str) -> str:
    """Walk up from ``start`` to the ``mpe_bluesky`` Bluesky root (see
    :func:`_mpe_root_error`).

    Falls back to two levels above the GUI (the ``<root>/B-PILOT/B_PILOT``
    layout) if no marker is found, so the GUI still works before the project
    is fully in place.
    """
    cur = start
    while True:
        if _mpe_root_error(cur) is None:
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:          # reached the filesystem root — stop
            break
        cur = parent
    return os.path.dirname(BUNDLE_DIR)   # fallback: <root>/B-PILOT/B_PILOT


def _read_json_quiet(path: str) -> dict:
    """Minimal, side-effect-free JSON read: no writes, no migrations.

    Deliberately duplicated rather than imported from :mod:`config` — that
    module imports this one, so importing it back here would be circular.
    This function runs at import time, before ``config``'s profile bootstrap/
    migration logic has had any chance to run, so it must tolerate a missing,
    partial, or pre-migration ``profiles/`` layout and never assume anything
    has been created on disk yet.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001  (missing/malformed file -- no override)
        return {}


def _resolve_bluesky_root_override() -> tuple[str | None, str | None]:
    """Best-effort read of the active profile's ``bluesky_root`` override.

    Returns ``(resolved_path, error)``:

    * ``(None, None)`` — no override configured (no active-profile pointer
      yet, or the key is unset/blank). Auto-detect applies, silently — this
      is the common case, not a problem.
    * ``(None, "<reason>")`` — an override *was* configured but its path
      doesn't satisfy :func:`_bluesky_root_error`. Auto-detect still applies
      as the fallback, but the caller (``main_window.py``) uses the reason
      to warn the user instead of failing silently — a typo'd path
      shouldn't just look like the setting was never touched.
    * ``(path, None)`` — a valid override.

    Honors the legacy ``project_root`` key (pre-2026-08-14 name) if
    ``bluesky_root`` isn't set, read-only — an old profile self-heals to the
    new key on its next Configuration→Save (see ``config.py``'s
    ``_migrate_bluesky_root_key``, which does the same for the live config).

    Mirrors (without importing) ``config.py``'s active-profile and
    active-over-default resolution closely enough to predict what it will
    return, but never writes anything — see :func:`_read_json_quiet`.
    """
    pointer = _read_json_quiet(CONFIG_PATH)
    name = pointer.get("active_profile")
    if not name or not isinstance(name, str):
        return None, None
    profile_dir = _abs(PROFILES_DIR, name)
    active_path = _abs(profile_dir, "active_config.json")
    cfg_path = active_path if os.path.isfile(active_path) else _abs(profile_dir, "default_config.json")
    cfg = _read_json_quiet(cfg_path)
    root = cfg.get("bluesky_root") or cfg.get("project_root")
    if not isinstance(root, str) or not root.strip():
        return None, None
    candidate = os.path.normpath(os.path.abspath(os.path.expanduser(root.strip())))
    reason = _bluesky_root_error(candidate)
    if reason:
        return None, f"The configured Bluesky root ({candidate}) was ignored: {reason}."
    return candidate, None


_override_root, BLUESKY_ROOT_OVERRIDE_ERROR = _resolve_bluesky_root_override()
BLUESKY_ROOT = _override_root or _find_bluesky_root(GUI_DIR)

# Which project layout BLUESKY_ROOT is (see _layout_of).  Everything below
# branches on this; "mpe" reproduces the original values exactly.
BLUESKY_LAYOUT = _layout_of(BLUESKY_ROOT)

if BLUESKY_LAYOUT == "bits":
    # APS BITS instrument (e.g. 3idc-bits, package id3c).  The src-layout
    # package plays the role mpe_bluesky's ``instrument/`` does.
    _BITS_PKG = _bits_package_dir(BLUESKY_ROOT) or _abs(BLUESKY_ROOT, "src")
    INSTRUMENT_DIR = _BITS_PKG                              # src/<pkg>
    PROJECT_USER_DIR = _abs(_BITS_PKG, "user")              # src/<pkg>/user
    ICONFIG = _abs(_BITS_PKG, "configs", "iconfig.yml")
    BLUESKY_STARTER = _abs(BLUESKY_ROOT, "blueskyStarter.sh")   # unused here
    # The package ROOT, not its ``plans/`` subdirectory: a BITS instrument
    # keeps library plans in ``<pkg>/plans/`` but user/campaign plans in
    # ``<pkg>/user/``, and the plan-runner scans a single tree.  Scoping the
    # scan to the package root reaches both; which files are actually offered
    # is the ``visible_plan_files`` whitelist's job, exactly as on MPE.
    PLANS_DIR = _BITS_PKG
    # module = path relative to ``src/`` -> ``id3c.plans.foo``,
    # ``id3c.user.s3idc_plans.bar``.
    IMPORT_ROOT = os.path.dirname(_BITS_PKG)
else:
    INSTRUMENT_DIR = _abs(BLUESKY_ROOT, "instrument")
    PROJECT_USER_DIR = _abs(BLUESKY_ROOT, "user")
    ICONFIG = _abs(INSTRUMENT_DIR, "iconfig.yml")
    BLUESKY_STARTER = _abs(BLUESKY_ROOT, "blueskyStarter.sh")

    # The real MPE plan directory, scanned by the plan-runner's file browser.
    PLANS_DIR = _abs(INSTRUMENT_DIR, "plans")

    # Root the generated ``from <module> import <plan>`` line is resolved
    # against (module = path of the plan file relative to this root).  With
    # IMPORT_ROOT = BLUESKY_ROOT, ``instrument/plans/foo.py`` ->
    # ``instrument.plans.foo``.
    IMPORT_ROOT = BLUESKY_ROOT

# Default working directory for a launched (embedded) kernel: the Bluesky
# root, so the RunEngine's ``from instrument.collection import *`` resolves
# regardless of where the GUI itself was started from.
KERNEL_CWD_DEFAULT = BLUESKY_ROOT


# ── Runtime state (per-user, NOT part of the repo) ───────────────────────────
# Kernel connection files, the plan queue, and transcripts.  Home-based so it is
# writable and per-user on shared beamline workstations.
#
# This is now PINNED: it used to be overridable through a ``session_dir`` config
# key, and that key is what let a profile relocate its kernel/queue/history into
# a macOS temp dir -- which the OS then purges, and which silently orphaned the
# fixed ``~/.bluesky_pilot/<beamline>/kernel.json`` attach path the README
# documents.  ``config.py`` now forces this value for every profile, existing
# and new (see its ``_PINNED_KEYS``).
#
# ``BPILOT_SESSION_DIR`` is the one remaining escape hatch, for a test harness
# that needs an isolated tree.  Deliberately environment-only and process-local:
# unlike a config key it cannot be persisted into a profile, so it can never
# become a permanent, invisible relocation the way ``session_dir`` did.
SESSION_DIR_DEFAULT = os.path.expanduser(
    os.environ.get("BPILOT_SESSION_DIR") or "~/.bluesky_pilot"
)


def ensure_on_syspath() -> None:
    """Put :data:`PKG_PARENT` on ``sys.path`` so ``import B_PILOT`` resolves.

    Safe to call from a script-mode entry point before the package is importable.
    """
    import sys

    if PKG_PARENT not in sys.path:
        sys.path.insert(0, PKG_PARENT)
