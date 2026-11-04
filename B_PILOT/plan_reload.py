"""Hot-reload of already-imported plan modules inside the live IPython kernel.

The GUI never imports plans itself -- the kernel does, at startup
(``from instrument.collection import *``) -- so after a plan file is edited the
kernel keeps running the OLD function objects until it is restarted.  This
module provides :func:`_bpilot_reload_plans`, a self-contained function that is
shipped as *source* to the kernel (see :func:`kernel_source` /
:func:`call_source`) and does the reload there.

Design (see .context/DECISIONS.md, 2026-11-04):

* Only modules whose file lives under one of the given ``roots`` (the plans
  directory) are touched.  Devices, ophyd, the RunEngine and the instrument
  framework are never reloaded -- that would duplicate device classes/instances
  and orphan ``RE``/``oregistry`` references.
* Only modules whose source changed since they were last loaded are reloaded,
  plus modules that reference them (``from changed import f`` copies the old
  ``f``), in dependency order (dependencies first).
* Afterwards every name in the user namespace that is a function/class from a
  reloaded module is rebound to the new object, and new public functions
  defined in reloaded modules are added (never overwriting an existing name).
* A module that fails to reload (syntax error, exception on import) is
  restored to its previous namespace and reported; its dependents are skipped.
* Refuses to run unless ``RE.state`` is ``'idle'`` (checked in the kernel, so
  the check and the reload are atomic).

The function body must stay self-contained (stdlib imports inside it, no
references to module globals) because only its source text reaches the kernel.
"""
from __future__ import annotations

import inspect

_ENTRY = "_bpilot_reload_plans"


