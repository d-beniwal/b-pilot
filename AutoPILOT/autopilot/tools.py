"""Read-only lookup tools available to the multi-turn agent loop in `pipeline.py`,
alongside the propose/decline/ask_user tools in `plan_spec.py`.

Most wrap data AutoPILOT already loads per request (the active profile's
`DeviceCatalog`, `plan_context.TEMPLATES`) -- no network calls, nothing that
could reach hardware. The search_runs/describe_run/read_run_data tools are
the exception: they read already-recorded run metadata/data from this
beamline's databroker catalog (via `data_catalog.py`, Mongo reads only,
still never touching hardware or the RunEngine). These exist so the model
can double-check a device name, discover what plan types actually exist, or
look up a real recorded run instead of confidently guessing when it isn't sure.
"""
from __future__ import annotations

import fnmatch
import itertools
import os
import re
from pathlib import Path

from . import data_catalog
from . import settings as autopilot_settings
from .device_catalog import DeviceCatalog
from .plan_catalog import PlanInfo
from .plan_context import TEMPLATES
from ._bpilot_path import ensure_bpilot_on_path

ensure_bpilot_on_path()

from B_PILOT import config as bpilot_config  # noqa: E402
from B_PILOT import paths as bpilot_paths  # noqa: E402
from B_PILOT import plan_parser as bpilot_plan_parser  # noqa: E402

LIST_DEVICES_TOOL_NAME = "list_devices"
LIST_PLANS_TOOL_NAME = "list_plans"
LIST_ALL_PLANS_TOOL_NAME = "list_all_plans"
DESCRIBE_PLAN_TOOL_NAME = "describe_plan"
LIST_SCAN_BUILDING_BLOCKS_TOOL_NAME = "list_scan_building_blocks"
READ_PLAN_FILE_TOOL_NAME = "read_plan_file"
VALIDATE_DOCSTRING_TOOL_NAME = "validate_docstring"
SEARCH_RUNS_TOOL_NAME = "search_runs"
DESCRIBE_RUN_TOOL_NAME = "describe_run"
READ_RUN_DATA_TOOL_NAME = "read_run_data"
LIST_DIRECTORY_TOOL_NAME = "list_directory"
SEARCH_CODEBASE_TOOL_NAME = "search_codebase"
READ_SOURCE_FILE_TOOL_NAME = "read_source_file"
READ_EXPERIMENT_REPORT_TOOL_NAME = "read_experiment_report"


def build_list_devices_schema() -> dict:
    return {
        "name": LIST_DEVICES_TOOL_NAME,
        "description": (
            "List devices available on the active beamline profile, optionally "
            "filtered by category (e.g. 'motor', 'area_detector', 'scaler'). Use "
            "this to check whether a device name you were given actually exists "
            "before guessing a substitute."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Optional device category to filter by. Omit to list all categories.",
                }
            },
            "required": [],
        },
    }


def build_list_plans_schema() -> dict:
    return {
        "name": LIST_PLANS_TOOL_NAME,
        "description": "List the scan/count plan types AutoPILOT currently knows how to generate.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }


def build_list_all_plans_schema() -> dict:
    return {
        "name": LIST_ALL_PLANS_TOOL_NAME,
        "description": (
            "List every real plan in this beamline's instrument.plans catalog "
            "(not just the ones AutoPILOT can draft) -- includes tomography, "
            "alignment, and grid-scan plans. Use this to find the right plan by "
            "name for a request, then call describe_plan for its parameters. "
            "Compact summaries only; most extended-tier plans have "
            "'documented': false, meaning their parameter list is incomplete, "
            "NOT that they take no parameters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": ["vetted", "extended", "all"],
                    "description": (
                        "'vetted' = fully parameter-documented plans shown in the "
                        "B-PILOT GUI. 'extended' = the broader real plan catalog "
                        "(often undocumented parameters). Default 'all'."
                    ),
                }
            },
            "required": [],
        },
    }


