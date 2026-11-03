# Agent instructions

## Project context

**B-PILOT** ("Bluesky-PILOT") is a PyQt GUI for composing, queuing, and
monitoring Bluesky plans on the **MPE beamlines** (APS Sectors 1 and 20:
1-ID and HEXM / 20-ID), plus a databroker-backed data viewer. It wraps the
`mpe_bluesky` instrument package (`instrument/`) — this is a GUI layer, not
the instrument codebase itself. Never connect to hardware or EPICS PVs; edit
source only. On the beamline workstation this extends to: never launch the
kernel against hardware, never run plans/the RunEngine, never click "Launch
IPython"/"Load Bluesky"/run the queue — only the user does that (see
`.context/DEPLOY.md`'s SAFETY section for the full on-site checklist).

This is its own git repo (pushed to `github.com/d-beniwal/b-pilot` and to
`git.aps.anl.gov/dbeniwal/b-pilot`), a **sibling** of the `mpe_bluesky`
checkout since 2026-09-02 (it used to be nested inside it, and `mpe_bluesky`
still gitignores the old path so the two histories stay independent).

For the underlying `instrument`/queueserver mechanics B-PILOT wraps (not
duplicated here), see `mpe_bluesky/AGENTS.md` and
`mpe_bluesky/.context/ARCHITECTURE.md`. For any Bluesky work, use the global
**`bluesky` skill** and keep its memory current.

## At the start of a task

Read `.context/STATE.md` for the current focus, next steps, blockers, and
recent changes.

Read only the relevant portions of the on-demand references below when the
task actually needs that background — do not bulk-read them:

- `README.md`                — package layout, how to launch, env setup
- `.context/ARCHITECTURE.md` — layout, launch-mode internals, path
  resolution, beamline profiles + device discovery, and the full AutoPILOT
  integration contract
- `.context/DECISIONS.md`    — append-only log of decisions and *why*
  (newest first; merges B-PILOT's and AutoPILOT's former separate logs)
- `.context/DOMAIN.md`       — MPE domain knowledge B-PILOT depends on
  (scan-building-block catalog, beamline account/catalog facts)
- `.context/DEPLOY.md`       — beamline bring-up checklist (prereqs, on-site
  config, the on-beamline safety rule). Read this before deploying/running
  on the beamline workstation.
- `.context/PORTING_NOTES.md` — the original 3-ID-C → MPE porting diary
  (why the parser grammar, catalog, and startup logic look the way they do)
- `.context/AUTO_INTEGRATE_PLAN.md` — design (not yet built) for a
  subprocess-based bridge to midas-gui's `batch_cli` that auto-integrates
  each scan's detector HDF5 into h5/zarr, analogous to the live-view bridge
- `.context/HIGHLIGHTS.md`   — presentation-ready challenge→solution log
  (accessibility / security / robustness). **Maintain it:** whenever a
  change touches one of those pillars, add a terse Challenge→Solution→Impact
  entry.

## After significant work

Update `.context/STATE.md` (keep it concise, under ~1 page) and prepend a
dated entry with reasoning to `.context/DECISIONS.md`. Do not overwrite prior
decisions. "Significant work" = a milestone/feature finished, a design or
architecture decision made, a bug root-caused, or a plan/approach changed —
trivial edits, typo fixes, or pure Q&A don't need an update.

## AutoPILOT (optional subsystem, lives in this same repo)

`AutoPILOT/` is an optional agentic AI layer (natural-language plan-drafting,
lookup tools over devices/plans/docstrings) embedded as a chat dock inside
B-PILOT's own window. It was a separate git repo until 2026-07-31, when it
was folded into this repo as a tracked subdirectory — it is **not** a
separate project anymore, and its docs live here, in this project's own
`.context/`, not in a folder of their own. **B-PILOT must always run
standalone with `AutoPILOT/` absent** — the dependency arrow points
AutoPILOT → B-PILOT only, via one guarded `try/except ImportError`
(`B_PILOT/autopilot_bridge.py`). See `.context/ARCHITECTURE.md`'s "AutoPILOT"
section for the full contract, integration surface, and pipeline design.
