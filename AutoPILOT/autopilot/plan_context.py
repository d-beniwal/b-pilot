"""Static, hand-authored context for plan generation: the docstring grammar
B-PILOT's plan_parser expects, and a registry of drafting templates built
from the real plan files.

The docstring grammar (``GRAMMAR`` below) is deliberately hand-written, not
derived at runtime -- it changes rarely and costs real tokens every session.

The template registry (``TEMPLATES``), however, IS derived at import time
from ``B_PILOT.plan_parser.find_plan_specs()`` over every ``.py`` entry in
the ACTIVE PROFILE's own ``visible_plan_files`` whitelist (the same config
key, and the same parser, B-PILOT's own plan-runner file browser uses --
see ``B_PILOT/plan_runner.py``'s ``_populate_file_browser``). This keeps
AutoPILOT's drafting scope exactly in sync with what a human can already
run through B-PILOT's GUI, on every profile (MPE, a BITS instrument like
s3idc, or the fully-simulated ``demo`` stack) -- add a new documented plan
to a file already listed there (or reformat an existing one into the
grammar) and it becomes draftable with no change here.

Each template wraps one of these real, tested plans rather than reproducing
its body -- the LLM only ever fills in the parameters the plan's own
docstring documents, and the renderer emits an ``RE(<plan>(...))`` command
that drives B-PILOT's form directly. See ``plan_renderer.py``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from ._bpilot_path import ensure_bpilot_on_path

ensure_bpilot_on_path()

from B_PILOT import config as bpilot_config  # noqa: E402
from B_PILOT import paths as bpilot_paths  # noqa: E402
from B_PILOT.plan_parser import ParamSpec, find_plan_specs  # noqa: E402  (reused, not reinvented)

# ── Docstring grammar (verbatim summary of B_PILOT/plan_parser.py's own docstring) ──

GRAMMAR = """\
Every generated plan function must document its parameters in this exact
NumPy-style grammar, so B-PILOT's docstring parser renders a working form:

    Parameters
    ----------
    <name> : <dtype>[ [<units>]]
        <short label> :: <longer description>

Rules:
* dtype is one of: str, int, float, bool, choice{a, b, ...}, positions,
  device{category}, device_list{category}, block{category}.
* block{category} names one of the active profile's plan-building-block
  lists (plan_opener, per_step, plan_closer, suspender, pseudo_suspender) --
  like device/device_list it is a real Python identifier, never a quoted
  string, and it must always be given a concrete value (never left blank).
* device{motor:whole} (category "motor" only): use this instead of plain
  device{motor} when the plan wants the bare multi-axis device object itself
  (it indexes sub-axes internally, e.g. `sms.y`) rather than a single
  resolved motor.axis. Do not use ":whole" on any other dtype/category.
* units are optional, in square brackets, e.g. [s], [mm], [deg].
* the body line is split on the FIRST ' :: ' into a short label and a
  longer tooltip -- both are required.
* default value and required/optional come from the Python signature, NOT
  the docstring: no default => required; a literal default => optional.
* device / device_list / block values are Python identifiers bound via
  imports at the top of the file (real ophyd objects or building-block
  functions), never quoted strings.
* only document parameters a human should be able to edit before running;
  everything else should be a plain positional/keyword argument to the
  wrapped real plan, left at that plan's own default.
"""

# Kept for plan_renderer.py's dormant _CALL_BODY/render() fallback path,
# which still keys off these two string constants even though the dynamic
# TEMPLATES registry below no longer populates them -- removing them would
# be a hard ImportError at module load, not a harmless dead branch.
STEP_SCAN = "step_scan"
COUNT = "count"


def _template_files() -> list[str]:
    """``plans_dir``-relative ``.py`` files in scope for AutoPILOT drafting.

    Reads the ACTIVE PROFILE's ``visible_plan_files`` (the same whitelist
    B-PILOT's own plan-runner file browser uses -- see
    ``B_PILOT/plan_runner.py``'s ``_populate_file_browser``), not a
    hardcoded MPE-specific list: a BITS profile like ``s3idc`` points this
    at ``user/s3idc_gui/...``, and ``demo`` at ``plans.py``, both of which
    a fixed MPE-only tuple would silently miss (leaving ``TEMPLATES`` empty
    and every ``propose_*_plan`` tool unavailable on those profiles).
    Preset ``.json`` entries in the same whitelist are for the GUI's own
    file browser only and are not (yet) draftable here.
    """
    visible = bpilot_config.get("visible_plan_files") or []
    return [f for f in visible if f.endswith(".py")]


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    description: str  # shown to the classifier / included in the system prompt
    module: str  # instrument/plans/<module>.py this template wraps
    function: str  # the real plan function being called
    param_specs: tuple[ParamSpec, ...]
    wrapper_name_hint: str  # slug used to name the generated function/file
    # When set, this template's param names line up with a REAL plan already
    # documented for B-PILOT's own form (see B_PILOT/plan_parser.py's grammar),
    # so a validated request can drive that form directly via
    # PlanRunnerPanel.load_from_command() instead of writing a draft file --
    # see plan_renderer.render_command() / pipeline.converse(). None means
    # "not yet drivable" (no dynamically-built template leaves this unset).
    gui_plan_name: str | None = None
    gui_plan_file: str | None = None  # file to check in B-PILOT's file browser
    # (shape, relative) from B_PILOT.plan_parser.SKELETON_SHAPES when this
    # plan takes its motor(s)/position(s) through a bare *args -- see
    # plan_spec.py's `axes` handling and plan_renderer.py's positional-token
    # rendering. None for every ordinary keyword-only plan.
    skeleton: tuple[str, bool] | None = None


def _build_templates() -> dict[str, Template]:
    templates: dict[str, Template] = {}
    for filename in _template_files():
        path = os.path.join(bpilot_paths.PLANS_DIR, filename)
        for name, spec in find_plan_specs(path).items():
            if not spec["documented"]:
                continue
            templates[name] = Template(
                key=name,
                title=name,
                description=spec["summary"],
                module=os.path.splitext(filename)[0],
                function=name,
                param_specs=tuple(spec["params"]),
                wrapper_name_hint=name,
                gui_plan_name=name,
                gui_plan_file=filename,
                skeleton=spec["skeleton"],
            )
    return templates


TEMPLATES: dict[str, Template] = _build_templates()
