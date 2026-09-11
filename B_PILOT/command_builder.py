"""Compose the two-line ``from ... import ...`` + ``RE(plan(...))`` command
string queued/run by :class:`B_PILOT.plan_runner.PlanRunnerPanel`,
:class:`B_PILOT.switchto_popup.SwitchToPopup`, and
:class:`B_PILOT.contacq_popup.ContAcqPopup`.

Extracted out of `PlanRunnerPanel` so both panels build commands the same
way; `plan_name`/`module`/`params` are passed explicitly instead of being
read off `self`.

The import line is always part of the composed text (the Command panel shows
it); :func:`for_console` decides whether it survives into what is actually
sent to the kernel, per the profile's ``send_import_line`` setting.

Also builds the structured item dict the Bluesky queueserver (QS)'s
``item_add`` expects (see :func:`make_queue_item`) -- kept dependency-free
(no ``bluesky_queueserver_api`` import) since this module is used by the
interactive-Run path too, which has nothing to do with QS.
"""
from __future__ import annotations

import ast
import re

from . import config
from .plan_parser import ParamSpec
from .plan_parser import RawCode

# A top-level (column-0) import statement, as emitted by `make_import_line`.
# Indented imports are deliberately NOT matched -- one inside a hand-edited
# `try:`/function body is part of that block's logic, not a dispatch preamble.
_IMPORT_LINE = re.compile(r"^(?:from\s+\S+\s+import\s|import\s)")


def make_import_line(plan_name: str, module: str) -> str:
    return f"from {module} import {plan_name}"


def strip_import_lines(text: str) -> str:
    """`text` with its top-level ``import`` / ``from ... import ...`` lines removed."""
    kept = [ln for ln in text.splitlines() if not _IMPORT_LINE.match(ln)]
    return "\n".join(kept).strip()


def for_console(text: str) -> str:
    """The dispatch form of a composed command: what actually goes to the kernel.

    The Command panel always *shows* the ``from ... import ...`` line, but
    whether it is *sent* is per-profile (Configuration -> Launch Session ->
    "Send the import line ..."; ``send_import_line``, off by default). Off is
    safe because the profile's starter script already imports the instrument
    into the kernel's namespace, and it keeps a stale/wrong module path from
    killing the whole cell -- the import and the ``RE(...)`` call travel as
    one execution, so a failed import means the plan never runs.

    A command that is *only* import lines is passed through unchanged rather
    than reduced to an empty cell.
    """
    if config.get("send_import_line"):
        return text
    return strip_import_lines(text) or text


def make_re_line(
    plan_name: str,
    params: list[ParamSpec],
    values: dict,
    notes: str = "",
    extra_md: dict | None = None,
) -> str:
    """Compose the ``RE(plan(...))`` line.

    `extra_md` adds entries to the run's ``md`` alongside `notes` -- used for
    scan-preset provenance (``{'preset': 'tomoscan_sw'}``), since a preset
    dispatches its parent skeleton plan and would otherwise leave no trace of
    which preset produced the run. `notes` wins on a key collision.
    """
    if "__args__" in values:
        inner = f"{plan_name}({values['__args__']})"
    else:
        values = dict(values)
        # Leading positional args (scan_skeletons.py's *args, from
        # MotorRowsWidget.tokens()) must come first — Python syntax requires
        # positional args before keyword args.
        args = list(values.pop("__positional__", []))
        for spec in params:
            if spec.name not in values:
                continue
            val = values[spec.name]
            # RawCode (device/block refs) emit verbatim; everything else via repr().
            rendered = str(val) if isinstance(val, RawCode) else repr(val)
            args.append(f"{spec.name}={rendered}")
        inner = f"{plan_name}({', '.join(args)})"
    # Lands in the run's start document (cat[uid].metadata["start"][...]).
    md = {k: v for k, v in (extra_md or {}).items() if v is not None}
    if notes:
        md["notes"] = notes
    if md:
        return f"RE({inner}, md={md!r})"
    return f"RE({inner})"


def _source_fragment_to_value(fragment: str):
    """Best-effort native Python value for a source-text fragment (a
    `MotorRowsWidget.tokens()` entry, or a raw `values["__args__"]` chunk):
    a numeric/string/list literal round-trips via `ast.literal_eval`; a bare
    device/motor name (or any other non-literal expression, e.g. `motor.axis`)
    has no live-object equivalent over the QS wire, so it is passed through
    as a plain string -- see `make_queue_item`'s docstring for why this is a
    known, flagged limitation rather than an oversight.
    """
    try:
        return ast.literal_eval(fragment)
    except Exception:  # noqa: BLE001
        return fragment


