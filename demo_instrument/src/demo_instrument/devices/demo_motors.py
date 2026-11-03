"""Fake sample-stage motors -- plain ``ophyd.sim.SynAxis``, no EPICS.

Filename ends in ``_motors`` so ``device_discovery`` categorizes everything
exported here as ``"motor"``.
"""
from ophyd.sim import SynAxis

__all__ = ["sim_samx", "sim_samy", "sim_samz", "sim_omega", "sim_base"]

sim_samx = SynAxis(name="sim_samx", labels={"motors"})
sim_samy = SynAxis(name="sim_samy", labels={"motors"})
sim_samz = SynAxis(name="sim_samz", labels={"motors"})
# Rotation axis and the base under it: used by BLADE's alignment demo (the `demo` profile of the blade project maps its roles onto these).
sim_omega = SynAxis(name="sim_omega", labels={"motors"})
sim_base = SynAxis(name="sim_base", labels={"motors"})
