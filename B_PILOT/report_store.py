"""Storage for the per-experiment **experiment report** -- B-PILOT's lab record.

A report is scoped to an *experiment*, exactly like :mod:`experiment_history`,
and lives in that same directory so the whole record of a beamtime travels as
one folder::

    <session_dir>/<beamline>/experiments/<safe-name>/report_events.jsonl
    <session_dir>/<beamline>/experiments/<safe-name>/report.md

**Two files, one artifact.** ``report.md`` is the thing a human reads, keeps,
and hands to a collaborator -- but it is *generated*, never appended to. Only
``report_events.jsonl`` is authored:

* ``report_events.jsonl`` holds the items a person adds -- notes, beamline
  snapshots, headings, agent-written blocks. Append-only.
* ``report.md`` is rewritten from scratch by :mod:`report_builder`, merging
  those events with the plan runs it derives from ``history.jsonl``.

The split exists because a run's *outcome* is not known when it starts, and
because ``history.jsonl`` is the only record that also captures runs dispatched
by the detached queue runner while the GUI was closed. Deriving runs and
merging by timestamp keeps the report complete and regenerable; keeping manual
items in their own append-only log means nothing a person typed can ever be
lost to a regeneration.

Storage conventions are inherited deliberately, not reinvented:

* Events are appended with a **single** ``write()`` of one JSON line, well
  under ``PIPE_BUF``, so concurrent appenders cannot interleave -- the same
  lock-free reasoning as :func:`experiment_history.append_entry`, rather than
  the ``flock`` pattern used by the mutable stores (``queue_store``,
  ``det_startup_state``).
* ``report.md`` is a whole-file rewrite, so it uses ``tmp`` + :func:`os.replace`
  -- the atomic-write pattern from :func:`config._write_json`.
* Every write is best-effort (``try/except OSError: pass``). Failing to record
  a note must never take down a run in progress.
"""
from __future__ import annotations

import json
import os
import time

from . import experiment_history as eh

# Event kinds written to report_events.jsonl. Runs are NOT here -- they are
# derived from history.jsonl by report_builder, so that queue-dispatched runs
# and runs from a previous GUI session are picked up just the same.
NOTE = "note"            # free prose the user typed
SNAPSHOT = "snapshot"    # a captured table of live device values
HEADING = "heading"      # a user-inserted section break
AGENT = "agent"          # a block a person accepted from AutoPILOT

EVENTS_FILENAME = "report_events.jsonl"
MARKDOWN_FILENAME = "report.md"


def events_path(beamline: str, experiment: str) -> str:
    """Path to the append-only manual-event log for one experiment."""
    return os.path.join(eh.experiment_dir(beamline, experiment), EVENTS_FILENAME)


def markdown_path(beamline: str, experiment: str) -> str:
    """Path to the generated report document for one experiment."""
    return os.path.join(eh.experiment_dir(beamline, experiment), MARKDOWN_FILENAME)


def append_event(
    beamline: str,
    experiment: str,
    kind: str,
    *,
    text: str = "",
    title: str = "",
    rows: list | None = None,
    ts: float | None = None,
) -> dict | None:
    """Append one manual report event; return it, or ``None`` if it was empty.

    `rows` carries a snapshot's captured values as ``[[label, value, units],
    ...]`` -- a list of lists rather than tuples because that is what survives
    a JSON round trip unchanged.
    """
    if not (text or title or rows):
        return None
    event = {
        "ts": ts if ts is not None else time.time(),
        "kind": kind,
        "title": title,
        "text": text,
        "rows": rows or [],
    }
    # Reuse the history store's meta.json bootstrap so a report started before
    # any kernel activity still records the experiment's real display name.
    eh._ensure_meta(beamline, experiment)  # noqa: SLF001
    try:
        with open(events_path(beamline, experiment), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError:
        return None
    return event


def read_events(beamline: str, experiment: str) -> list[dict]:
    """All manual events for one experiment, oldest first.

    Malformed lines (a write torn by a crash) are skipped rather than aborting
    the read -- same tolerance as :func:`experiment_history.read_entries`.
    """
    events: list[dict] = []
    try:
        with open(events_path(beamline, experiment), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return events


def write_markdown(beamline: str, experiment: str, markdown: str) -> bool:
    """Atomically (re)write the generated report document. True if it landed."""
    path = markdown_path(beamline, experiment)
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def read_markdown(beamline: str, experiment: str) -> str:
    """The generated report document, or ``""`` if it has not been built yet."""
    try:
        with open(markdown_path(beamline, experiment), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def source_sizes(beamline: str, experiment: str) -> tuple[int, float]:
    """``(history bytes, events mtime)`` -- the cheap change-detection tuple.

    The report panel polls this instead of re-reading and re-rendering on every
    tick; a rebuild is only worth doing when one of the two inputs actually
    moved. Missing files read as ``0``, so a report with no activity yet is a
    stable, non-changing value rather than a repeated rebuild.
    """
    try:
        hist = os.path.getsize(eh.history_path(beamline, experiment))
    except OSError:
        hist = 0
    try:
        mtime = os.path.getmtime(events_path(beamline, experiment))
    except OSError:
        mtime = 0.0
    return hist, mtime