def make_queue_item(
    plan_name: str,
    params: list[ParamSpec],
    values: dict,
    extra_md: dict | None = None,
) -> dict | None:
    """Return a QS ``item_add``-shaped dict: ``{item_type, name, args, kwargs}``.

    `extra_md` becomes the item's ``md`` kwarg -- scan-preset provenance, the
    queue-side counterpart of :func:`make_re_line`'s. Only ever non-empty for
    a preset, whose `parent_plan` is one of the ``scan_skeletons.py`` plans;
    all six take an ``md`` argument, so it is always bindable. (It cannot be
    checked against `params` here: ``md`` is deliberately undocumented, so it
    never appears as a `ParamSpec`.)

    Mirrors `make_re_line`'s walk over `params`/`values`, but keeps native
    Python values instead of `repr()`'d source text (QS's ZMQ transport
    serializes the dict itself). Returns ``None`` for the generic
    "arguments as Python" fallback shape (``values["__args__"]``, used for
    undocumented plans with no parsed `ParamSpec`s) -- that free-text shape
    has no reliable structured equivalent, same as a hand-edited command;
    callers should treat a ``None`` result like the hand-edited-text case
    (queueing via QS isn't available for it).

    **Known limitation, not resolved here:** a `RawCode` value (a device/
    motor reference, e.g. ``det1`` or ``motor.axis`` or a device_list's
    ``[pg6, pg7]``) is a *live object reference* in the embedded-kernel
    world this grammar was designed for -- there is no equivalent "the
    running QS environment's object named X" wire value, so these become
    plain strings (or a list of plain strings, for a device_list) in the
    QS item's `kwargs`. Whether QS's own plan-argument binding resolves a
    bare device-name string to the real object depends on how the target
    plan itself is annotated in `instrument/plans/*.py`.

    For `det`/`scalers`/other device-typed kwargs this was verified live
    against redwood on 2026-08-25 (plain string device kwargs resolved
    correctly -- see `mpe_bluesky/b-pilot/.context/DECISIONS.md`). For the
    `plan_opener`/`per_step`/`plan_closer` (and `auxiliary_scan.py`'s
    `take_reading`) `block{...}`-dtype kwargs specifically -- i.e. the six
    `scan_skeletons.py` plans and `one_shot_no_checkpoint` -- this was a
    real, confirmed gap as of 2026-08-25: those plans defaulted these
    kwargs to live function objects, which QS's own registration can't
    `ast.literal_eval`, so the plans never registered with QS at all (see
    `mpe_bluesky/documents/qserver_plan_compatibility_deep_dive.md`). Fixed
    on the `mpe_bluesky` side (branch `qserver_update`): those plans now
    default these kwargs to plain string names and resolve the string back
    to the real callable internally (`instrument/plans/plan_registry.py`),
    which is exactly the string shape this function already sends -- no
    change was needed here. Verified statically (the deep-dive's own AST
    audit + an isolated `_process_plan` registration check against the real
    `bluesky_queueserver` package) but **not yet exercised through a live
    QS dispatch of one of these six plans** -- do that before considering
    this fully closed.
    """
    if "__args__" in values:
        return None

    values = dict(values)
    args = [
        _source_fragment_to_value(tok) for tok in values.pop("__positional__", [])
    ]
    kwargs: dict = {}
    for spec in params:
        if spec.name not in values:
            continue
        val = values[spec.name]
        if isinstance(val, RawCode):
            text = str(val).strip()
            if spec.dtype == "code":
                # A `code` field holds an arbitrary expression. When it is a
                # real literal (a dict for `md`, a number, a string) it has an
                # exact wire value, so send that rather than its source text --
                # QS needs `md` as a dict, not as "{'sample': 'A'}". Only when
                # it is NOT a literal (`[scaler1, tc32E]` -- live object
                # references) does it fall through to the name-string handling
                # below, which is the same best-effort the device dtypes get.
                try:
                    kwargs[spec.name] = ast.literal_eval(text)
                    continue
                except (ValueError, SyntaxError):
                    pass
            if text.startswith("[") and text.endswith("]"):
                kwargs[spec.name] = [
                    n.strip() for n in text[1:-1].split(",") if n.strip()
                ]
            else:
                kwargs[spec.name] = text
        elif isinstance(val, list):
            # `positions` dtype: a list of (x, y, z) tuples -- tuples aren't
            # native to every wire encoding, so flatten to lists.
            kwargs[spec.name] = [list(v) if isinstance(v, tuple) else v for v in val]
        else:
            kwargs[spec.name] = val
    md = {k: v for k, v in (extra_md or {}).items() if v is not None}
    if md:
        kwargs["md"] = md
    return {"item_type": "plan", "name": plan_name, "args": args, "kwargs": kwargs}


def display_command_from_item(item: dict, notes: str = "") -> str:
    """Reconstruct an ``RE(plan(...))``-shaped preview string from a QS item
    dict -- used only for `queue_panel.py`'s Command-column preview and its
    "Copy to form" round-trip (via `plan_runner.load_from_command`), never
    sent anywhere. Not a faithful inverse of `make_queue_item` (device names
    lost their `RawCode`-unquoted marking once round-tripped through QS's
    JSON wire) -- good enough for a human-readable preview and for
    `load_from_command`'s own `ast`-based re-parse, which only needs valid
    Python source shaped like a real call.
    """
    name = item.get("name", "")
    parts = [repr(a) for a in item.get("args", [])]
    parts.extend(f"{k}={v!r}" for k, v in item.get("kwargs", {}).items())
    inner = f"{name}({', '.join(parts)})"
    if notes:
        return f"RE({inner}, md={{'notes': {notes!r}}})"
    return f"RE({inner})"