def build_describe_plan_schema() -> dict:
    return {
        "name": DESCRIBE_PLAN_TOOL_NAME,
        "description": (
            "Get full parameter detail for a specific plan by name from the real "
            "instrument.plans catalog (from list_all_plans). Use this before "
            "answering any question about a plan's arguments, defaults, or units."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact plan (function) name, e.g. 'mpe_count' or 'tomoscan_sw'.",
                }
            },
            "required": ["name"],
        },
    }


def build_list_scan_building_blocks_schema() -> dict:
    return {
        "name": LIST_SCAN_BUILDING_BLOCKS_TOOL_NAME,
        "description": (
            "List the named plan_opener/per_step/plan_closer/suspender/"
            "pseudo_suspender building blocks available on this beamline for "
            "scan_skeletons.py-style plans (e.g. mpe_step_scan's plan_opener= "
            "argument). Use this to answer questions about what building blocks "
            "exist or to check a name before assuming it's valid."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }


def known_categories() -> list[str]:
    return sorted(
        {spec.category for template in TEMPLATES.values() for spec in template.param_specs if spec.category}
    )


def list_devices(catalog: DeviceCatalog, category: str | None) -> dict:
    if category:
        return {"category": category, "devices": catalog.names_for(category)}
    return {cat: catalog.names_for(cat) for cat in known_categories()}


def list_plans() -> dict:
    return {
        key: {
            "title": template.title,
            "description": template.description,
            "parameters": [
                {
                    "name": spec.name,
                    "dtype": spec.dtype,
                    "category": spec.category,
                    "required": spec.required,
                    "description": spec.long,
                }
                for spec in template.param_specs
            ],
        }
        for key, template in TEMPLATES.items()
    }


def _plan_summary(plan: PlanInfo) -> dict:
    return {
        "name": plan.name,
        "file": plan.file,
        "tier": plan.tier,
        "summary": plan.summary,
        "documented": plan.documented,
        "required_params": (
            [p.name for p in plan.params if p.required] if plan.documented else None
        ),
    }


def list_all_plans(plan_catalog: list[PlanInfo], tier: str | None) -> dict:
    tier = tier or "all"
    plans = plan_catalog if tier == "all" else [p for p in plan_catalog if p.tier == tier]
    return {"plans": [_plan_summary(p) for p in plans]}


def _plan_param_detail(param) -> dict:
    return {
        "name": param.name,
        "dtype": param.dtype,
        "units": param.units,
        "short": param.short,
        "long": param.long,
        "default": param.default,
        "required": param.required,
        "choices": param.choices,
        "category": param.category,
    }


def describe_plan(plan_catalog: list[PlanInfo], name: str) -> dict:
    matches = [p for p in plan_catalog if p.name == name]
    if not matches:
        return {"found": False, "name": name}
    return {
        "found": True,
        "matches": [
            {
                "name": p.name,
                "summary": p.summary,
                "file": p.file,
                "files": list(p.files),
                "module": p.module,
                "tier": p.tier,
                "documented": p.documented,
                "params": [_plan_param_detail(param) for param in p.params],
                "skeleton": list(p.skeleton) if p.skeleton else None,
                "has_varargs": p.has_varargs,
            }
            for p in matches
        ],
    }


def list_scan_building_blocks(blocks: dict) -> dict:
    return blocks


def build_read_plan_file_schema() -> dict:
    return {
        "name": READ_PLAN_FILE_TOOL_NAME,
        "description": (
            "Read a Python file of bluesky plan functions and report each "
            "plan's real signature (every argument name/default) and its "
            "current docstring (verbatim, or null if missing), plus whether "
            "it already parses as B-PILOT-compliant. Use this before "
            "drafting or fixing a docstring for a real file -- never invent "
            "a function's signature or guess at its existing docstring. "
            "Path must be a .py file inside instrument/plans/; files "
            "elsewhere (e.g. iconfig.yml) cannot be read with this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the .py file, absolute or relative to instrument/plans/.",
                }
            },
            "required": ["path"],
        },
    }


