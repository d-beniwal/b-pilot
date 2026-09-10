"""Persistent GUI configuration, organized into per-beamline **profiles**.

Small, dependency-free settings store for things the user should be able to
change without editing code:

* **Location** — ``bluesky_root``: an optional override pointing at the real
  ``mpe_bluesky`` checkout, so B-PILOT itself can be checked out anywhere
  rather than nested inside that tree. Empty (the default) means "auto-detect
  by walking up from B-PILOT's own location," i.e. today's behavior. Read
  specially by :mod:`paths` at import time — see its module docstring.
* **Files** — which plan files the plan-runner shows (the search scope):
  ``plans_dir`` (folder scanned), ``import_root`` (root the generated
  ``from <module> import <plan>`` line is resolved against),
  ``default_plan_file`` (checked on startup), and ``visible_plan_files`` (an
  explicit whitelist of ``plans_dir``-relative paths that are even shown as
  rows in the file browser — edited via the Configuration dialog's Plan
  visibility card).
* **Launch** — ``bluesky_startup``: extra command(s) run automatically in the
  console right after *Launch IPython* connects (one per line, in order).
  Empty by default — the instrument import itself is done by the profile's
  starter script (``starter_scripts/*.sh``) as the kernel initializes, not
  from here. Edited per profile via the Configuration dialog's Launch
  Session tab.
* **Devices** — ``device_search_paths`` (directories scanned by
  :mod:`device_discovery`) and ``device_selection`` (per-name shown/hidden).

All of the above (plus Session/Appearance) live in a **profile** — a folder
per beamline under :data:`PROFILES_DIR`, e.g. ``profiles/s20ide/``,
``profiles/s20idd/`` — so a different beamline (different plans, devices,
launch scripts, screen/kernel naming) is a different profile, loadable/
editable/saveable independently, rather than a single hand-edited file. Only
one file, a tiny pointer at :data:`CONFIG_PATH`
(``{"active_profile": "s20ide"}``), says which profile is active (i.e. which
beamline folder is currently selected — not to be confused with the
default/active split described next).

**The profile folder name IS the beamline identity.** ``beamline`` and
``session_dir`` are :data:`_PINNED_KEYS`: derived on load, never written to
either profile file, and not editable in the Configuration dialog. They used
to be ordinary settings, and both drifted — ``profiles/s20ide`` ended up
carrying ``beamline: "20ide"`` (and once ``"testbl"``), while ``session_dir``
was pointed at a macOS temp dir. Each distinct value silently created its own
``~/.bluesky_pilot/<value>/`` tree, so the fixed per-beamline kernel path the
README documents stopped being where the kernel actually was. Pinning them
makes ``profiles/<name>/`` <-> ``~/.bluesky_pilot/<name>/`` a structural
guarantee for every profile, existing and new.

Each profile folder holds **two** files, not one:

* ``default_config.json`` — the shared baseline for that beamline. Meant to
  be committed to git and handed between beamline staff.
* ``active_config.json`` — the live, day-to-day settings actually read by
  the running GUI and written on every *Save*. Gitignored: it's
  per-workstation state, bootstrapped as a copy of ``default_config.json``
  the first time that beamline is touched on a given checkout, and free to
  diverge from then on. The Configuration dialog's *Restore Defaults*
  button reloads (but doesn't yet persist) the default; its explicit
  *Save as Default* button is the only thing that writes back to
  ``default_config.json``.

Defaults come from :mod:`plan_parser` (paths) so there is a single source of
truth for the built-in scope. Both files are otherwise **self-documenting**
— every setting is written out in full, even where it matches the built-in
default — with one exception: the handful of keys in
:data:`_WORKSTATION_KEYS` (paths derived from *this* GUI's own location, plus
one piece of pure runtime state) are only written when they've been
explicitly overridden. That's what keeps a workstation-specific absolute
path out of a profile you commit to git and hand to another beamline. The
Configuration dialog reads/writes through here, and the panels read it
live, so changes apply without a restart (except UI scale).
"""
from __future__ import annotations

import json
import os
import shutil

from . import paths as _paths
from . import plan_parser as _P

# Tiny pointer file: {"active_profile": "<name>"}.
CONFIG_PATH = _paths.CONFIG_PATH
PROFILES_DIR = _paths.PROFILES_DIR

