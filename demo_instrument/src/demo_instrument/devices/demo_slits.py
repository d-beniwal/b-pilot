"""Fake slit-blade motors -- plain ``ophyd.sim.SynAxis``, no EPICS.

Filename ends in ``_slits`` so ``device_discovery`` categorizes everything
exported here as ``"slit"``.
"""
from ophyd.sim import SynAxis

__all__ = ["sim_slit_h", "sim_slit_v"]

sim_slit_h = SynAxis(name="sim_slit_h", egu="mm", labels={"slits"})
sim_slit_v = SynAxis(name="sim_slit_v", egu="mm", labels={"slits"})
