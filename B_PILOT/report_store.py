"""The experiment report's master file -- B-PILOT's lab record for a beamtime.

**One file per experiment**, sitting next to the kernel history it is built
from, so the whole record of a beamtime travels as a single folder::

    <session_dir>/<beamline>/experiments/<safe-name>/history.jsonl   (kernel transcript)
    <session_dir>/<beamline>/experiments/<safe-name>/report.jsonl    (the report)

``report.jsonl`` is the master. Everything the report contains lives in it --
plan runs, notes, beamline snapshots, section headings, blocks accepted from
AutoPILOT -- so it is self-contained and can be rendered without consulting
anything else. It is **operated on only through B-PILOT**: it is a machine
store, not a document to hand-edit, and the readable artifact is produced on
demand by the Report panel's Export button (Markdown or HTML, saved wherever
the user chooses). Nothing generated is left lying in the experiment folder to
be mistaken for the master.

**Why append-only rather than a rewritten document.** A run's outcome is not
known when it starts, so a record has to be revisable; and the writer may be
interrupted at any moment by a kernel restart or a closed GUI. An append log
gets both: a revision is a new line that supersedes an earlier one with the
same ``ts`` (see :func:`report_builder.collect`), and a torn final line costs
one entry rather than the file. Each append is a **single** ``write()`` of one
JSON line, well under ``PIPE_BUF``, so concurrent writers cannot interleave --
the same lock-free reasoning as :func:`experiment_history.append_entry`, not
the ``flock`` pattern the mutable stores use (``queue_store``,
``det_startup_state``).

**Editing is an append, never a rewrite.** Moving a block or hiding it does not
touch the entry it acts on: it appends an :data:`EDIT` entry naming that
entry's ``id`` and the fields to override (see
:func:`report_builder.apply_edits`). So the record is lossless -- a hidden
figure is still on disk, a reordered note still carries the timestamp it was
captured at -- and the single-``write()`` durability argument above survives
unchanged, which a rewritten document would not.

Every write is best-effort (``try/except OSError: pass``). Failing to record a
note must never take down a run in progress.
"""
from __future__ import annotations

import json
import os
import time
import uuid

from . import experiment_history as eh

# Entry kinds. `run` records are reconciled in from the kernel's own
# history.jsonl by report_builder; the rest are authored by a person.
RUN = "run"              # one plan invocation, with its outcome
NOTE = "note"            # free prose
SNAPSHOT = "snapshot"    # a captured table of live device values
IMAGE = "image"          # a figure; the pixels are a sidecar file (report_images)
HEADING = "heading"      # a section break
AGENT = "agent"          # a block a person accepted from AutoPILOT
EDIT = "edit"            # an overlay on another entry: {target, pos?, hidden?}

REPORT_FILENAME = "report.jsonl"

# Pre-release name of the same file, from before runs were folded into it.
# Renamed rather than left behind so a report started on an early build of
# this branch keeps its notes instead of silently starting empty.
_LEGACY_FILENAME = "report_events.jsonl"


def report_path(beamline: str, experiment: str) -> str:
    """Path to the master report file for one experiment."""
    return os.path.join(eh.experiment_dir(beamline, experiment), REPORT_FILENAME)


def _migrate_legacy(beamline: str, experiment: str) -> None:
    legacy = os.path.join(eh.experiment_dir(beamline, experiment), _LEGACY_FILENAME)
    target = report_path(beamline, experiment)
    try:
        if os.path.isfile(legacy) and not os.path.exists(target):
            os.replace(legacy, target)
    except OSError:
        pass


def current_experiment(beamline: str) -> str:
    """The experiment a report defaults to: the most recently active one.

    Used by callers that have no handle on the live console -- AutoPILOT, whose
    only injected B-PILOT object is the plan-runner panel. Resolving it from the
    history store keeps that narrow contract intact instead of widening the
    bridge just to read a name.
    """
    known = eh.list_experiments(beamline)
    return (known[0].get("name") or "") if known else ""


def new_id() -> str:
    """A fresh entry id. Random rather than sequential: two writers appending
    at once (the GUI and the detached queue runner) must never collide, and
    nothing here can see the other's last-used number."""
    return uuid.uuid4().hex[:12]


def append_event(
    beamline: str,
    experiment: str,
    kind: str,
    *,
    ts: float | None = None,
    **fields,
) -> dict | None:
    """Append one entry to the master file; return it, or ``None`` on failure.

    `fields` are stored verbatim, so each kind carries what it needs: a
    snapshot's ``rows``, a run's ``plan_name``/``ok``/``end_ts``, a note's
    ``text``. They must be JSON-serialisable -- snapshot rows are lists of
    lists rather than tuples for exactly that reason.

    Two fields are common to every kind. ``id`` identifies the entry so an
    :data:`EDIT` can name it; one is generated unless the caller supplies it
    (:func:`report_builder.collect` does, because a run is written twice and
    both writes must share an identity). ``pos`` is the optional sort override
    that lets an entry be filed somewhere other than its own timestamp -- see
    :func:`report_builder.sort_key`.
    """
    fields.setdefault("id", new_id())
    event = {"ts": ts if ts is not None else time.time(), "kind": kind, **fields}
    # Reuse the history store's meta.json bootstrap so a report started before
    # any kernel activity still records the experiment's real display name.
    eh._ensure_meta(beamline, experiment)  # noqa: SLF001
    _migrate_legacy(beamline, experiment)
    try:
        with open(report_path(beamline, experiment), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError:
        return None
    return event


def append_edits(beamline: str, experiment: str, edits: list[dict]) -> bool:
    """Append overlay entries; ``True`` if every one was written.

    Each edit is ``{"target": <entry id>, "pos": <float>?, "hidden": <bool>?}``.
    They are written one line at a time rather than batched, keeping the
    one-entry-per-``write()`` guarantee that makes concurrent appends safe; a
    reordering that renormalises the whole report is therefore several lines,
    which is exactly what it should be.
    """
    ok = True
    for edit in edits:
        target = edit.get("target")
        if not target:
            continue
        fields = {k: v for k, v in edit.items() if k != "target"}
        if append_event(beamline, experiment, EDIT, target=target, **fields) is None:
            ok = False
    return ok


def read_events(beamline: str, experiment: str) -> list[dict]:
    """Every entry in the master file, in file (oldest-first) order.

    Superseding entries are *not* collapsed here -- that is
    :func:`report_builder.collect`'s job, since it is the one that knows a
    later ``run`` entry replaces an earlier one with the same ``ts``.

    Malformed lines (a write torn by a crash) are skipped rather than aborting
    the read -- same tolerance as :func:`experiment_history.read_entries`.
    """
    _migrate_legacy(beamline, experiment)
    events: list[dict] = []
    try:
        with open(report_path(beamline, experiment), encoding="utf-8", errors="replace") as fh:
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


def source_state(beamline: str, experiment: str) -> tuple[int, int]:
    """``(history bytes, report bytes)`` -- the cheap change-detection tuple.

    The report panel polls this instead of re-reading and re-rendering on every
    tick; work is only worth doing when one of the two files actually grew.
    Missing files read as ``0``, so an experiment with no activity yet is a
    stable value rather than a repeated rebuild.
    """
    sizes = []
    for path in (eh.history_path(beamline, experiment), report_path(beamline, experiment)):
        try:
            sizes.append(os.path.getsize(path))
        except OSError:
            sizes.append(0)
    return sizes[0], sizes[1]
