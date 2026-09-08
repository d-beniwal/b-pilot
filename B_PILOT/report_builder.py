"""Fill the experiment report's master file, and render it.

Qt-free on purpose (same reasoning as :mod:`databroker_access`): everything
here is pure functions over plain dicts, so the interesting logic -- run
boundaries, status, reconciliation, ordering -- is testable without a display,
a kernel, or a beamline.

**Runs are reconciled, not pushed.** The GUI never tells this module that a
plan ran. :func:`collect` folds runs out of the kernel's own ``history.jsonl``
and appends any it has not already recorded into ``report.jsonl``. That
indirection is the whole point: ``session_recorder`` watches the kernel's IOPub
channel rather than the GUI, so a plan dispatched by the *detached queue
runner*, by another attached client, or during a session three restarts ago is
already in that file -- and reconciling picks all of them up the next time the
report is opened, with no extra plumbing anywhere.

Once reconciled, the report is self-contained: :func:`render_markdown` reads
only ``report.jsonl``.

Two wrinkles worth knowing about. A run's *outcome* is never in the ``input``
entry that starts it -- it has to be inferred from what follows, which is what
:func:`fold_runs` does. And because that outcome arrives late, a run record
has to be revisable in an append-only file: a later entry with the same ``ts``
supersedes an earlier one, and :func:`collect` collapses them.
"""
from __future__ import annotations

import ast
import re
import time

from . import experiment_history as eh
from . import report_store as rs

# Plan name inside an ``RE(<plan>(...))`` call. Small independent copies of
# this regex already live in experiment_history, plan_runner and queue_store;
# kept consistent with that established pattern rather than adding a
# cross-module import for one line.
_RE_PLAN = re.compile(r"\bRE\(\s*([A-Za-z_]\w*)\s*\(")

# Entry kinds that belong to the run that preceded them.
_OUTPUT_KINDS = {"stream", "result", "display", "error"}

# The run-notes metadata `plan_runner._make_re_line` bakes into every command
# it composes: ``RE(plan(...), md={'notes': '...'})``. Parsing it back out of
# the recorded command is what makes notes work for *queued* runs too -- the
# note is typed when the item is enqueued but the run may not reach the kernel
# for another hour, far outside any timestamp-matching window.
_RE_MD = re.compile(r",\s*md\s*=\s*(\{.*\})\s*\)\s*$")


def _split_notes(command: str) -> tuple[str, list[str]]:
    """``(command without its md=..., notes found in it)``.

    The metadata is stripped from the displayed command because the note is
    rendered as prose directly beneath it -- showing it twice, once as a
    Python dict repr, makes the record harder to read, not more complete.

    Parsed with :func:`ast.literal_eval`, never ``eval``: this is arbitrary
    text that reached the kernel, and the report must never execute it.
    """
    match = _RE_MD.search(command or "")
    if not match:
        return command, []
    try:
        md = ast.literal_eval(match.group(1))
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return command, []
    if not isinstance(md, dict) or not md.get("notes"):
        return command, []
    stripped = command[: match.start()] + ")"
    return stripped, [str(md["notes"])]


# How long after a note was typed we still consider a matching plan run to be
# "the run that note was about". Generous, because the note is written when
# Run is clicked but the kernel may not echo `execute_input` until a queued
# cell ahead of it (e.g. an injected det_startup) has finished.
_NOTE_ATTACH_WINDOW_S = 180.0

# A note may be recorded a moment *after* its command reaches the kernel when
# the GUI is under load, so allow a small backwards slack too.
_NOTE_ATTACH_SLACK_S = 5.0


def _fmt_duration(seconds: float | None) -> str:
    """Human duration: ``48 s``, ``4 min 08 s``, ``1 h 12 min``."""
    if seconds is None or seconds < 0:
        return "?"
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min {int(seconds % 60):02d} s"
    return f"{int(seconds // 3600)} h {int((seconds % 3600) // 60):02d} min"


