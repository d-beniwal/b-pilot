"""Fake sample-stage motors -- plain ``ophyd.sim.SynAxis``, no EPICS.

Filename ends in ``_motors`` so ``device_discovery`` categorizes everything
exported here as ``"motor"``.
"""
from ophyd.sim import SynAxis

__all__ = ["sim_samx", "sim_samy", "sim_samz"]

sim_samx = SynAxis(name="sim_samx", labels={"motors"})
sim_samy = SynAxis(name="sim_samy", labels={"motors"})
sim_samz = SynAxis(name="sim_samz", labels={"motors"})