# Built-in defaults. Only keys listed here are persisted / accepted.
DEFAULTS: dict = {
    # Optional override so B-PILOT can be checked out *anywhere*, not just
    # nested inside the mpe_bluesky tree -- empty (the default) means "walk
    # up from B-PILOT's own location looking for instrument/ + a root script,
    # same as always." Read specially, and read-only, by paths.py at import
    # time (paths._resolve_bluesky_root_override()) -- it duplicates a tiny,
    # side-effect-free slice of this module's active-profile resolution
    # rather than importing it, since this module imports paths already.
    # Takes effect on next launch only, like theme/font/ui_scale below.
    "bluesky_root": "",
    "plans_dir": _P.USER_DIR,
    "import_root": _P.SRC_DIR,
    "default_plan_file": _P.DEFAULT_PLAN_FILE,
    # Whitelist of plans_dir-relative paths (forward-slash separated) shown as
    # rows in the plan-runner's file browser. Explicit, not "empty = show all"
    # — Select-all/Deselect-all in the Configuration dialog cover both extremes.
    "visible_plan_files": [_P.DEFAULT_PLAN_FILE],
    # Blank by default: each profile's starter script (starter_scripts/*.sh)
    # performs the instrument import itself inside the kernel, via
    # --IPKernelApp.exec_lines, so there is nothing left to send as a console
    # cell. Set it only to run EXTRA commands after the kernel comes up.
    "bluesky_startup": "",
    # Whether the ``from <module> import <plan>`` line shown above every
    # generated ``RE(plan(...))`` command is actually SENT to the console.
    # Off by default: the profile's starter script already imports the
    # instrument into the kernel namespace, so the line is redundant -- and
    # because it shares one execution with the RE() call, a stale module
    # path would take the plan down with it. The Command panel always shows
    # the line regardless (see command_builder.for_console).
    "send_import_line": False,
    # Working directory the embedded kernel is started in (the toolbar's
    # "Bluesky dir" field). Empty = the Bluesky root, which is what this
    # always used to be. Worth setting per profile because a plan's
    # CWD-relative outputs land here -- 3-ID-C's `flyscan` writes its master
    # files next to the running console, and apsbits resolves iconfig.yml's
    # relative MD_PATH (`.re_md_dict.yml`, which persists RE.md including
    # scan_id) against it too. `~` is expanded when used.
    "kernel_work_dir": "",
    # Console session persistence / reattach:
    "keep_kernel_on_exit": True,          # leave the kernel running when the GUI closes
    "last_kernel_connection_file": "",    # runtime state — path to reattach to
    # Single-instance kernel (see kernel_session.py):
    # Both of these are PINNED (see _PINNED_KEYS): the placeholders below are
    # never the effective value -- `beamline` is forced to the profile folder
    # name and `session_dir` to _paths.SESSION_DIR_DEFAULT on every load.
    "beamline": "",                       # derived: the profile folder name
    "use_screen": True,                   # host the kernel in a named screen session
    "session_dir": _paths.SESSION_DIR_DEFAULT,  # derived: always ~/.bluesky_pilot
    # Starter for the embedded kernel: activates env + records experiment,
    # then starts a connectable ipykernel. Empty = launch a bare ipykernel
    # directly (no env activation / collection import).
    "embedded_starter_script": _paths.EMBEDDED_STARTER,
    # Arguments recorded alongside the embedded kernel launch:
    "dm_experiment": "",
    "setup_file": "exp_setup.yml",
    # Display-only multiplier applied to every font/widget/window size at
    # startup (see style.SCALE) — for high-DPI screens (e.g. 4K). Takes
    # effect on the next launch, not live.
    "ui_scale": 1.0,
    # Color theme applied at startup (see style.THEMES / style.apply_theme).
    # Takes effect on the next launch, not live -- same as ui_scale.
    "theme": "light",
    # UI font family (see style.FONT_STACKS), independent of the color theme.
    # Takes effect on the next launch, not live -- same as ui_scale.
    "font_family": "system",
    # Optional AutoPILOT chat dock (../AutoPILOT, see B_PILOT/autopilot_bridge.py).
    # Off by default -- it's an add-on AI layer, not core B-PILOT -- toggled
    # from Configuration -> Appearance. Has no effect if AutoPILOT/ isn't
    # present or its deps aren't installed (autopilot_bridge.AVAILABLE).
    "autopilot_enabled": False,
    # Experiment Report dock (see B_PILOT/report_panel.py) -- the per-experiment
    # lab record that builds itself from the plans that run plus the notes and
    # beamline snapshots the user adds. On by default: it only ever reads
    # history that is already being written, and writes into that experiment's
    # own folder. Toggled from Python -> Experiment Report, or Configuration ->
    # Reports; visibility is persisted here the same way autopilot_enabled is.
    "report_enabled": True,
    # Optional human title for the report's first heading (e.g. "HEDM -- Ni625
    # sample B"). Blank falls back to the experiment name.
    "report_title": "",
    # Plan names kept out of the rendered report (fnmatch patterns, so
    # "cont_acq*" covers a family). Continuous acquisition is the motivating
    # case: it is a temporary-analysis tool, run constantly while aligning, and
    # it buries the actual measurements in a lab record.
    #
    # An excluded run is still reconciled and still WRITTEN to report.jsonl --
    # it comes back marked hidden, reappears under the panel's "Show hidden"
    # toggle labelled "excluded by configuration", and returns to the document
    # the moment the pattern is removed. Nothing is ever dropped from the
    # record on the strength of a config value.
    #
    # Here rather than in the four default_config.json files for the same
    # reason as report_snapshot_groups below: a workstation that already has an
    # active_config.json never sees a change to a tracked default
    # (DECISIONS 2026-09-03 (5th)).
    "report_excluded_plans": ["cont_acq"],
    # Readings offered by the report's Snapshot button, as
    # [{"name": <group>, "items": [{"label", "expr", "kind", "units", "fmt"}]}].
    # `expr` is evaluated IN THE USER'S OWN KERNEL via ConsolePanel.query_values
    # -- B-PILOT never opens an EPICS channel itself. `kind` (float/int/str/
    # bool/raw) wraps the expression so its repr survives the ast.literal_eval
    # the reply is decoded with; without it, anything that isn't already a
    # Python literal comes back as None. See snapshot_dialog.py.
    #
    # The shipped default is deliberately INSTRUMENT-AGNOSTIC -- RE exists in
    # every profile (MPE and BITS alike) whereas device names do not, and a
    # fabricated device name reads as an empty value rather than an error.
    # Real device readings are added per site from Configuration -> Reports,
    # whose "Add from device catalog" button offers only names that actually
    # exist in that profile. Living in DEFAULTS rather than in each profile's
    # default_config.json is what makes it reach a workstation that already
    # has an active_config.json (see DECISIONS 2026-09-03 (5th)).
    "report_snapshot_groups": [
        {
            "name": "RunEngine",
            "items": [
                {"label": "RE state", "expr": "RE.state", "kind": "str", "units": ""},
                {"label": "scan_id", "expr": "RE.md.get('scan_id')", "kind": "raw", "units": ""},
            ],
        }
    ],
    # Mirror the report to a remote read-only web viewer, so collaborators who
    # are not at the beamline can follow it live (see B_PILOT/report_sync.py
    # and the report_server/ service in this repo). Outbound only: the
    # workstation opens no port and accepts nothing from the network.
    #
    # THESE TWO KEYS DO NOT ARM THE FEATURE ON THEIR OWN, and that is
    # deliberate. Sync also requires BPILOT_REPORT_SYNC_TOKEN in the
    # environment, which is not a config key and never will be. active_config.
    # json is committed and the beamline runs on shared accounts, so a flag
    # alone would start pushing from every checkout of this profile --
    # a colleague's workstation, a dev laptop -- to a service nobody there
    # chose. Requiring something that lives only in the environment makes the
    # committed flag harmless wherever it wasn't intended. Same reasoning, and
    # the same ~/.bashrc line, as ARGO_API_KEY (see .context/DEPLOY.md).
    #
    # Even fully armed this publishes nothing until the user shares a specific
    # experiment from the Report panel; sharing is per experiment, never
    # per profile.
    "report_sync_enabled": False,
    # Where the mirror publishes to: "http" (the report_server/ service in this
    # repo), "gdocs" (a Google Doc shared read-only by link, see
    # B_PILOT/report_gdocs.py), or "outbox" (write to a shared folder and let
    # the report_relay/ daemon on an internet-connected machine publish it --
    # for a workstation with no route out). The HTTP service is the only genuinely *live*
    # target and the only one where hiding an entry takes its figures offline
    # instantly; Google Docs needs no hosting at all, which is the whole reason
    # it exists. An unavailable backend (the Google client libraries are not in
    # the pinned beamline environment) falls back to "http" rather than
    # erroring, so a profile naming it stays harmless on a machine without it.
    "report_sync_backend": "http",
    # Base URL of the viewer service, e.g. "https://reports.inside.anl.gov".
    # A beamline fact like qs_zmq_control_addr, so it belongs in the committed
    # profile -- unlike the push token, which never does. Unused by "gdocs".
    "report_sync_url": "",
    # Optional Drive folder id to create report documents in ("gdocs" only).
    # Blank means the account's My Drive root. Not a secret -- a folder id is
    # useless without access to the folder -- so the profile is the right home.
    "report_gdocs_folder_id": "",
    # Longest the remote copy may lag the local record, in seconds. One number
    # does for the whole debounce: it is the ceiling that stops a plan
    # streaming output into history.jsonl from starving the push, and the
    # quiet period that coalesces a burst of edits is derived from it.
    "report_sync_interval_s": 5,
    # Auto-start MIDAS_GUI's live view when a Run/Queue dispatch involves an
    # area_detector device (see B_PILOT/midas_bridge.py). On by default -- a
    # no-op if MIDAS_GUI isn't running; never auto-launches it. Toggled from
    # the toolbar's "Bridge Live-View" checkbox or Configuration -> Data Viewer.
    "midas_bridge_enabled": True,
    # Devices (see device_discovery.py / device_source.py):
    "device_search_paths": [],   # directories scanned for __all__-exported devices
    # {category: {device_name: shown_bool}}; unseen names default shown.
    "device_selection": {},
    # {device_name: category}. Manual per-profile override of the category
    # device_discovery.scan() infers from filename/class-name — lets a user fix
    # a wrong/awkward grouping without touching the discovery heuristics
    # (which stay beamline-agnostic and unchanged). Applied on top of the
    # discovered category everywhere one is used (device_source.get_catalog()
    # and the Configuration dialog's Devices tab).
    "device_category_overrides": {},
    # Files scanned by scan_building_discovery.scan() for scan_skeletons.py's
    # plan_opener/per_step/plan_closer (common to every beamline) and
    # suspenders/pseudo_suspenders (common + one <bl>_suspenders.py). Same
    # static-analysis, never-import guarantee as device_search_paths.
    "plan_building_search_paths": [],
    "suspender_search_paths": [],
    # Files scanned by switchto_popup.discover_shortcuts() for that beamline's
    # switch_to_* shortcut plans (instrument/plans/<bl>_plans/<bl>_shortcuts.py).
    # Same static-analysis, never-import guarantee as device_search_paths.
    "switch_to_search_paths": [],
    # {category: [name, ...]} for plan_opener/per_step/plan_closer/suspender/
    # pseudo_suspender, as of the last Discover click. Unlike device_selection
    # this is NOT rescanned live on every use — these building blocks change
    # rarely, so the catalog itself is committed (like device_selection) and
    # only refreshed via Configuration -> Scan blocks -> Discover, then Save.
    "plan_building_blocks": {},
    # Data viewer (B_PILOT/viewer.py). `databroker_catalog` is a NAME registered
    # in ~/.local/share/intake/*.yml — never a credentialed connection string.
    # Empty means "auto-detect from instrument/iconfig.yml by account", the
    # viewer's original zero-config behavior.
    "databroker_catalog": "",
    # Optional Tiled (or other) URI override — NOT for a credentialed
    # mongodb://user:pass@host URI: profiles are meant to be committed to git
    # and shared between beamline staff, so secrets don't belong here. The
    # MongoDB URIs in iconfig.yml stay where they are, resolved locally per
    # account via the pre-registered intake catalog files.
    "databroker_uri": "",
    "databroker_nexus_dir": "",  # optional folder holding raw NeXus files
    # Which plan-queue backend "Add to Queue"/the queue panel use: "native"
    # (default, B_PILOT.queue_store's own persistent per-beamline queue,
    # driven by queue_runner.py) or "qs" (the Bluesky queueserver, see
    # B_PILOT/qs_client.py). Restart required to take effect (same pattern
    # as bluesky_root/ui_scale). Selecting "native" makes zero connection
    # attempts toward a queue server -- qs_client's background thread is
    # never even created.
    "queue_backend": "native",
    # Bluesky queueserver (QS) connection for the QS-backed plan queue (see
    # B_PILOT/qs_client.py). Beamline facts, like databroker_catalog above --
    # not a credentialed connection string, so safe to commit to a profile.
    # QS's own start-re-manager (mpe_bluesky/qserver/qserver.sh) is set with
    # no explicit --zmq-control-addr/--zmq-info-addr, so it binds the
    # library defaults (ports 60615/60625) on whatever host it runs on --
    # redwood today. Only used when queue_backend == "qs".
    "qs_zmq_control_addr": "tcp://redwood.xray.aps.anl.gov:60615",
    "qs_zmq_info_addr": "tcp://redwood.xray.aps.anl.gov:60625",
    "qs_user": "",  # blank -> getpass.getuser() at connect time
    "qs_user_group": "primary",  # matches user_group_permissions.yaml's "primary:" group
    # Which sections the viewer's "Export run…" writes out (Data Viewer's own
    # "Export settings…" dialog). data_preview defaults off — it can be large
    # and, unlike the others, isn't already on screen unless the user asked
    # for a preview.
    "viewer_export_fields": {
        "summary": True,
        "start_metadata": True,
        "stop_metadata": True,
        "notes": True,
        "file_references": True,
        "data_preview": False,
    },
}

