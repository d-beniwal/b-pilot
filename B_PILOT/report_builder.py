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

**Reading order is not storage order.** An entry sorts by its ``pos`` if it has
one and by its ``ts`` otherwise, so a figure captured now can be filed beside
the scan it illustrates from two hours ago without its timestamp being touched
-- the record still says when the pixels were captured, the notebook says where
they belong. ``pos`` and ``hidden`` are never written onto the entry itself;
they arrive as :data:`report_store.EDIT` overlays that :func:`apply_edits`
folds on at read time (see :func:`plan_move` for how a new ``pos`` is chosen).
"""
from __future__ import annotations

import ast
import fnmatch
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


#: Shown against a run suppressed by ``report_excluded_plans`` rather than by
#: an explicit click, so a reader looking at the hidden entries can tell the
#: two apart.
EXCLUDED_REASON = "excluded by configuration"


def entry_id(event: dict) -> str:
    """Stable identity for one entry, for an :data:`report_store.EDIT` to name.

    Entries written before ids existed have none, so the fallback is derived
    from what they do have. It has to be *deterministic* -- an id that changed
    between two reads would silently orphan every edit made against it -- which
    rules out anything generated at read time.
    """
    stored = event.get("id")
    if stored:
        return str(stored)
    return f"{event.get('kind') or 'x'}-{float(event.get('ts') or 0.0):.6f}"


def run_id(ts: float) -> str:
    """Identity for the run starting at `ts`.

    :func:`collect` writes a run **twice** -- once when first seen, once when
    it closes -- and both writes must land on one identity or an edit made
    against the first would be orphaned by the second. Deriving it from the
    timestamp, which is also what keys the supersession, guarantees that, and
    matches what :func:`entry_id` computes for a run written by an older build.
    """
    return f"{rs.RUN}-{float(ts):.6f}"


def sort_key(event: dict) -> float:
    """Where this entry reads, which is its ``pos`` if it has been placed and
    its capture time otherwise. ``bool`` is excluded explicitly -- it is an
    ``int`` subclass in Python and would otherwise sort a stray flag as 0/1."""
    pos = event.get("pos")
    if isinstance(pos, (int, float)) and not isinstance(pos, bool):
        return float(pos)
    return float(event.get("ts") or 0.0)


def ordered(entries: list[dict]) -> list[dict]:
    """`entries` in reading order.

    Runs come first at an identical key so a snapshot taken the instant a plan
    starts still reads as belonging to it -- the same tie-break the renderer
    has always used, kept in one place now that two callers need it.
    """
    return sorted(
        entries, key=lambda e: (sort_key(e), 0 if e.get("kind") == rs.RUN else 1)
    )


def is_excluded(plan_name: str | None, patterns) -> bool:
    """Whether `plan_name` matches one of the configured exclusion patterns.

    ``fnmatch`` rather than equality so ``cont_acq*`` covers a family, while a
    bare name still matches itself exactly. Case-sensitive: plan names are
    Python identifiers, and a case-insensitive match would be a lie.
    """
    if not plan_name or not patterns:
        return False
    return any(
        fnmatch.fnmatchcase(plan_name, pattern.strip())
        for pattern in patterns
        if isinstance(pattern, str) and pattern.strip()
    )


def apply_edits(events: list[dict], *, exclude=()) -> list[dict]:
    """Fold :data:`report_store.EDIT` overlays onto their targets.

    Returns the non-edit entries, each with its ``id`` filled in and any
    ``pos``/``hidden`` override applied. Edits are newest-wins per *field*, so
    hiding a block and later moving it keeps both.

    Exclusion by plan name is applied as a **default** rather than an override:
    an explicit edit wins, so a run the config would suppress stays visible if
    the user has deliberately unhidden it. An edit naming an entry that is not
    in `events` is simply ignored -- the report may have been read at a moment
    that entry was not yet reconciled in.
    """
    overrides: dict[str, dict] = {}
    for event in events:
        if event.get("kind") != rs.EDIT:
            continue
        target = event.get("target")
        if not target:
            continue
        overrides.setdefault(str(target), {}).update(
            {k: v for k, v in event.items() if k in ("pos", "hidden")}
        )

    resolved: list[dict] = []
    for event in events:
        if event.get("kind") == rs.EDIT:
            continue
        entry = dict(event)
        identity = entry_id(entry)
        entry["id"] = identity
        override = overrides.get(identity) or {}

        if entry.get("kind") == rs.RUN and is_excluded(entry.get("plan_name"), exclude):
            entry["hidden"] = True
            entry["hidden_reason"] = EXCLUDED_REASON
        if "hidden" in override:
            entry["hidden"] = bool(override["hidden"])
            if not entry["hidden"]:
                entry.pop("hidden_reason", None)
        if "pos" in override:
            entry["pos"] = override["pos"]
        resolved.append(entry)
    return resolved


# ── Placement ────────────────────────────────────────────────────────────────
# Positions are floats and a new one is the midpoint of its neighbours, which
# is ample in practice: consecutive entries are normally seconds apart in `ts`,
# so there are billions of representable slots between any two. The guard below
# exists for the pathological case of repeatedly inserting into one shrinking
# gap, which would eventually exhaust double precision and start silently
# tying entries together.

#: Below this, a gap is treated as having no room left and the whole report is
#: renumbered instead. Chosen well above double-precision resolution near a
#: Unix timestamp (~1e-7 at 1.7e9) so the check fires before ties can occur.
MIN_GAP = 1e-6


def _mid(before: float, after: float) -> float | None:
    """Midpoint of two positions, or ``None`` if the gap has closed."""
    if after - before < MIN_GAP:
        return None
    return before + (after - before) / 2.0


def renumber(items: list[dict]) -> list[dict]:
    """Edits assigning `items` (in reading order) the positions ``1..N``.

    The escape hatch when a gap runs out. Renumbering to small integers is
    deliberate: a later entry defaults to ``pos = ts`` (~1.7e9), so it still
    sorts after everything renumbered here, which is where a new entry belongs.
    """
    return [{"target": entry_id(item), "pos": float(i + 1)} for i, item in enumerate(items)]


def _index_of(items: list[dict], identity: str) -> int | None:
    for i, item in enumerate(items):
        if entry_id(item) == identity:
            return i
    return None


def plan_insert(entries: list[dict], after_id: str) -> tuple[float | None, list[dict]]:
    """``(pos for a new entry, edits to apply first)``.

    A ``None`` position means "leave it unset" -- the entry then sorts by its
    own timestamp, which is already the end of the report. That is the answer
    both for "at the end" and for "after the last entry", so the common case
    writes no override at all and the report stays exactly as it was.
    """
    if not after_id:
        return None, []
    items = ordered(entries)
    idx = _index_of(items, after_id)
    if idx is None or idx == len(items) - 1:
        return None, []
    position = _mid(sort_key(items[idx]), sort_key(items[idx + 1]))
    if position is not None:
        return position, []
    return float(idx + 1) + 0.5, renumber(items)


def plan_move(entries: list[dict], moved_id: str, after_id: str) -> list[dict]:
    """Edits that place `moved_id` directly after `after_id` (``""`` = the top).

    The moved entry is taken out of the list before its destination is measured,
    so "after the entry that currently follows me" means what a reader expects
    rather than landing back where it started.
    """
    items = [e for e in ordered(entries) if entry_id(e) != moved_id]
    if not items:
        return []

    if not after_id:
        return [{"target": moved_id, "pos": sort_key(items[0]) - 1.0}]

    idx = _index_of(items, after_id)
    if idx is None:
        return []
    before = sort_key(items[idx])
    if idx == len(items) - 1:
        return [{"target": moved_id, "pos": before + 1.0}]
    position = _mid(before, sort_key(items[idx + 1]))
    if position is not None:
        return [{"target": moved_id, "pos": position}]
    return renumber(items) + [{"target": moved_id, "pos": float(idx + 1) + 0.5}]


def plan_reset(entries: list[dict]) -> list[dict]:
    """Edits that drop every position override, back to timestamp order.

    Clearing a position is itself an append -- ``{"pos": None}`` -- rather than
    a deletion of the edit that set it. Same reason as everything else in this
    module: the file only ever grows, so a reset is recorded as the deliberate
    act it was, and the edits it supersedes are still there to read.

    Because ``sort_key`` falls back to ``ts`` for a ``pos`` that is not a
    number, an explicit ``None`` and a never-set position sort identically.

    Only ordering is touched. Hidden entries stay hidden: "put this back in
    time order" is not a statement about what belongs in the report.
    """
    return [
        {"target": entry_id(e), "pos": None}
        for e in ordered(entries)
        if e.get("pos") is not None
    ]


def plan_step(entries: list[dict], moved_id: str, delta: int) -> list[dict]:
    """Edits that nudge `moved_id` one place up (``delta=-1``) or down (``+1``).

    What the report's own inline ``[↑]``/``[↓]`` controls call. Expressed in
    terms of :func:`plan_move` so there is one implementation of the awkward
    part; a nudge off either end is a no-op rather than an error.
    """
    items = ordered(entries)
    idx = _index_of(items, moved_id)
    if idx is None:
        return []
    if delta < 0:
        if idx == 0:
            return []
        # Above my predecessor == after whatever precedes *it* (or the top).
        return plan_move(entries, moved_id, entry_id(items[idx - 2]) if idx >= 2 else "")
    if idx >= len(items) - 1:
        return []
    return plan_move(entries, moved_id, entry_id(items[idx + 1]))


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
    by_time = sorted(entries, key=lambda e: e.get("ts") or 0.0)
    runs: list[dict] = []
    current: dict | None = None

    for entry in by_time:
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


#: URL scheme the report's own inline controls use. `report_panel._on_anchor`
#: is the only thing that interprets it, and `QTextBrowser.setOpenLinks(False)`
#: means nothing tries to navigate to it.
CONTROL_SCHEME = "bpilot"


def _controls_md(entry: dict, controls: bool) -> str:
    """The trailing ``[hide] [↑] [↓]`` links for one block, or nothing.

    Emitted only for the live panel. An exported document must never carry
    them: they are UI, they would render as dead links in anyone else's
    Markdown viewer, and a PDF of a lab record should read as a document.
    """
    if not controls:
        return ""
    identity = entry.get("id") or entry_id(entry)
    verb = "show" if entry.get("hidden") else "hide"
    return (
        f"  [{verb}]({CONTROL_SCHEME}:{verb}/{identity})"
        f" [↑]({CONTROL_SCHEME}:up/{identity})"
        f" [↓]({CONTROL_SCHEME}:down/{identity})"
    )


def _hidden_prefix(entry: dict) -> str:
    return "🚫 " if entry.get("hidden") else ""


def _hidden_note(entry: dict) -> list[str]:
    """The line explaining *why* a block is hidden, when it is being shown."""
    if not entry.get("hidden"):
        return []
    reason = entry.get("hidden_reason") or "hidden from the report"
    return [f"_{reason}_", ""]


def _stamp(entry: dict, day: str) -> str:
    """Time of capture -- with its date whenever the section in effect is not
    this entry's own day. A relocated entry must still say when it actually
    happened, or moving a figure would quietly relabel it.

    An empty `day` counts as "differs", which is the case of an entry dragged
    above the report's first day heading: there is no section over it at all,
    so a bare clock time there would read as belonging to the heading that
    comes *after* it -- a different day.

    An unmoved entry is always rendered right after its own day heading was
    emitted, so it takes the short form.
    """
    when = time.localtime(entry.get("ts") or 0.0)
    if time.strftime("%Y-%m-%d", when) != day:
        return time.strftime("%Y-%m-%d %H:%M:%S", when)
    return time.strftime("%H:%M:%S", when)


def _run_markdown(run: dict, *, controls: bool = False, day: str = "") -> str:
    """One run as a Markdown section."""
    stamp = _stamp(run, day)
    out = [
        f"### {_hidden_prefix(run)}{stamp} — {run['plan_name']}{_controls_md(run, controls)}",
        "",
    ]
    out += _hidden_note(run)
    out += ["```python", run["command"], "```", ""]

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


def _event_markdown(event: dict, *, controls: bool = False, day: str = "") -> str:
    """One manual event as a Markdown section."""
    kind = event.get("kind")
    stamp = _stamp(event, day)
    text = event.get("text") or ""
    title = event.get("title") or ""
    mark = _hidden_prefix(event)
    tools = _controls_md(event, controls)

    if kind == rs.HEADING:
        # "▸" marks this as the user's own section break, so it reads
        # distinctly from the automatic day headings at the same level.
        return f"## {mark}▸ {title or text}{tools}\n"

    if kind == rs.SNAPSHOT:
        head = title or "Beamline snapshot"
        out = [f"#### {mark}📸 {head} — {stamp}{tools}", ""]
        out += _hidden_note(event)
        out += ["| Reading | Value | Units |", "|---|---|---|"]
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
        out = [f"#### {mark}🖼 {head} — {stamp}{tools}", ""]
        out += _hidden_note(event)
        out += [f"![{head}]({event.get('file') or ''})", ""]
        return "\n".join(out)

    if kind == rs.AGENT:
        # Always labelled: a reader must be able to tell agent-written prose
        # from the instrument's own record at a glance.
        out = [f"#### {mark}✨ AutoPILOT — {stamp}{tools}", ""]
        out += _hidden_note(event)
        if title:
            out.append(f"*{title}*")
            out.append("")
        out.append(text)
        out.append("")
        return "\n".join(out)

    out = [f"#### {mark}✎ Note — {stamp}{tools}", ""]
    out += _hidden_note(event)
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


def collect(
    beamline: str, experiment: str, *, persist: bool = True, exclude=()
) -> list[dict]:
    """Everything the report contains, with new runs reconciled into the file.

    Folds :mod:`experiment_history`'s entries into runs and writes any that
    ``report.jsonl`` does not already hold. Returns the complete, de-duplicated
    entry list to render, with every ``pos``/``hidden`` overlay already applied
    (see :func:`apply_edits`).

    Two entries per run at most: one when it is first seen, one when it closes
    and its outcome is final (see ``_RUN_REWRITE_FIELDS``). A later entry
    supersedes an earlier one with the same ``ts``.

    `exclude` is the configured plan-name exclusion list. Excluded runs are
    still reconciled and still written -- they come back marked ``hidden``, so
    they stay in the record and reappear the moment the exclusion is lifted,
    with nothing to rebuild.

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
        identity = run_id(run["ts"])
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
                id=identity,
                **{k: v for k, v in run.items() if k != "ts"},
            )
        # Always overlay the freshly folded version, persisted or not: it
        # carries the newest end_ts, so a run in progress shows a live
        # duration rather than whatever was true when it was first written.
        fresh.append({"kind": rs.RUN, "id": identity, **run})

    return apply_edits(_collapse(stored, fresh), exclude=exclude)


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


