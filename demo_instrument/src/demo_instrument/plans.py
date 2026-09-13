"""Demonstration plans for B-PILOT's ``demo`` profile.

Small, hand-written Bluesky plans against the ``ophyd.sim``-based devices in
:mod:`demo_instrument.devices` -- no EPICS, no hardware, safe to run on any
machine with ``bluesky``/``ophyd`` installed. Written directly against
B-PILOT's plan-runner docstring grammar (``Parameters`` section,
``device{<category>}`` dtype) so the parameter form exercises the same
device pickers a real beamline plan uses. Device/motor parameters are plain,
un-annotated, required arguments -- exactly like the real MPE plans in
``mpe_bluesky/instrument/plans/`` -- because the GUI renders the chosen
device's *name* unquoted into the dispatched command, so at run time the
parameter is the real (here, simulated) device object, not a string.
"""

import bluesky.plan_stubs as bps
import bluesky.plans as bp

__all__ = [
    "demo_sim_check",
    "demo_count",
    "demo_ascan",
    "demo_grid_scan",
    "demo_expose",
]


def demo_sim_check():
    """Zero-argument smoke test -- opens no run, touches no device.

    A quick way to confirm the kernel, the RunEngine, and this instrument's
    import all came up cleanly before running anything real.

    Example::

        RE(demo_sim_check())
    """
    print("demo_instrument: RunEngine is alive and the import worked.")
    yield from bps.null()


def demo_count(det, num=1, delay=0.0):
    """Count a fake detector a number of times.

    Parameters
    ----------
    det : device{scaler}
        Detector :: Fake scalar detector to read.
    num : int
        Readings :: Number of readings, one event per reading.
    delay : float [s]
        Delay :: Seconds to wait between readings.

    Example::

        RE(demo_count(det=sim_scaler, num=5))
    """
    yield from bp.count([det], num=num, delay=delay)


def demo_ascan(motor, det, start=-5, stop=5, num=21):
    """Scan a fake motor through a fake Gaussian peak.

    Parameters
    ----------
    motor : device{motor}
        Motor :: Fake motor to move.
    det : device{scaler}
        Detector :: Fake detector to read at each point (its peak tracks
        ``sim_samx``, so scanning that motor produces a real Gaussian).
    start : float [egu]
        Start :: First position.
    stop : float [egu]
        Stop :: Last position.
    num : int
        Points :: Number of points, start to stop inclusive.

    Example::

        RE(demo_ascan(motor=sim_samx, det=sim_scaler, start=-5, stop=5, num=21))
    """
    yield from bp.scan([det], motor, start, stop, num=num)


def demo_grid_scan(
    motor1,
    motor2,
    det,
    start1=-3,
    stop1=3,
    num1=7,
    start2=-3,
    stop2=3,
    num2=7,
):
    """Raster two fake motors over a fake detector.

    Parameters
    ----------
    motor1 : device{motor}
        Motor 1 :: Outer (slow) axis.
    motor2 : device{motor}
        Motor 2 :: Inner (fast) axis.
    det : device{scaler}
        Detector :: Fake detector to read at each grid point.
    start1 : float [egu]
        Motor 1 start :: First position of the outer axis.
    stop1 : float [egu]
        Motor 1 stop :: Last position of the outer axis.
    num1 : int
        Motor 1 points :: Number of steps on the outer axis.
    start2 : float [egu]
        Motor 2 start :: First position of the inner axis.
    stop2 : float [egu]
        Motor 2 stop :: Last position of the inner axis.
    num2 : int
        Motor 2 points :: Number of steps on the inner axis.

    Example::

        RE(demo_grid_scan(motor1=sim_samx, motor2=sim_samy, det=sim_scaler))
    """
    yield from bp.grid_scan(
        [det], motor1, start1, stop1, num1, motor2, start2, stop2, num2,
    )


def demo_expose(det, exposure_time=1.0, nframes=1):
    """Take one or more fake exposures on the fake area detector.

    Mirrors the shape of a real beamline "expose" plan (detector, exposure
    time, frame count) without needing any real camera.

    Parameters
    ----------
    det : device{area_detector}
        Detector :: Fake area detector to expose.
    exposure_time : float [s]
        Exposure time :: Simulated exposure time (recorded as metadata only
        -- the fake detector doesn't actually wait).
    nframes : int
        Frames :: Number of frames to acquire.

    Example::

        RE(demo_expose(det=sim_det2d, exposure_time=0.5, nframes=3))
    """
    yield from bp.count([det], num=nframes, md={"exposure_time": exposure_time})