def build_validate_docstring_schema() -> dict:
    return {
        "name": VALIDATE_DOCSTRING_TOOL_NAME,
        "description": (
            "Check one or more drafted docstrings against B-PILOT's exact "
            "parameter grammar before presenting them -- catches unknown "
            "dtypes, a documented parameter name that doesn't match the "
            "real signature, and a missing short/long ' :: ' split. Call "
            "this on every docstring you draft for a real file before your "
            "final reply, and fix and re-check anything it flags."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "drafts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "function_name": {"type": "string"},
                            "arg_names": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Every real argument name from the function's signature (from read_plan_file), in any order.",
                            },
                            "docstring": {
                                "type": "string",
                                "description": "The full drafted docstring text (opening summary paragraph plus a Parameters section).",
                            },
                        },
                        "required": ["function_name", "arg_names", "docstring"],
                    },
                }
            },
            "required": ["drafts"],
        },
    }


def _resolve_plan_path(path: str) -> Path | None:
    """Resolve `path` against instrument/plans/, refusing anything outside it.

    Deliberately scoped to `PLANS_DIR`, not the whole Bluesky root -- this
    tool must never be able to reach `instrument/iconfig.yml`, which holds
    live plaintext MongoDB credentials.
    """
    root = Path(bpilot_paths.PLANS_DIR).resolve()
    candidate = Path(path)
    candidate = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if resolved.suffix != ".py" or not resolved.is_file():
        return None
    return resolved


def read_plan_file(path: str) -> dict:
    resolved = _resolve_plan_path(path)
    if resolved is None:
        return {"error": f"'{path}' is not a readable .py file inside instrument/plans/."}
    functions = bpilot_plan_parser.find_plan_functions_raw(str(resolved))
    if not functions:
        return {
            "path": str(resolved),
            "functions": [],
            "note": "No top-level plan functions detected (no __all__ and no generator functions found).",
        }
    return {"path": str(resolved), "functions": functions}


def validate_docstring(drafts: list[dict]) -> dict:
    results = []
    for draft in drafts:
        check = bpilot_plan_parser.validate_docstring_text(draft.get("docstring", ""), draft.get("arg_names"))
        results.append({"function_name": draft.get("function_name"), **check})
    return {"results": results}


def build_search_runs_schema() -> dict:
    return {
        "name": SEARCH_RUNS_TOOL_NAME,
        "description": (
            "Search runs already recorded in this beamline's data catalog "
            "(the active profile's configured databroker catalog -- never a "
            "raw URI or credential). Read-only historical lookup, newest "
            "first; never affects or executes anything. Use this to answer "
            "questions like 'how many expose runs aborted' or 'what scans "
            "ran yesterday' before guessing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_name": {
                    "type": "string",
                    "description": "Exact plan name to filter by, e.g. 'expose' or 'mpe_step_grid_scan'.",
                },
                "exit_status": {
                    "type": "string",
                    "enum": ["success", "abort", "fail"],
                    "description": "Filter by how the run ended.",
                },
                "scan_id": {
                    "type": "integer",
                    "description": "Filter to one specific scan_id.",
                },
                "since": {
                    "type": "string",
                    "description": "ISO date/time lower bound, e.g. '2026-07-01'.",
                },
                "until": {
                    "type": "string",
                    "description": "ISO date/time upper bound, e.g. '2026-07-31'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max runs to return, default 20, capped at 100.",
                },
            },
            "required": [],
        },
    }


def build_describe_run_schema() -> dict:
    return {
        "name": DESCRIBE_RUN_TOOL_NAME,
        "description": (
            "Get full metadata for one already-recorded run: start/stop "
            "documents (noisy internal fields stripped), stream names, and "
            "event counts. Use this before answering any question about a "
            "specific run's parameters or outcome."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Full or partial run uid, or a numeric scan_id, as a string.",
                }
            },
            "required": ["run_id"],
        },
    }


