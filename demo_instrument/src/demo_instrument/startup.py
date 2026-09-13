"""Kernel entry point for the ``demo`` profile: ``from demo_instrument.startup import *``.

Builds one ``RunEngine``, best-effort subscribes an in-memory temp
databroker catalog (so runs are browsable in B-PILOT's viewer without any
MongoDB/Tiled service), and imports every fake device + demo plan into this
module's namespace. No EPICS, no hardware, nothing that talks to a network.
"""
from bluesky import RunEngine

from .devices.demo_area_detectors import *  # noqa: F401,F403
from .devices.demo_area_detectors import __all__ as _area_detector_names
from .devices.demo_motors import *  # noqa: F401,F403
from .devices.demo_motors import __all__ as _motor_names
from .devices.demo_scalers import *  # noqa: F401,F403
from .devices.demo_scalers import __all__ as _scaler_names
from .devices.demo_slits import *  # noqa: F401,F403
from .devices.demo_slits import __all__ as _slit_names
from .plans import *  # noqa: F401,F403
from .plans import __all__ as _plan_names

__all__ = [
    "RE",
    *_motor_names,
    *_slit_names,
    *_scaler_names,
    *_area_detector_names,
    *_plan_names,
]

RE = RunEngine({})

try:
    import databroker

    catalog = databroker.temp().v2
    RE.subscribe(catalog.v1.insert)
    __all__.append("catalog")
except ImportError:
    print("demo_instrument: databroker not installed -- runs won't be "
          "recorded to a catalog, but plans still run.")

print(
    "demo_instrument: ready -- RE, "
    f"{len(_motor_names)} motor(s), {len(_slit_names)} slit(s), "
    f"{len(_scaler_names)} scaler(s), {len(_area_detector_names)} area "
    f"detector(s), {len(_plan_names)} plan(s). No EPICS, no hardware."
)