def _error_summary(text: str) -> str:
    """The one line of a traceback worth putting in a lab record.

    A Python traceback's *last* non-empty line is the exception and its
    message; everything above it is frames. Falls back to the first line for
    anything that is not shaped like a traceback.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def fold_runs(entries: list[dict]) -> list[dict]:
    """``history.jsonl`` entries (any order) -> one record per plan run.

    Returns, oldest first::

        {"ts", "end_ts", "plan_name", "command", "ok", "error"}

    An ``input`` entry that looks like ``RE(plan(...))`` opens a run; the
    output entries that follow belong to it until the next ``input`` or
    ``marker`` closes it. A run with any ``error`` entry is marked failed and
    keeps that error's summary line.

    ``end_ts`` is the timestamp of the last entry the run produced, so the
    duration it implies is "until the last thing it printed" rather than
    "until the kernel went idle". Those differ by however long a plan runs
    silently after its final message -- close enough for a lab record, and
    honest about it, which is why the caller labels it as approximate. A run
    that printed nothing at all has ``end_ts is None``.

    ``closed`` says whether the record is final: True once a later ``input`` or
    ``marker`` has ended the run, meaning no further output can be attributed
    to it. :func:`collect` uses it to decide when a run is worth writing again,
    which is what bounds the master file to at most two entries per run.

    Non-``RE()`` input (a mode-button ``put()``, an ad-hoc console command) is
    deliberately not a run: it would bury the plans in noise. It is still in
    the Session log tab, which is where that belongs.
    """
    ordered = sorted(entries, key=lambda e: e.get("ts") or 0.0)
    runs: list[dict] = []
    current: dict | None = None

    for entry in ordered:
        kind = entry.get("kind")
        ts = entry.get("ts") or 0.0

        if kind in ("input", "marker"):
            if current is not None:
                current["closed"] = True
            current = None
            if kind != "input":
                continue
            text = entry.get("text") or ""
            match = _RE_PLAN.search(text)
            if not match:
                continue
            command, notes = _split_notes(text.strip())
            current = {
                "ts": ts,
                "end_ts": None,
                "plan_name": match.group(1),
                "command": command,
                "ok": True,
                "error": "",
                "notes": notes,
                "closed": False,
            }
            runs.append(current)
            continue

        if current is None or kind not in _OUTPUT_KINDS:
            continue
        current["end_ts"] = ts
        if kind == "error":
            current["ok"] = False
            if not current["error"]:
                current["error"] = _error_summary(entry.get("text") or "")

    return runs


def attach_notes(runs: list[dict], events: list[dict]) -> list[dict]:
    """Move run-scoped notes onto their run; return the events left over.

    Most run notes never reach this function -- :func:`_split_notes` already
    recovered them from the command's own ``md={'notes': ...}``, which is exact
    and works however long a queued item waited. This is the fallback for the
    case that loses them: a **hand-edited** command, where ``plan_runner``
    explicitly warns "Notes NOT attached" because the user's own text replaced
    the generated call. The GUI still knows what was typed, and records it here.

    So the first thing to do is drop anything already recovered from the
    command -- otherwise a normal run shows its note twice, once under the run
    and once floating at the timestamp it was typed.

    Whatever is left over (a free note typed into the report panel, or one
    whose run never reached the kernel) is returned unchanged and renders
    standalone at its own timestamp. Notes are never dropped.
    """
    leftover: list[dict] = []
    for event in events:
        if event.get("kind") != rs.NOTE:
            leftover.append(event)
            continue
        match = _RE_PLAN.search(event.get("title") or "")
        if not match:
            leftover.append(event)
            continue
        plan = match.group(1)
        text = event.get("text") or ""
        if any(r["plan_name"] == plan and text in r["notes"] for r in runs):
            continue  # already recovered from the command itself
        note_ts = event.get("ts") or 0.0
        target = next(
            (
                r
                for r in runs
                if r["plan_name"] == plan
                and -_NOTE_ATTACH_SLACK_S
                <= (r["ts"] - note_ts)
                <= _NOTE_ATTACH_WINDOW_S
                and not r["notes"]
            ),
            None,
        )
        if target is None:
            leftover.append(event)
        else:
            target["notes"].append(text)
    return leftover


def _run_markdown(run: dict) -> str:
    """One run as a Markdown section."""
    stamp = time.strftime("%H:%M:%S", time.localtime(run["ts"]))
    out = [f"### {stamp} — {run['plan_name']}", "", "```python", run["command"], "```", ""]

    status = "✅ ok" if run["ok"] else "❌ failed"
    bits = [f"**Status:** {status}"]
    if run["end_ts"]:
        bits.append(f"**Duration:** ~{_fmt_duration(run['end_ts'] - run['ts'])}")
    out.append(" · ".join(bits))
    out.append("")

    if run["error"]:
        out.append(f"> ⚠ {run['error']}")
        out.append("")
    for note in run["notes"]:
        for line in note.splitlines() or [""]:
            out.append(f"> {line}")
        out.append("")
    return "\n".join(out)


def _event_markdown(event: dict) -> str:
    """One manual event as a Markdown section."""
    kind = event.get("kind")
    stamp = time.strftime("%H:%M:%S", time.localtime(event.get("ts") or 0.0))
    text = event.get("text") or ""
    title = event.get("title") or ""

    if kind == rs.HEADING:
        # "▸" marks this as the user's own section break, so it reads
        # distinctly from the automatic day headings at the same level.
        return f"## ▸ {title or text}\n"

    if kind == rs.SNAPSHOT:
        head = title or "Beamline snapshot"
        out = [f"#### 📸 {head} — {stamp}", "", "| Reading | Value | Units |", "|---|---|---|"]
        for row in event.get("rows") or []:
            cells = list(row) + ["", "", ""]
            label, value, units = cells[0], cells[1], cells[2]
            out.append(f"| {label} | {value} | {units} |")
        out.append("")
        return "\n".join(out)

    if kind == rs.IMAGE:
        # Standard Markdown image syntax, with a path relative to the
        # experiment folder -- so an exported .md opens correctly in any
        # Markdown viewer once `report_images.package_markdown` has copied the
        # figures next to it. The pixels are a sidecar file; see report_images.
        head = title or "Figure"
        return "\n".join(
            [f"#### 🖼 {head} — {stamp}", "", f"![{head}]({event.get('file') or ''})", ""]
        )

    if kind == rs.AGENT:
        # Always labelled: a reader must be able to tell agent-written prose
        # from the instrument's own record at a glance.
        out = [f"#### ✨ AutoPILOT — {stamp}", ""]
        if title:
            out.append(f"*{title}*")
            out.append("")
        out.append(text)
        out.append("")
        return "\n".join(out)

    out = [f"#### ✎ Note — {stamp}", ""]
    for line in text.splitlines() or [""]:
        out.append(f"> {line}")
    out.append("")
    return "\n".join(out)


# Fields compared to decide whether a persisted run record is out of date.
# `end_ts` is deliberately NOT among them: it advances with every line a
# chatty plan prints, and writing a new entry each time would grow the master
# file by thousands of lines over one long scan. It is picked up by the
# closing write instead, and the live view always shows the fresh fold anyway.
_RUN_REWRITE_FIELDS = ("closed", "ok", "error")


def collect(beamline: str, experiment: str, *, persist: bool = True) -> list[dict]:
    """Everything the report contains, with new runs reconciled into the file.

    Folds :mod:`experiment_history`'s entries into runs and writes any that
    ``report.jsonl`` does not already hold. Returns the complete, de-duplicated
    entry list to render.

    Two entries per run at most: one when it is first seen, one when it closes
    and its outcome is final (see ``_RUN_REWRITE_FIELDS``). A later entry
    supersedes an earlier one with the same ``ts``.

    `persist=False` reads without writing anything -- the mode AutoPILOT's
    read-only report tool uses, so that answering a question in chat never
    mutates the record.
    """
    stored = rs.read_events(beamline, experiment)
    persisted = {
        e.get("ts"): e for e in stored if e.get("kind") == rs.RUN and e.get("ts") is not None
    }

    fresh: list[dict] = []
    for run in fold_runs(eh.read_entries(beamline, experiment)):
        old = persisted.get(run["ts"])
        if persist and (
            old is None
            or any(old.get(f) != run[f] for f in _RUN_REWRITE_FIELDS)
        ):
            rs.append_event(
                beamline,
                experiment,
                rs.RUN,
                ts=run["ts"],
                **{k: v for k, v in run.items() if k != "ts"},
            )
        # Always overlay the freshly folded version, persisted or not: it
        # carries the newest end_ts, so a run in progress shows a live
        # duration rather than whatever was true when it was first written.
        fresh.append({"kind": rs.RUN, **run})

    return _collapse(stored, fresh)


def _collapse(stored: list[dict], fresh: list[dict]) -> list[dict]:
    """One entry per run (the newest wins); every authored entry kept."""
    runs: dict = {}
    others: list[dict] = []
    for event in list(stored) + list(fresh):
        if event.get("kind") == rs.RUN and event.get("ts") is not None:
            runs[event["ts"]] = event
        elif event.get("kind") != rs.RUN:
            others.append(event)
    return others + list(runs.values())


def render_markdown(
    events: list[dict],
    *,
    experiment: str,
    beamline: str,
    title: str = "",
) -> str:
    """The whole report document, rendered from the master file's entries.

    `events` is what :func:`collect` returns -- runs and authored entries
    together, in any order.
    """
    runs = sorted(
        (e for e in events if e.get("kind") == rs.RUN), key=lambda r: r.get("ts") or 0.0
    )
    for run in runs:  # tolerate an entry written by an older build
        run.setdefault("notes", [])
    loose = attach_notes(runs, [e for e in events if e.get("kind") != rs.RUN])

    items: list[tuple[float, str, dict]] = [(r["ts"], "run", r) for r in runs]
    items += [(e.get("ts") or 0.0, "event", e) for e in loose]
    # Stable sort with runs ahead of events at an identical timestamp, so a
    # snapshot taken the instant a plan starts still reads as belonging to it.
    items.sort(key=lambda it: (it[0], 0 if it[1] == "run" else 1))

    started = items[0][0] if items else time.time()
    out = [
        "<!-- Rendered by B-PILOT from the experiment's report.jsonl, which is",
        "     the live record. This copy is a point-in-time snapshot: it is not",
        "     updated, and editing it changes nothing on the instrument. -->",
        "",
        f"# {title or experiment}",
        "",
        f"**Experiment:** {experiment} · **Beamline:** {beamline} · "
        f"**Started:** {time.strftime('%Y-%m-%d %H:%M', time.localtime(started))}",
        "",
    ]

    if not items:
        out.append("_Nothing recorded yet. Runs appear here automatically; use the")
        out.append("Report panel's buttons to add notes and beamline snapshots._")
        out.append("")
        return "\n".join(out)

    day = ""
    for ts, kind, payload in items:
        this_day = time.strftime("%Y-%m-%d", time.localtime(ts))
        if this_day != day:
            day = this_day
            out.append("---")
            out.append("")
            out.append(f"## {time.strftime('%A, %d %B %Y', time.localtime(ts))}")
            out.append("")
        out.append(_run_markdown(payload) if kind == "run" else _event_markdown(payload))

    out.append("")
    out.append(f"_{len(runs)} plan run(s) recorded._")
    out.append("")
    return "\n".join(out)