def _bpilot_reload_plans(roots, *, ns=None, require_idle=True, out=print):
    """Reload changed plan modules under `roots`; see the module docstring.

    `ns` is the namespace to rebind names in (default: the IPython user
    namespace).  Returns a dict summary; prints a human-readable report via
    `out`.
    """
    import importlib
    import linecache
    import os
    import sys
    import types

    summary = {"reloaded": [], "failed": {}, "skipped": [], "rebound": 0, "added": 0,
               "refused": None}

    # ── Guards ────────────────────────────────────────────────────────────────
    if ns is None:
        ip = None
        try:
            ip = get_ipython()  # noqa: F821  (defined inside IPython kernels)
        except NameError:
            pass
        ns = ip.user_ns if ip is not None else {}
    if require_idle:
        re_obj = ns.get("RE")
        if re_obj is None:
            summary["refused"] = "no RunEngine (`RE`) in the namespace; cannot confirm it is idle"
        else:
            try:
                state = str(re_obj.state)
            except Exception as exc:  # noqa: BLE001
                state = f"unreadable ({exc})"
            if state != "idle":
                summary["refused"] = f"RunEngine state is '{state}', not 'idle'"
        if summary["refused"]:
            out(f"[reload] REFUSED: {summary['refused']}. Nothing was reloaded.")
            return summary

    # ── Candidate modules: files under `roots` ────────────────────────────────
    real_roots = [os.path.realpath(r) + os.sep for r in roots if r]

    def _under_roots(path):
        rp = os.path.realpath(path)
        return any(rp.startswith(r) for r in real_roots)

    cands = {}
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if (isinstance(mod, types.ModuleType) and isinstance(f, str)
                and f.endswith(".py") and _under_roots(f)):
            cands[name] = mod
    if not cands:
        out("[reload] No imported plan modules found under: " + ", ".join(roots))
        return summary

    # Per-kernel record of the source mtime each module was last loaded at.
    # Modules never reloaded fall back to the kernel process start time.
    stamps = ns.setdefault("_bpilot_reload_stamps", {})
    try:
        import psutil
        start = psutil.Process().create_time()
    except Exception:  # noqa: BLE001
        start = 0.0

    def _mtime(mod):
        try:
            return os.path.getmtime(mod.__file__)
        except OSError:
            return 0.0

    changed = {n for n, m in cands.items() if _mtime(m) > stamps.get(n, start)}
    if not changed:
        out("[reload] No plan-file changes detected; nothing to reload.")
        return summary

    # ── Dependencies between candidates (via names living in module dicts) ───
    def _deps(mod):
        found = set()
        for val in list(vars(mod).values()):
            if isinstance(val, types.ModuleType):
                dep = val.__name__
            else:
                try:
                    dep = getattr(val, "__module__", None)
                except Exception:  # noqa: BLE001
                    dep = None
            if isinstance(dep, str) and dep != mod.__name__ and dep in cands:
                found.add(dep)
        return found

    deps = {n: _deps(m) for n, m in cands.items()}

    todo = set(changed)
    grew = True
    while grew:  # transitive dependents of anything changed
        grew = False
        for n in cands:
            if n not in todo and deps[n] & todo:
                todo.add(n)
                grew = True

    order, seen = [], set()

    def _visit(n):  # dependencies first; `seen` also breaks import cycles
        if n in seen:
            return
        seen.add(n)
        for d in sorted(deps[n] & todo):
            _visit(d)
        order.append(n)

    for n in sorted(todo):
        _visit(n)

    # ── Reload ────────────────────────────────────────────────────────────────
    importlib.invalidate_caches()
    linecache.checkcache()
    failed = summary["failed"]
    for n in order:
        mod = cands[n]
        bad = sorted(d for d in deps[n] if d in failed or d in summary["skipped"])
        if bad:
            summary["skipped"].append(n)
            out(f"[reload] SKIPPED {n} (depends on failed module: {', '.join(bad)})")
            continue
        snapshot = dict(vars(mod))
        try:
            try:  # same-second edits with same size would otherwise hit a stale .pyc
                from importlib.util import cache_from_source
                os.remove(cache_from_source(mod.__file__))
            except (OSError, NotImplementedError, ValueError):
                pass
            importlib.reload(mod)
        except BaseException as exc:  # noqa: BLE001  (incl. SyntaxError/SystemExit)
            vars(mod).clear()
            vars(mod).update(snapshot)
            failed[n] = f"{type(exc).__name__}: {exc}"
            out(f"[reload] FAILED {n}: {failed[n]} (previous version kept)")
            continue
        stamps[n] = _mtime(mod)
        summary["reloaded"].append(n)

    # ── Rebind names in the user namespace ────────────────────────────────────
    reloaded = {n: cands[n] for n in summary["reloaded"]}
    if reloaded:
        for key, val in list(ns.items()):
            if key.startswith("_") or not isinstance(val, (types.FunctionType, type)):
                continue
            home = getattr(val, "__module__", None)
            if home not in reloaded:
                continue
            new = getattr(reloaded[home], getattr(val, "__name__", key), None)
            if new is not None and new is not val and callable(new):
                ns[key] = new
                summary["rebound"] += 1
        # New public functions defined in a reloaded module (never clobbers).
        for n, mod in reloaded.items():
            public = getattr(mod, "__all__", None)
            for key, val in list(vars(mod).items()):
                if key.startswith("_") or key in ns:
                    continue
                if public is not None and key not in public:
                    continue
                if isinstance(val, types.FunctionType) and val.__module__ == n:
                    ns[key] = val
                    summary["added"] += 1

    # ── Report ────────────────────────────────────────────────────────────────
    if summary["reloaded"]:
        short = ", ".join(summary["reloaded"])
        out(f"[reload] Reloaded {len(summary['reloaded'])} module(s): {short}")
        out(f"[reload] Rebound {summary['rebound']} name(s); added {summary['added']} new plan(s).")
    if failed or summary["skipped"]:
        out(f"[reload] {len(failed)} failed, {len(summary['skipped'])} skipped -- "
            "fix the file and click Reload again.")
    return summary


def kernel_source() -> str:
    """Source text of :func:`_bpilot_reload_plans` (defines it in the kernel)."""
    return inspect.getsource(_bpilot_reload_plans)


def call_source(roots: list[str]) -> str:
    """The one-line, visible console cell that runs the reload."""
    # Trailing `;` stops IPython echoing the returned summary dict as Out[n].
    return f"{_ENTRY}({[str(r) for r in roots]!r});"
