"""Fake scalar detector -- an ``ophyd.sim.SynGauss`` peak whose center tracks
``sim_samx``, plus Poisson noise, so a real alignment scan through it looks
meaningful. No EPICS.

Filename ends in ``_scalers`` so ``device_discovery`` categorizes everything
exported here as ``"scaler"``.
"""
from ophyd.sim import SynGauss

from .demo_motors import sim_samx

__all__ = ["sim_scaler"]

sim_scaler = SynGauss(
    "sim_scaler",
    sim_samx,
    "sim_samx",
    center=0,
    Imax=1000,
    sigma=1.5,
    noise="poisson",
    labels={"detectors"},
)