def visible_entries(events: list[dict], *, show_hidden: bool = False) -> list[dict]:
    """The entries a reader sees, in reading order.

    Shared by the renderer and the Arrange list so the two can never disagree
    about what the report currently contains or what order it is in.
    """
    return ordered(
        [e for e in events if show_hidden or not e.get("hidden")]
    )


def render_markdown(
    events: list[dict],
    *,
    experiment: str,
    beamline: str,
    title: str = "",
    show_hidden: bool = False,
    controls: bool = False,
) -> str:
    """The whole report document, rendered from the master file's entries.

    `events` is what :func:`collect` returns -- runs and authored entries
    together, in any order, with their overlays already applied.

    `show_hidden` brings hidden entries back, marked and with the reason they
    were suppressed; every export path leaves it off, which is what hiding
    means. `controls` adds the panel's inline ``[hide] [↑] [↓]`` links and is
    likewise for the live view only.
    """
    runs = sorted(
        (e for e in events if e.get("kind") == rs.RUN), key=lambda r: r.get("ts") or 0.0
    )
    for run in runs:  # tolerate an entry written by an older build
        run.setdefault("notes", [])
    # Notes are attached before anything is filtered, so hiding a run takes the
    # note that belongs to it along rather than leaving it floating unmoored.
    loose = attach_notes(runs, [e for e in events if e.get("kind") != rs.RUN])
    items = visible_entries(runs + loose, show_hidden=show_hidden)

    # Earliest capture time, not the first entry in reading order: moving a
    # block to the top must not relabel when the experiment started.
    started = min((e.get("ts") or 0.0) for e in items) if items else time.time()
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
    for entry in items:
        when = entry.get("ts") or 0.0
        this_day = time.strftime("%Y-%m-%d", time.localtime(when))
        # An entry that has been deliberately filed somewhere does NOT open a
        # day: one figure moved back two hours would otherwise make the day
        # headings flap A -> B -> A. The chronological spine stays the entries
        # nobody has moved, and a relocated one prints its own full date
        # instead (see `_stamp`).
        if this_day != day and entry.get("pos") is None:
            day = this_day
            out.append("---")
            out.append("")
            out.append(f"## {time.strftime('%A, %d %B %Y', time.localtime(when))}")
            out.append("")
        out.append(
            _run_markdown(entry, controls=controls, day=day)
            if entry.get("kind") == rs.RUN
            else _event_markdown(entry, controls=controls, day=day)
        )

    shown = sum(1 for e in items if e.get("kind") == rs.RUN)
    tally = f"_{shown} plan run(s) recorded._"
    buried = len(runs) - shown
    if buried > 0:
        tally = f"_{shown} plan run(s) recorded; {buried} hidden._"
    out.append("")
    out.append(tally)
    out.append("")
    return "\n".join(out)