# Keys that stay diff-only (omitted from a saved profile unless overridden),
# even though every other key is written out in full. Three kinds: paths
# derived from *this* GUI's own location (B_PILOT/paths.py), an explicit
# override of that same location (bluesky_root), and pure runtime state that
# isn't really a "setting" at all -- all workstation-specific, so baking any
# of them into a profile would break portability to another workstation.
_WORKSTATION_KEYS = {
    "bluesky_root",
    "kernel_work_dir",
    "plans_dir",
    "import_root",
    "embedded_starter_script",
    "last_kernel_connection_file",
}

# Keys that are DERIVED, not configured: forced to their computed value by
# :func:`_apply_pinned` on every load and dropped from every file we write, so
# a legacy override still on disk is ignored rather than obeyed. See the module
# docstring for why these two stopped being ordinary settings.
_PINNED_KEYS = ("beamline", "session_dir")

# Pure runtime state that must never reach a profile's *shared baseline*
# (``default_config.json``), which is committed to git and handed to another
# workstation. `_WORKSTATION_KEYS` alone does not cover this: `_as_overrides`
# drops one of those only when it still equals the computed default, so a real
# value always survives -- which is how one workstation's absolute
# ``~/.bluesky_pilot/20ide/kernel.json`` came to be committed in the s20ide
# profile. Narrow on purpose: s1id's committed ``bluesky_root``/``plans_dir``/
# ``import_root`` under /home/beams12/S1IDUSER are deliberate and must keep
# round-tripping through "Save as default".
_NEVER_IN_DEFAULTS = {"last_kernel_connection_file"}


