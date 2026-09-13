# demo_instrument

A fully self-contained, **simulated** Bluesky instrument — no EPICS, no
hardware, no network — that backs B-PILOT's **`demo`** profile. It exists so
someone can try B-PILOT (launch a kernel, run/queue plans, browse run
history) before ever reaching a real beamline.

## What's here

- `src/demo_instrument/devices/` — fake motors, slits, a scalar detector, and
  a 2D area detector, all built on `ophyd.sim` (`SynAxis`, `SynGauss`,
  `DirectImage`). One module per device category, named the same way real
  beamline device directories are (`demo_motors.py`, `demo_slits.py`, ...),
  so B-PILOT's static device discovery categorizes them automatically.
- `src/demo_instrument/plans.py` — a handful of plans (`demo_count`,
  `demo_ascan`, `demo_grid_scan`, `demo_expose`, `demo_sim_check`) written in
  B-PILOT's plan-runner docstring grammar, so the parameter form and device
  pickers work exactly like they do for a real beamline plan.
- `src/demo_instrument/startup.py` — the kernel entry point
  (`from demo_instrument.startup import *`): builds one `RunEngine`, tries to
  subscribe an in-memory `databroker.temp()` catalog, and pulls every
  device/plan into scope.
- `src/demo_instrument/configs/iconfig.yml` — a placeholder. Its only job is
  to exist, so B-PILOT recognizes this as a "BITS-shaped" project (see
  `B_PILOT/paths.py`'s `"bits"` layout, the same one `3idc-bits` uses).

## Dependencies

Only three packages, and they need to be importable in whatever Python
environment launches B-PILOT (the `demo` profile runs its kernel under
B-PILOT's own interpreter — no separate conda env, no `screen`, no starter
script):

- `bluesky`
- `ophyd`
- `databroker` (optional — its absence just means runs aren't recorded to a
  browsable catalog; plans still run)

No `pip install -e` of this package is needed: the `demo` profile's
`bluesky_startup` puts `demo_instrument/src` on `sys.path` itself before
importing.

## Trying it

1. In B-PILOT, switch the profile (toolbar dropdown) to **demo**.
2. Launch the kernel — no beamline account, no `screen`, nothing beyond the
   three packages above.
3. Pick a plan (`demo_count`, `demo_ascan`, ...) from the plan file, fill in
   the form (the device pickers list the fake motors/detectors above), and
   run or queue it.
4. Open the Bluesky Viewer to browse the run just recorded (via the
   in-memory temp catalog).

## Adding to this stack

This is meant to be the **one** demo stack for every beamline project in
this workspace, not something rebuilt per beamline — add more fake devices
or plans here rather than starting a second one.