def build_read_run_data_schema() -> dict:
    return {
        "name": READ_RUN_DATA_TOOL_NAME,
        "description": (
            "Summarize one already-recorded run's scalar data stream: "
            "per-column min/max/mean/first/last plus a short head/tail "
            "preview. Never returns raw per-event data in full -- use "
            "columns from describe_run's motor/detector list to narrow "
            "large streams. Use this to answer questions about what a "
            "motor or detector actually did during a run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Full or partial run uid, or a numeric scan_id, as a string.",
                },
                "stream": {
                    "type": "string",
                    "description": "Stream name, default 'primary'.",
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional column names to restrict the summary to.",
                },
            },
            "required": ["run_id"],
        },
    }


def search_runs(profile: str | None, **kwargs) -> dict:
    return data_catalog.search_runs(profile, **kwargs)


def describe_run(profile: str | None, run_id: str) -> dict:
    return data_catalog.describe_run(profile, run_id)


def read_run_data(profile: str | None, run_id: str, stream: str = "primary", columns: list | None = None) -> dict:
    return data_catalog.read_run_data(profile, run_id, stream=stream, columns=columns)


# ---------------------------------------------------------------------------
# General project-wide knowledge tools: list_directory / search_codebase /
# read_source_file. Scoped to the whole mpe_bluesky checkout
# (bpilot_paths.BLUESKY_ROOT), not just instrument/plans/ like read_plan_file
# above -- these exist so AutoPILOT can answer free-form questions about
# anything in the project (GUI widgets, docs, plan internals) instead of only
# the narrow slices the structured tools above cover.
# ---------------------------------------------------------------------------

_TEXT_EXTENSIONS = {
    ".py", ".md", ".txt", ".yml", ".yaml", ".json", ".cfg", ".ini", ".toml", ".sh", ".rst",
}

# Directories never listed, searched, or read into.
_EXCLUDED_DIR_NAMES = {
    ".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache",
    ".idea", ".vscode", "dist", "build",
    # Gitignored raw experiment-data dump (uids, plan args, instrument config) --
    # real run data, not codebase/GUI documentation; search_runs/describe_run/
    # read_run_data above already cover actual run-data questions.
    "hexm_export",
}
_EXCLUDED_DIR_SUFFIXES = (".egg-info",)

# Exact files never listed, searched, or read, beyond the extension/dir filters
# above -- resolved once at import time.
_DENIED_PATHS = {
    Path(bpilot_paths.ICONFIG).resolve(),  # live plaintext MongoDB credentials
    autopilot_settings.SETTINGS_PATH.resolve(),  # may hold a plaintext Argo API key override
}

# Filename patterns denylisted anywhere in the tree, defense-in-depth against
# a future secret file that isn't one of the two known ones above.
_DENIED_NAME_PATTERNS = ("*.env", "*secret*", "*credential*", "*password*", "id_rsa*", "*.pem", "*.key")

# Redacts credentialed connection strings (e.g. "mongodb://user:pass@host") from
# any line before it leaves search_codebase/read_source_file -- a couple of real
# plan files under instrument/plans/ hardcode live Mongo credentials as source
# text, which no path/filename denylist above would catch (ordinary .py names).
_CREDENTIAL_URL_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/:@]+:[^\s/:@]+@")


def redact(text: str) -> str:
    return _CREDENTIAL_URL_RE.sub(r"\1***:***@", text)


def _is_denied_name(name: str) -> bool:
    return any(fnmatch.fnmatch(name.lower(), pat) for pat in _DENIED_NAME_PATTERNS)