def _apply_pinned(merged: dict, name: str) -> dict:
    """Force the derived :data:`_PINNED_KEYS` onto an effective-config dict.

    ``name`` is the profile folder name, which *is* the beamline identity.
    """
    merged["beamline"] = name
    merged["session_dir"] = _paths.SESSION_DIR_DEFAULT
    return merged

_cache: dict | None = None
_active_profile: str | None = None


def _profile_dir(name: str) -> str:
    return os.path.join(PROFILES_DIR, name)


def _default_path(name: str) -> str:
    return os.path.join(_profile_dir(name), "default_config.json")


def _active_path(name: str) -> str:
    return os.path.join(_profile_dir(name), "active_config.json")


def list_profiles() -> list[str]:
    """Beamline profile names — subfolders of :data:`PROFILES_DIR` that have
    a ``default_config.json`` (the source of truth for "this beamline
    exists"; ``active_config.json`` is bootstrapped lazily, see
    :func:`_ensure_active`)."""
    try:
        return sorted(
            name
            for name in os.listdir(PROFILES_DIR)
            if os.path.isfile(_default_path(name))
        )
    except OSError:
        return []


def _ensure_active(name: str) -> None:
    """Bootstrap ``active_config.json`` from ``default_config.json`` if the
    former doesn't exist yet (fresh checkout / new workstation)."""
    if not os.path.isfile(_active_path(name)):
        _write_json(_active_path(name), _read_json(_default_path(name)))


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001  (malformed json etc. — fall back to defaults)
        return {}


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _migrate_flat_profiles_if_needed() -> None:
    """Upgrade pre-split flat profile files (``profiles/<name>.json``) into
    ``profiles/<name>/default_config.json`` — the current-tree layout as of
    2026-07-21, before default/active were split. ``active_config.json`` is
    left to :func:`_ensure_active` to bootstrap on first read."""
    try:
        flat_files = [fn for fn in os.listdir(PROFILES_DIR) if fn.endswith(".json")
                      and os.path.isfile(os.path.join(PROFILES_DIR, fn))]
    except OSError:
        return
    for fn in flat_files:
        name = fn[:-5]
        old_path = os.path.join(PROFILES_DIR, fn)
        overrides = _read_json(old_path)
        _write_json(_default_path(name), overrides)
        os.remove(old_path)


