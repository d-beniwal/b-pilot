"""Fake, ``ophyd.sim``-based devices for B-PILOT's ``demo`` profile.

One module per device category, named ``demo_<category>s.py`` so
``B_PILOT.device_discovery`` categorizes everything each module exports
purely from its filename -- the same convention real beamline device
directories use (``<bl>_motors.py``, ``<bl>_area_detectors.py``, ...).
"""