def _resolve_project_path(path: str | None) -> Path | None:
    """Resolve `path` against BLUESKY_ROOT, refusing anything outside it, any
    denylisted file, or anything matching a denylisted name pattern anywhere
    along the path.

    Mirrors `_resolve_plan_path` above but rooted at the whole project
    instead of just instrument/plans/, since these tools' whole purpose is
    project-wide knowledge -- see the module-level denylists for what stays
    excluded regardless.
    """
    root = Path(bpilot_paths.BLUESKY_ROOT).resolve()
    candidate = Path(path) if path else root
    candidate = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = candidate.resolve()
        rel_parts = resolved.relative_to(root).parts
    except (OSError, ValueError):
        return None
    for part in rel_parts[:-1]:
        if part in _EXCLUDED_DIR_NAMES or part.endswith(_EXCLUDED_DIR_SUFFIXES) or _is_denied_name(part):
            return None
    if resolved in _DENIED_PATHS or _is_denied_name(resolved.name):
        return None
    return resolved


def build_list_directory_schema() -> dict:
    return {
        "name": LIST_DIRECTORY_TOOL_NAME,
        "description": (
            "List the immediate files and subdirectories of a directory in "
            "the mpe_bluesky project (GUI code, instrument plans, docs, "
            "READMEs -- everything except a small denylist of sensitive "
            "files/dirs). Use this to orient yourself before searching or "
            "reading, e.g. to see what's inside B_PILOT/ or B-PILOT/documents/."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Directory path, relative to the mpe_bluesky Bluesky root "
                        "(e.g. 'B-PILOT/B_PILOT') or absolute. Omit to list the "
                        "Bluesky root itself."
                    ),
                }
            },
            "required": [],
        },
    }


def build_search_codebase_schema() -> dict:
    return {
        "name": SEARCH_CODEBASE_TOOL_NAME,
        "description": (
            "Case-insensitive substring search over text files (.py, .md, "
            ".json, .yml, etc.) anywhere in the mpe_bluesky project -- GUI "
            "code, instrument plans, docs. Use this to find where something "
            "is implemented or documented (e.g. a GUI button's name like "
            "'BEAMMODE') before answering, instead of guessing. Returns "
            "matching lines with file path and line number; call "
            "read_source_file on a promising match for full context. Some "
            "files/directories are excluded (e.g. instrument/iconfig.yml, "
            "AutoPILOT's own settings file) and credentialed connection "
            "strings are always redacted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring to search for, case-insensitive.",
                },
                "path_prefix": {
                    "type": "string",
                    "description": "Optional subtree to restrict the search to, e.g. 'B-PILOT/B_PILOT'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max matches to return, default 40, capped at 200.",
                },
            },
            "required": ["query"],
        },
    }


def build_read_source_file_schema() -> dict:
    return {
        "name": READ_SOURCE_FILE_TOOL_NAME,
        "description": (
            "Read the text of one file anywhere in the mpe_bluesky project "
            "(GUI code, instrument plans, docs, READMEs). Use this to get "
            "full context after search_codebase points you at a promising "
            "file -- never guess at a file's contents. Large files are "
            "capped; use start_line/end_line to page through them. Some "
            "files are excluded (e.g. instrument/iconfig.yml, AutoPILOT's "
            "own settings file) and credentialed connection strings are "
            "always redacted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path, relative to the mpe_bluesky Bluesky root or absolute.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "1-indexed first line to return. Omit to start from the beginning.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "1-indexed last line to return (inclusive). Omit to read to the cap.",
                },
            },
            "required": ["path"],
        },
    }


def list_directory(path: str | None) -> dict:
    resolved = _resolve_project_path(path)
    if resolved is None or not resolved.is_dir():
        return {"error": f"'{path or '.'}' is not a listable directory in this project."}
    root = Path(bpilot_paths.BLUESKY_ROOT).resolve()
    entries = []
    for entry in sorted(os.scandir(resolved), key=lambda e: e.name.lower()):
        if entry.is_dir():
            if entry.name in _EXCLUDED_DIR_NAMES or entry.name.endswith(_EXCLUDED_DIR_SUFFIXES) or _is_denied_name(entry.name):
                continue
            entries.append({"name": entry.name, "type": "dir"})
        else:
            entry_path = Path(entry.path).resolve()
            if entry_path in _DENIED_PATHS or _is_denied_name(entry.name):
                continue
            entries.append({"name": entry.name, "type": "file", "size": entry.stat().st_size})
    return {"path": str(resolved.relative_to(root)) or ".", "entries": entries}


