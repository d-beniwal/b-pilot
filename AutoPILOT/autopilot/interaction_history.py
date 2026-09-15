"""Persistent, per-beamline record of every AutoPILOT chat interaction.

Mirrors ``B_PILOT/experiment_history.py``'s storage pattern (same
``session_dir``/beamline nesting, append-only JSON-Lines, single ``write()``
call per line, ``try/except OSError: pass`` on every write) but keyed by
**beamline + calendar day** rather than by experiment -- a chat conversation
isn't necessarily tied to a running kernel/experiment, and a per-day file
keeps individual files bounded in size while still segregating cleanly by
beamline at the directory level, since B-PILOT runs standalone on separate
machines at separate beamlines and this data is never synced between them
(that's a deliberately separate, later piece of work).

Storage::

    <session_dir>/<beamline>/autopilot/interactions/<YYYY-MM-DD>.jsonl

If the active profile sets ``experiment_data_root`` (see
``B_PILOT.config.DEFAULTS`` and ``B_PILOT.experiment_history``'s module
docstring), chats are filed per **experiment** instead, alongside the report
they can contribute to::

    <experiment_data_root>/<safe-name>/b_pilot/autopilot/<YYYY-MM-DD>.jsonl

still day-bucketed, still segregated by beamline (via the experiment's own
folder), just no longer able to mix every experiment's chats into one
beamline-wide directory.

Two entry kinds, one JSON object per line:

- ``"turn"`` -- one full :func:`autopilot.pipeline.converse` call: the
  request, the final reply, which tool(s) fired, the drafted plan spec (raw
  and validated), and token usage. Written by :func:`record_turn`.
- ``"outcome"`` -- a later human action tied back to a turn's ``turn_id``
  (e.g. ``action="opened_in_form"``) -- the clearest available implicit
  signal for whether a proposed plan was actually good. Written by
  :func:`record_outcome`.

``conversation_id`` groups every turn (and outcome) of one chat-dock session
together; ``turn_id`` identifies one turn precisely so an outcome can be
correlated back to the turn that produced it.

Every string value is passed through :func:`autopilot.tools.redact` (the
same credential-URL redaction already applied to ``search_codebase``/
``read_source_file`` results) before it reaches disk -- defense-in-depth in
case a user pastes something sensitive into chat.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
import uuid

from . import tools
from ._bpilot_path import ensure_bpilot_on_path

ensure_bpilot_on_path()

from B_PILOT import config as bpilot_config  # noqa: E402
from B_PILOT import experiment_history as bpilot_experiment_history  # noqa: E402


def _safe_name(beamline: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", (beamline or "").strip()).strip("_")
    return safe or "unknown"


def _beamline_dir(beamline: str) -> str:
    """Old-layout location: ``<session_dir>/<beamline>/autopilot/interactions``."""
    return os.path.join(
        os.path.expanduser(bpilot_config.get("session_dir")), _safe_name(beamline), "autopilot", "interactions"
    )


def _new_root() -> str:
    """See :func:`experiment_history._new_root` -- same key, same meaning."""
    root = (bpilot_config.get("experiment_data_root") or "").strip()
    return os.path.expanduser(root) if root else ""


def _interactions_dir(beamline: str, experiment: str | None) -> str:
    """Where this beamline's (and, under the new layout, this experiment's)
    interaction logs live.

    Mirrors :func:`experiment_history.experiment_dir`'s branch, **including
    its safety rule**: this never creates the top-level
    ``<experiment_data_root>/<name>`` folder either (that's DM's, not
    B-PILOT's) -- it only routes into the new per-experiment layout if that
    folder already exists, reusing that module's ``_safe_name``/unknown-
    experiment fallback (same reach-into-a-private-helper pattern
    :mod:`report_store` already uses). Anything else -- no root configured,
    the DM folder doesn't exist yet, or no active experiment -- falls back to
    the old per-beamline location, so `_append`'s own ``os.makedirs`` below
    only ever creates the ``autopilot/`` subtree inside an experiment folder
    that was already there.
    """
    root = _new_root()
    if root:
        safe = bpilot_experiment_history._safe_name(experiment)  # noqa: SLF001
        top = os.path.join(root, safe)
        if os.path.isdir(top):
            return os.path.join(top, "b_pilot", "autopilot")
    return _beamline_dir(beamline)


def _day_path(beamline: str, experiment: str | None, ts: float) -> str:
    day = time.strftime("%Y-%m-%d", time.localtime(ts))
    return os.path.join(_interactions_dir(beamline, experiment), f"{day}.jsonl")


def new_conversation_id() -> str:
    """A short random id grouping every turn/outcome of one chat-dock session."""
    return uuid.uuid4().hex[:12]


def _redact_value(value):
    """Recursively apply `tools.redact` to every string in `value` (a
    dict/list/str/anything), leaving non-string leaves (numbers, bools,
    None) untouched."""
    if isinstance(value, str):
        return tools.redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _append(beamline: str, experiment: str | None, entry: dict) -> None:
    """Append one JSON line. Never raises -- a logging failure must never
    break a real chat turn (same convention as
    `experiment_history.append_entry`)."""
    path = _day_path(beamline, experiment, entry["ts"])
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def record_turn(
    beamline: str,
    *,
    conversation_id: str,
    turn_id: str,
    profile: str | None,
    experiment: str | None,
    request: str,
    result,  # pipeline.PlanResult -- typed loosely to avoid a pipeline<->interaction_history import cycle
) -> None:
    """Append one `"turn"` entry for a completed `pipeline.converse()` call."""
    entry = {
        "ts": time.time(),
        "kind": "turn",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "beamline": beamline,
        "profile": profile,
        "experiment": experiment or None,
        "request": tools.redact(request or ""),
        "ok": result.ok,
        "message": tools.redact(result.message or ""),
        "template_key": result.template_key,
        "tool_name": result.tool_name,
        "tool_calls": result.tool_calls,
        "raw_spec": _redact_value(result.raw_spec),
        "clean_spec": _redact_value(result.clean_spec),
        "errors": result.errors,
        "filepath": result.filepath,
        "gui_command": tools.redact(result.gui_command) if result.gui_command else None,
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_read_input_tokens": result.cache_read_input_tokens,
    }
    _append(beamline, experiment, entry)


def record_outcome(
    beamline: str,
    *,
    conversation_id: str,
    turn_id: str | None,
    action: str,
    detail: str | None = None,
    experiment: str | None = None,
) -> None:
    """Append one `"outcome"` entry -- a human action taken on a prior turn.

    `experiment` should be the same one the originating turn was recorded
    under (callers already have it at hand -- the active profile's
    ``dm_experiment``) so the outcome lands in the same per-experiment file
    under the new layout rather than an unrelated "unknown experiment" bucket.
    """
    entry = {
        "ts": time.time(),
        "kind": "outcome",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "beamline": beamline,
        "action": action,
        "detail": detail,
    }
    _append(beamline, experiment, entry)


def list_days(beamline: str, experiment: str | None = None) -> list[str]:
    """Available `YYYY-MM-DD` days with recorded interactions for `beamline`
    (and, under the new per-experiment layout, `experiment`), most recent
    first. With `experiment_data_root` set, this only sees one experiment's
    days -- there is no longer one flat per-beamline directory to browse."""
    days = [
        os.path.splitext(os.path.basename(p))[0]
        for p in glob.glob(os.path.join(_interactions_dir(beamline, experiment), "*.jsonl"))
    ]
    return sorted(days, reverse=True)


def read_day(beamline: str, day: str, experiment: str | None = None) -> list[dict]:
    """All entries recorded for `beamline` (and `experiment`, see
    :func:`list_days`) on `day` (`YYYY-MM-DD`), in file (oldest-first) order.
    Malformed lines are skipped rather than aborting the whole read."""
    entries: list[dict] = []
    path = os.path.join(_interactions_dir(beamline, experiment), f"{day}.jsonl")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
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
