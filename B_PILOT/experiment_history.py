"""Persistent, per-experiment record of everything that reaches the IPython kernel.

Unlike the kernel itself (one per **beamline**, see :mod:`kernel_session`),
this history is keyed by **experiment name** and never deleted when a kernel
stops -- launching or attaching under the same experiment name again just
keeps appending to the same record, across any number of kernel restarts.

Storage: one append-only JSON-Lines file per experiment, nested under the
beamline the same way :func:`kernel_session.paths` already nests everything::

    <session_dir>/<beamline>/experiments/<safe-name>/history.jsonl
    <session_dir>/<beamline>/experiments/<safe-name>/meta.json

If the active profile sets ``experiment_data_root`` (see
:data:`config.DEFAULTS`), these files move alongside the beamline's real
experiment data instead, next to the report they feed (see
:mod:`report_store`)::

    <experiment_data_root>/<safe-name>/b_pilot/report/history.jsonl
    <experiment_data_root>/<safe-name>/b_pilot/report/meta.json

Purely profile-level state -- the kernel connection file, queue, session
sidecar -- is unaffected and stays under ``session_dir`` either way (see
:mod:`kernel_session`). :func:`experiment_dir` is the single place this
branch happens; every other function here builds on it.

Each line is one timestamped entry: ``{"ts": <epoch float>, "kind": "input" |
"stream" | "result" | "display" | "error" | "marker", "text": "..."}``. The
file is physically oldest-line-first (a plain append log -- crash-safe, no
locking needed since every writer does a single ``write()`` call per line,
well under ``PIPE_BUF``), but every reader in this module returns/renders
entries **newest first**, the same way a Mongo/tiled catalog query sorted by
``-1`` presents its most recent documents at the top without needing the
underlying collection stored in that order.

``kind="input"`` entries hold the exact code string sent to the kernel (e.g.
an ``RE(plan(...))`` call) -- the same text :meth:`console_panel.ConsolePanel.
run_code` sends -- so they round-trip straight through
:meth:`plan_runner.PlanRunnerPanel.load_from_command` with no new parsing.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time

from . import config

# Fallback bucket for the rare case of attaching to a kernel B-PILOT didn't
# itself launch, where the real experiment name isn't known synchronously
# (see console_panel._resolve_attach_experiment). Nothing is lost -- it just
# isn't filed under the real name for that stretch of activity.
UNKNOWN_EXPERIMENT = "(unknown experiment)"

# Plan name inside an ``RE(<plan>(...))`` command -- independent small copies
# of this same regex already exist in plan_runner._RE_PLAN and
# queue_store._RE_PLAN; kept consistent with that established pattern rather
# than introducing a cross-module import for one line.
_RE_PLAN = re.compile(r"\bRE\(\s*([A-Za-z_]\w*)\s*\(")

_META_NAME_KEY = "name"


def _beamline_dir(beamline: str) -> str:
    return os.path.join(os.path.expanduser(config.get("session_dir")), beamline, "experiments")


def _new_root() -> str:
    """Absolute ``experiment_data_root`` for the active profile, or ``""``.

    Blank (the default) means "no override" -- every path in this module
    keeps its historical ``session_dir``-nested location. See
    :data:`config.DEFAULTS`'s ``experiment_data_root`` entry.
    """
    root = (config.get("experiment_data_root") or "").strip()
    return os.path.expanduser(root) if root else ""


def _safe_name(experiment: str) -> str:
    """Filesystem-safe directory name for an experiment (never empty)."""
    name = (experiment or "").strip() or UNKNOWN_EXPERIMENT
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")
    return safe or "unnamed"


#: Path from an ``experiment_data_root``-relative experiment folder down to
#: where this module's files (and report_store's/report_images') live --
#: ``report_store.py``'s docstring already treats history.jsonl/meta.json as
#: "sitting next to" report.jsonl, so all of it moves together.
_NEW_LAYOUT_SUBDIR = os.path.join("b_pilot", "report")


def experiment_dir(beamline: str, experiment: str) -> str:
    """Where this experiment's history/report/figures live.

    **B-PILOT never creates the top-level ``<experiment_data_root>/<name>``
    folder.** That folder is owned by the beamline's data-management
    workflow (DM), not by B-PILOT -- it is the one thing this function
    refuses to bring into existence. So the branch below only routes into
    the new layout if that folder is *already there*; every caller here
    (`_ensure_meta`, `report_store.append_event`, `report_images.store_image`,
    ...) then only ever ``os.makedirs()``\\ s the ``b_pilot/...`` subtree
    *inside* an existing experiment folder, never the experiment folder
    itself. An experiment with no matching DM folder (including a blank/
    unknown one) automatically falls back to the old, B-PILOT-owned location
    below -- exactly where a user-confirmed "create this as a local/
    temporary experiment" (see `launch_dialog.LaunchDialog`) ends up, with no
    extra plumbing needed.

    Checked fresh on every call rather than cached: if DM creates the real
    folder mid-experiment, later entries switch to the new layout while
    earlier ones stay where they were written -- a known, accepted split
    (matches this project's "no migration" stance elsewhere in this module).
    """
    root = _new_root()
    if root:
        top = os.path.join(root, _safe_name(experiment))
        if os.path.isdir(top):
            return os.path.join(top, _NEW_LAYOUT_SUBDIR)
    return os.path.join(_beamline_dir(beamline), _safe_name(experiment))


def history_path(beamline: str, experiment: str) -> str:
    return os.path.join(experiment_dir(beamline, experiment), "history.jsonl")


def _meta_path(beamline: str, experiment: str) -> str:
    return os.path.join(experiment_dir(beamline, experiment), "meta.json")


def _ensure_meta(beamline: str, experiment: str) -> None:
    """Record the original (possibly not filesystem-safe) display name once."""
    d = experiment_dir(beamline, experiment)
    try:
        os.makedirs(d, exist_ok=True)
        mp = _meta_path(beamline, experiment)
        if not os.path.exists(mp):
            with open(mp, "w", encoding="utf-8") as fh:
                json.dump({_META_NAME_KEY: experiment or UNKNOWN_EXPERIMENT}, fh)
    except OSError:
        pass


def append_entry(beamline: str, experiment: str, kind: str, text: str, ts: float | None = None) -> None:
    """Append one timestamped entry. Never overwrites -- this is the persistent record.

    A single ``write()`` call of one JSON line is used deliberately so
    concurrent appenders (the detached recorder subprocess and the GUI
    process both write marker lines) can never interleave and corrupt a line.
    """
    if not text:
        return
    _ensure_meta(beamline, experiment)
    line = json.dumps({"ts": ts if ts is not None else time.time(), "kind": kind, "text": text})
    try:
        with open(history_path(beamline, experiment), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def read_entries(beamline: str, experiment: str) -> list[dict]:
    """All entries for one experiment, in file (oldest-first) order.

    Malformed lines (a write torn by a crash mid-line) are skipped rather
    than aborting the whole read.
    """
    entries: list[dict] = []
    try:
        with open(history_path(beamline, experiment), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return entries


def _scan_experiment_dirs(pattern: str, name_of) -> list[dict]:
    out: list[dict] = []
    for d in glob.glob(pattern):
        hp = os.path.join(d, "history.jsonl")
        if not os.path.isdir(d) or not os.path.isfile(hp):
            continue
        name = name_of(d)
        try:
            with open(os.path.join(d, "meta.json"), encoding="utf-8") as fh:
                name = json.load(fh).get(_META_NAME_KEY) or name
        except (OSError, ValueError):
            pass
        try:
            mtime = os.path.getmtime(hp)
        except OSError:
            mtime = 0.0
        out.append({"name": name, "path": hp, "last_activity": mtime})
    return out


def list_experiments(beamline: str) -> list[dict]:
    """Known experiments for `beamline`: ``{"name", "path", "last_activity"}``,
    most-recently-active first.

    With ``experiment_data_root`` set, this merges **both** locations
    `experiment_dir` can resolve to: DM-backed experiments under the new
    root, and local/temporary ones that fell back to the old
    ``session_dir``-nested location (see `experiment_dir`'s docstring) --
    otherwise a temporary experiment would silently vanish from this list
    the moment the new root was configured.
    """
    root = _new_root()
    out = _scan_experiment_dirs(
        os.path.join(_beamline_dir(beamline), "*"), os.path.basename
    )
    if root:
        out += _scan_experiment_dirs(
            os.path.join(root, "*", _NEW_LAYOUT_SUBDIR),
            lambda d: os.path.basename(d[: -len(_NEW_LAYOUT_SUBDIR) - 1]),
        )
    out.sort(key=lambda e: e["last_activity"], reverse=True)
    return out


def extract_plan_runs(entries: list[dict]) -> list[dict]:
    """`entries` (any order) -> ``{"ts", "plan_name", "command"}`` for every
    ``input`` entry that looks like an ``RE(plan(...))`` call, newest first."""
    runs = []
    for e in entries:
        if e.get("kind") != "input":
            continue
        text = e.get("text") or ""
        m = _RE_PLAN.search(text)
        if not m:
            continue
        runs.append({"ts": e.get("ts"), "plan_name": m.group(1), "command": text})
    runs.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return runs


def format_entry(entry: dict) -> str:
    """Render one entry as a timestamped transcript block."""
    ts = entry.get("ts")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "?"
    kind = entry.get("kind", "")
    text = (entry.get("text") or "").rstrip("\n")
    return f"----- {stamp}  [{kind}] -----\n{text}\n"


def format_timeline(entries: list[dict]) -> str:
    """`entries` (any order) -> readable transcript text, newest block first."""
    ordered = sorted(entries, key=lambda e: e.get("ts") or 0, reverse=True)
    return "\n".join(format_entry(e) for e in ordered)