def _iter_text_files(start: Path, root: Path):
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = [
            d for d in dirnames
            if d not in _EXCLUDED_DIR_NAMES and not d.endswith(_EXCLUDED_DIR_SUFFIXES) and not _is_denied_name(d)
        ]
        for filename in filenames:
            candidate = Path(dirpath) / filename
            if candidate.suffix.lower() not in _TEXT_EXTENSIONS:
                continue
            if candidate.resolve() in _DENIED_PATHS or _is_denied_name(filename):
                continue
            yield candidate


_MAX_MATCH_LINE_CHARS = 300
_COLLECT_CAP = 5000


def search_codebase(query: str, path_prefix: str | None, limit: int | None) -> dict:
    if not query:
        return {"error": "query must not be empty."}
    root = Path(bpilot_paths.BLUESKY_ROOT).resolve()
    start = _resolve_project_path(path_prefix) if path_prefix else root
    if start is None or not start.is_dir():
        return {"error": f"'{path_prefix}' is not a searchable directory in this project."}
    cap = min(max(limit or 40, 1), 200)
    query_lower = query.lower()
    collected = []
    collect_truncated = False
    for file_path in _iter_text_files(start, root):
        try:
            lines = file_path.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        rel_file = str(file_path.relative_to(root))
        for lineno, line in enumerate(lines, start=1):
            if query_lower in line.lower():
                text = redact(line.strip())
                if len(text) > _MAX_MATCH_LINE_CHARS:
                    text = text[:_MAX_MATCH_LINE_CHARS] + "…"
                collected.append({"file": rel_file, "line": lineno, "text": text})
                if len(collected) >= _COLLECT_CAP:
                    collect_truncated = True
                    break
        if collect_truncated:
            break

    if len(collected) <= cap:
        return {"query": query, "matches": collected, "truncated": collect_truncated}

    # More matches than the display cap: a single noisy subtree (e.g.
    # instrument/, with dozens of hits) could otherwise starve out other
    # subtrees (e.g. B-PILOT/) before they're ever represented. Group by
    # top-level path component (relative to the searched subtree) and
    # interleave round-robin so every group gets a fair share.
    start_rel_parts = start.relative_to(root).parts
    groups: dict[str, list] = {}
    for match in collected:
        file_parts = Path(match["file"]).parts
        key_parts = file_parts[len(start_rel_parts):]
        key = key_parts[0] if key_parts else match["file"]
        groups.setdefault(key, []).append(match)
    interleaved = [
        m for group in itertools.zip_longest(*groups.values()) for m in group if m is not None
    ]
    return {"query": query, "matches": interleaved[:cap], "truncated": True}


_MAX_READ_LINES = 4000
_MAX_READ_CHARS = 150_000