def _migrate_legacy_if_needed() -> None:
    """Upgrade a pre-profile ``gui_config.json`` (flat overrides, no
    ``active_profile`` key) into the first profile instead of losing it."""
    pointer = _read_json(CONFIG_PATH)
    if "active_profile" in pointer or list_profiles():
        return
    overrides = {k: v for k, v in pointer.items() if k in DEFAULTS}
    name = str(overrides.get("beamline") or "default")
    _write_json(_default_path(name), overrides)
    _write_json(CONFIG_PATH, {"active_profile": name})


def _migrate_bluesky_root_key(raw: dict) -> dict:
    """Back-compat for profiles saved before the ``project_root`` ->
    ``bluesky_root`` rename (2026-08-14): honor the old key if the new one
    isn't set. Read-only -- the file rewrites cleanly under the new key on
    the next :func:`save` since :func:`_as_overrides` only ever emits
    :data:`DEFAULTS` keys."""
    if not raw.get("bluesky_root") and raw.get("project_root"):
        raw = dict(raw)
        raw["bluesky_root"] = raw["project_root"]
    return raw


def _ensure_active_profile() -> str:
    _migrate_flat_profiles_if_needed()
    _migrate_legacy_if_needed()
    if not list_profiles():
        _write_json(_default_path("default"), {})
    available = list_profiles()
    pointer = _read_json(CONFIG_PATH)
    name = pointer.get("active_profile")
    if name not in available:
        name = available[0]
        _write_json(CONFIG_PATH, {"active_profile": name})
    return name


