"""Fake area detector -- an in-memory synthetic 2D image whose brightness
peak tracks ``sim_samx``. Built on ``ophyd.sim.DirectImage``, which stores
the frame directly in the Event (unlike ``ophyd.sim.img``, which needs a
filestore/registry we don't have here). No EPICS.

Filename ends in ``_area_detectors`` so ``device_discovery`` categorizes
everything exported here as ``"area_detector"``.
"""
import numpy as np
from ophyd.sim import DirectImage

from .demo_motors import sim_samx

__all__ = ["sim_det2d"]

_SHAPE = (32, 32)
_YY, _XX = np.indices(_SHAPE)
_CY, _CX = (s / 2 for s in _SHAPE)


def _frame() -> np.ndarray:
    """One synthetic frame: a Gaussian blob shifted by sim_samx's position."""
    sigma = 4.0
    r2 = (_XX - _CX - sim_samx.position) ** 2 + (_YY - _CY) ** 2
    peak = 500 * np.exp(-r2 / (2 * sigma**2))
    noise = np.random.default_rng().poisson(5, size=_SHAPE)
    return (peak + noise).astype(np.uint16)


sim_det2d = DirectImage(name="sim_det2d", func=_frame, labels={"detectors"})