def _coerce_line_arg(value) -> int | None:
    """Best-effort int coercion for a declared-integer tool arg -- the model
    occasionally emits a numeric string instead. Anything non-coercible (or
    None) is treated as unset rather than raising."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_source_file(path: str, start_line: int | None, end_line: int | None) -> dict:
    resolved = _resolve_project_path(path)
    if resolved is None or not resolved.is_file() or resolved.suffix.lower() not in _TEXT_EXTENSIONS:
        return {"error": f"'{path}' is not a readable text file in this project."}
    root = Path(bpilot_paths.BLUESKY_ROOT).resolve()
    try:
        lines = resolved.read_text(errors="ignore").splitlines()
    except OSError as exc:
        return {"error": f"Could not read '{path}': {exc}"}
    total_lines = len(lines)
    start_idx = max((_coerce_line_arg(start_line) or 1) - 1, 0)
    end_idx = min(_coerce_line_arg(end_line) or total_lines, total_lines)
    selected = lines[start_idx:end_idx]
    truncated = False
    if len(selected) > _MAX_READ_LINES:
        selected = selected[:_MAX_READ_LINES]
        truncated = True
    text = redact("\n".join(selected))
    if len(text) > _MAX_READ_CHARS:
        text = text[:_MAX_READ_CHARS]
        truncated = True
    return {
        "path": str(resolved.relative_to(root)),
        "start_line": start_idx + 1,
        "end_line": start_idx + len(selected),
        "total_lines": total_lines,
        "truncated": truncated,
        "text": text,
    }


# ── Experiment report ────────────────────────────────────────────────────────
# B-PILOT's per-experiment lab record (see B_PILOT/report_store.py). Read-only,
# deliberately: AutoPILOT has NO tool that writes to the report. Anything it
# contributes goes through the chat dock's "Add to report" button, so a person
# has seen the text before it enters the record. A lab notebook the agent could
# append to unattended is not a record anyone should trust.

# A long beamtime's report can run to many pages; the model only ever needs the
# recent end of it, and an unbounded read would blow the context window.
_REPORT_MAX_CHARS = 20000


def build_read_experiment_report_schema() -> dict:
    return {
        "name": READ_EXPERIMENT_REPORT_TOOL_NAME,
        "description": (
            "Read the current experiment report -- B-PILOT's running lab record "
            "of this beamtime: every plan that ran with its outcome, the notes "
            "the user attached, and any beamline snapshots they captured. Use "
            "this to answer questions about what has happened in THIS session "
            "('what did we run this morning?', 'did the last grid scan work?', "
            "'summarise today'). It reflects what actually reached the kernel, "
            "so prefer it over search_runs for recent activity that the data "
            "catalog may not have ingested yet. Returns Markdown."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "experiment": {
                    "type": "string",
                    "description": (
                        "Experiment name. Omit for the session's current "
                        "experiment, which is almost always what is wanted."
                    ),
                }
            },
            "required": [],
        },
    }


def list_experiment_reports() -> dict:
    """Experiments on this beamline that have a report, most recent first."""
    from B_PILOT import experiment_history as bpilot_history

    beamline = bpilot_config.as_dict().get("beamline") or ""
    return {
        "beamline": beamline,
        "experiments": [e.get("name") for e in bpilot_history.list_experiments(beamline)],
    }


def read_experiment_report(experiment: str | None = None) -> dict:
    """The report Markdown for `experiment` (default: the current one).

    Falls back to building it on the fly when ``report.md`` has not been
    written yet -- the file only appears once the report panel has rendered
    once, and a chat question should not depend on whether the user happened
    to have that dock open.
    """
    from B_PILOT import experiment_history as bpilot_history
    from B_PILOT import report_builder as bpilot_report_builder
    from B_PILOT import report_store as bpilot_report_store

    cfg = bpilot_config.as_dict()
    beamline = cfg.get("beamline") or ""
    if not experiment:
        experiment = bpilot_report_store.current_experiment(beamline)
        if not experiment:
            return {"error": "No experiment history exists for this beamline yet."}

    markdown = bpilot_report_store.read_markdown(beamline, experiment)
    if not markdown.strip():
        markdown = bpilot_report_builder.render_markdown(
            bpilot_history.read_entries(beamline, experiment),
            bpilot_report_store.read_events(beamline, experiment),
            experiment=experiment,
            beamline=beamline,
            title=cfg.get("report_title") or "",
        )

    truncated = len(markdown) > _REPORT_MAX_CHARS
    if truncated:
        # Keep the END: a question about "this session" is about recent work.
        markdown = "...(earlier entries omitted)...\n" + markdown[-_REPORT_MAX_CHARS:]
    return {
        "beamline": beamline,
        "experiment": experiment,
        "truncated": truncated,
        # Same credential-URL scrubbing every other tool output gets.
        "report_markdown": redact(markdown),
    }