def active_profile() -> str:
    """The currently active profile name (seeds one on first run)."""
    global _active_profile
    if _active_profile is None:
        _active_profile = _ensure_active_profile()
    return _active_profile


def set_active_profile(name: str) -> None:
    """Make `name` the active profile; must already exist."""
    global _active_profile, _cache
    if name not in list_profiles():
        raise ValueError(f"Unknown profile: {name!r}")
    _active_profile = name
    _cache = None
    _write_json(CONFIG_PATH, {"active_profile": name})


def _as_overrides(cfg: dict) -> dict:
    """Full-effective-config -> the dict actually written to a profile file.

    Every key is kept as-is except :data:`_PINNED_KEYS`, which are dropped
    unconditionally (they are derived, so persisting them is what let them
    drift), and :data:`_WORKSTATION_KEYS`, which are dropped when they still
    match the computed default (see module docstring).
    """
    return {
        k: v
        for k, v in cfg.items()
        if k in DEFAULTS
        and k not in _PINNED_KEYS
        and (k not in _WORKSTATION_KEYS or v != DEFAULTS[k])
    }


def new_profile(name: str, clone_from: str | None = None) -> None:
    """Create profile `name` (self-documenting defaults, or a clone of
    `clone_from`'s default baseline). Its default and active files start
    identical."""
    if not name or name in list_profiles():
        raise ValueError(f"Invalid or already-existing profile name: {name!r}")
    if clone_from:
        # Strip the derived keys: a clone must take the NEW folder's identity,
        # never inherit the source profile's beamline/session_dir.
        overrides = {k: v for k, v in _read_json(_default_path(clone_from)).items()
                     if k not in _PINNED_KEYS}
    else:
        overrides = _as_overrides(dict(DEFAULTS))
    _write_json(_default_path(name), overrides)
    _write_json(_active_path(name), overrides)


