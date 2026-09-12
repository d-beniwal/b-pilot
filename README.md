# B-PILOT (Bluesky-PILOT)
## Bluesky (B) - Plan Interface for Launch, Operation & Tracking (PILOT)

A PyQt5 GUI for running [Bluesky](https://blueskyproject.io/) plans at APS
beamlines: point it at a plan module, get a parameter form generated straight
from the plan's docstring, run it in a live embedded IPython console, queue up
a sequence of runs, and page through the resulting data — all from one
window.

Built for the MPE beamlines (APS Sectors 1 and 20 — 1-ID and HEXM/20-ID), and
designed to generalize to other beamlines' Bluesky instrument packages over
time.

## Features

- **Docstring-driven parameter forms** — no plan-specific GUI code to write.
  Document a plan's arguments in a small NumPy-style `Parameters` grammar
  (see `B_PILOT/plan_parser.py`) and B-PILOT builds a typed form for it
  automatically: text/number fields with live validation, dropdowns for
  `choice{...}` options, and device pickers for `device{category}` /
  `device_list{category}` arguments. Plan files are read with `ast` only —
  **never imported** — so building the form never touches EPICS or connects
  to hardware.
- **Embedded IPython console** — a persistent, detachable Bluesky kernel
  (single instance per beamline, hosted in `screen`) that survives closing
  and reopening the GUI, with a full session transcript.
- **Run queue** — build up a sequence of plan invocations, run them
  unattended, and track status per item.
- **Run controls** — pause/resume/stop/abort the RunEngine from the toolbar
  without switching to a terminal.
- **Data viewer** — browse runs from a `databroker` catalog.
- **Remote report mirror** *(optional)* — publish a read-only copy of an
  experiment report so collaborators off-site can follow it. B-PILOT only ever
  pushes *outward*; the workstation opens no port, so a remote reader has no
  access to it or to the instrument. Off by default and per-experiment opt-in,
  and it additionally requires a credential in the environment. Published as
  a **Google Doc** — no hosting needed, but readers refresh rather than watch,
  updates are held to one every 30 s, and Drive keeps earlier revisions of
  what was published (so hiding an entry is not retroactive there); figures
  are carried into the document. See `documents/GDOCS_SETUP.md`. A workstation
  with no route to the internet at all can instead write to a **shared
  outbox** and let a relay on a machine that does have internet publish from
  there — see `report_relay/README.md`.
- **Configurable plan scope** — a Configuration dialog controls which
  directory is scanned for plans, which files are even shown in the file
  browser ("Plan visibility," with select-all/deselect-all), and what startup
  command loads the beamline's device/plan session.

## Requirements

A PyQt5 + Bluesky/ophyd environment. The environment this was developed and
verified against ships with this repo at `environments/bpilot-mpe.yml`
(PyQt5, qtconsole, ipykernel, bluesky, ophyd, databroker, queueserver, etc.,
python 3.11) — the beamline-capable variant, including `epics-base`/`hklpy`/
`aps-dm-api` so `instrument.collection` fully imports on the beamline
workstation. Create it with:

```bash
conda env create -f environments/bpilot-mpe.yml
conda activate bpilot-mpe
```

### Running against the beamline runtime env instead

If you'd rather run B-PILOT inside the beamline's existing Bluesky env
(`environment_2024_1.yml` in the parent workspace, the one the queueserver
and `instrument.collection` actually run under) rather than
`bpilot-mpe`, it's missing one package the GUI needs:

- **`qtconsole`** (pulls in `QtPy`) — powers the embedded IPython console
  (`B_PILOT/console_panel.py`). `environment_2024_1.yml` only installs
  `pyqt =5` / `qt =5`, and nothing else in that env depends on `qtconsole`,
  so it has to be added explicitly, e.g. `pip install qtconsole`.

Everything else B-PILOT imports (`PyQt5`, `tiled`, `databroker`, and the
Jupyter messaging stack — `jupyter_client`, `pyzmq`, `traitlets` — via
`ipykernel`) is already covered by `environment_2024_1.yml`, directly or
transitively.

## Running it

From inside this directory:

```bash
python launch.py
```

equivalently:

```bash
python -m B_PILOT
```

## Where it needs to live

B-PILOT auto-discovers the Bluesky instrument package it belongs to by
walking **up** from its own location looking for an `instrument/` directory
alongside a `blueskyStarter.sh` or `qserver.sh` script (see
`B_PILOT/paths.py`). That means B-PILOT should sit as a subfolder directly
inside a beamline's Bluesky root, e.g.:

```
<beamline-bluesky-project>/
├── instrument/
├── blueskyStarter.sh (or qserver.sh)
└── B-PILOT/            <- this repo
    ├── launch.py
    └── B_PILOT/
```

Everything else (which directory holds plans, where the device manifest is,
where runtime/session state is kept) is derived from that discovery — no
hard-coded absolute paths, so the same checkout works unmodified regardless
of where the parent project lives on disk.

## Configuring which plans show up

Open **Python → Configuration…** in the menu bar:

- **Files** — the plans directory scanned, the import root used to build the
  generated `from <module> import <plan>` line, and the default file checked
  on startup.
- **Plan visibility** — every `.py` file found under the plans directory,
  with a checkbox for whether it appears as a row in the main window's file
  browser at all (Select all / Deselect all / Refresh list). This is separate
  from the per-row checkbox in the main panel, which controls whether a
  *visible* file's plans are merged into the plan dropdown.
- **Launch** — the command(s) run automatically in the console right after
  "Launch IPython" connects (e.g. `from instrument.collection import *`),
  and whether the kernel is kept alive when the GUI closes.

## Connecting to the embedded kernel from a terminal

The embedded console (see Features) is a real Jupyter kernel, not something
private to the GUI — anything that speaks the Jupyter messaging protocol can
attach to it from any terminal or `screen` session, independent of the GUI
(it works even after you close the GUI, since the kernel is detached and
survives GUI exit).

Each beamline's kernel keeps its connection file at a fixed, predictable path
(`B_PILOT/kernel_session.py`):

```
~/.bluesky_pilot/<beamline>/kernel.json
```

`<beamline>` is the **profile folder name** — so the `s20idd` profile is always
`~/.bluesky_pilot/s20idd/kernel.json`. Neither half of that path is
configurable: both are derived (`config._PINNED_KEYS`), and the beamline id is
shown read-only in the Configuration dialog. They used to be editable settings
and drifted apart from the profile, which left the documented path pointing at a
kernel that no longer existed. Point any Jupyter client at the file with
`--existing <path>`:

```bash
jupyter qtconsole --existing ~/.bluesky_pilot/s20idd/kernel.json
jupyter console   --existing ~/.bluesky_pilot/s20idd/kernel.json
```

If a client reports `kernel died: 3.0…` immediately, it reached the file but
nothing answered on its heartbeat port — that connection file is stale, not a
crashed kernel. Ask the code where the live one is:

```bash
python -m B_PILOT.kernel_session status --beamline s20idd   # prints connection_file + alive
```

This works from any terminal or `screen` session — it doesn't need to be the
one that started the kernel, and you don't need `screen -r` for it. The
connection file carries everything a client needs to connect (ports, IP,
HMAC key, signature scheme). You may notice its `kernel_name` field is blank
— that's expected: B-PILOT starts the kernel directly via
`ipykernel_launcher` (`starter_scripts/mpe.sh`),
bypassing the Jupyter
kernelspec lookup that normally fills that field in, and it has no effect on
connecting.

Multiple clients — the GUI's own console panel, a standalone `qtconsole`, a
`jupyter console` — can all attach to the same kernel at once; they share one
live Bluesky/RunEngine session.

To instead watch the raw process (stdout, tracebacks — not a Jupyter
client), reattach to the `screen` session hosting it:

```bash
screen -r bluesky-kernel-s20idd
```

## Status

Actively developed for MPE (Sectors 1/20). The plan-parsing grammar,
device-picker, queue, and console machinery are beamline-agnostic by
construction, and every beamline-specific setting — plans directory/
visibility, launch/session commands, device search paths, appearance — lives
in a **profile** — a folder (`profiles/<name>/`, e.g. `profiles/s20ide/`)
holding a shared `default_config.json` (git-committed baseline) and a live
`active_config.json` (per-workstation, gitignored, bootstrapped from the
default on first use) — editable from the Configuration dialog's profile
bar (create/load/save-as/save-as-default/delete; Restore Defaults previews
the profile's `default_config.json`). Device names for the
`device{...}`/`device_list{...}` pickers are no
longer a hand-maintained manifest: the Devices tab's **Discover** button
statically scans a profile's configured search path(s) for `__all__`-exported
names (never imports, never touches EPICS) and infers a category from the
source filename (see `B_PILOT/device_discovery.py`). A new beamline is
onboarded by creating a profile, pointing its device search paths at that
beamline's `instrument/devices/<bl>_devices/` directory, and clicking
Discover.