def save_profile_as(name: str, values: dict) -> None:
    """Write `values` (merged over DEFAULTS) as a new profile `name`,
    self-documenting. Its default and active files start identical."""
    if not name:
        raise ValueError("Profile name required")
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in values.items() if k in DEFAULTS})
    overrides = _as_overrides(merged)
    _write_json(_default_path(name), overrides)
    _write_json(_active_path(name), overrides)


def save_as_default(name: str, values: dict) -> None:
    """Promote `values` (merged over DEFAULTS) to profile `name`'s shared
    ``default_config.json`` only — does not touch ``active_config.json``."""
    if not name:
        raise ValueError("Profile name required")
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in values.items() if k in DEFAULTS})
    overrides = {k: v for k, v in _as_overrides(merged).items()
                 if k not in _NEVER_IN_DEFAULTS}
    _write_json(_default_path(name), overrides)


def delete_profile(name: str) -> None:
    """Delete profile `name`; refuses to delete the last remaining profile."""
    global _active_profile, _cache
    available = list_profiles()
    if name not in available:
        return
    if len(available) <= 1:
        raise ValueError("Cannot delete the last remaining profile")
    shutil.rmtree(_profile_dir(name), ignore_errors=True)
    if _active_profile == name:
        _active_profile = None
        _cache = None


def profile_values(name: str) -> dict:
    """Effective *live* config for profile `name` (defaults + its active
    overrides), without activating it. Bootstraps ``active_config.json``
    from ``default_config.json`` on first access."""
    _ensure_active(name)
    merged = dict(DEFAULTS)
    raw = _migrate_bluesky_root_key(_read_json(_active_path(name)))
    merged.update({k: v for k, v in raw.items() if k in DEFAULTS})
    return _apply_pinned(merged, name)


def default_profile_values(name: str) -> dict:
    """Effective *default* (shared baseline) config for profile `name`,
    ignoring any local ``active_config.json`` overrides."""
    merged = dict(DEFAULTS)
    raw = _migrate_bluesky_root_key(_read_json(_default_path(name)))
    merged.update({k: v for k, v in raw.items() if k in DEFAULTS})
    return _apply_pinned(merged, name)


def as_dict() -> dict:
    """Return the effective config (defaults merged with the active profile)."""
    global _cache
    if _cache is None:
        _cache = profile_values(active_profile())
    return dict(_cache)


def get(key: str):
    """Return one effective config value."""
    return as_dict().get(key, DEFAULTS.get(key))


def update(values: dict) -> None:
    """Merge `values` (known keys only) into the active profile and persist.

    :data:`_PINNED_KEYS` are ignored: they are derived, and letting a caller
    push one into the in-memory cache would desync it from what `save()`
    writes (which drops them) until the next reload.
    """
    global _cache
    cfg = as_dict()
    for k, v in values.items():
        if k in DEFAULTS and k not in _PINNED_KEYS:
            cfg[k] = v
    _cache = cfg
    save()


def save() -> None:
    """Write the active profile's ``active_config.json`` to disk,
    self-documenting (best effort).

    Every setting is written out in full, even where it matches the built-in
    default — except :data:`_PINNED_KEYS` (derived, never written) and
    :data:`_WORKSTATION_KEYS`, which stay diff-only so a profile committed to
    git doesn't bake in one workstation's absolute paths (see module
    docstring).
    """
    try:
        _write_json(_active_path(active_profile()), _as_overrides(as_dict()))
    except Exception:  # noqa: BLE001
        pass
