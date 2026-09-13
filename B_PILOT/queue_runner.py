"""Detached driver for the persistent plan queue (one per beamline).

Runs alongside the kernel (started like the session recorder) and is the ONLY
thing that dispatches queued plans — so the queue progresses and its per-item
status updates **independently of the GUI** (even while it is detached/closed).

* **Singleton** via an ``flock`` on ``queue_runner.lock``; extra copies self-exit,
  so it is safe to (re)launch on every kernel start/attach.
* While the queue ``state`` is ``running`` it dispatches the next ``waiting`` item
  to the kernel (as a normal, non-silent execution so it shows in the console /
  transcript), waits for the reply, and writes back ``done``/``error``.
* On error it pauses the queue (matches the interactive scheduler; a Ctrl-C /
  ``RunEngineInterrupted`` surfaces as an errored reply).
* Once no ``waiting`` item is left, it pauses the queue too — draining the
  queue always ends in ``paused``, never a `running` queue with nothing left
  to do. This means a plan added later sits `waiting` until the user presses
  Start/Resume again; it never starts on its own just because the queue
  happened to be left `running`.
* Exits when the kernel dies.

No Qt, with one narrow exception: a `QCoreApplication` instance (no event
loop entered) is created so `midas_bridge`'s `QLocalSocket` blocking calls
work when a queued item has an area_detector device and the MIDAS_GUI
bridge is enabled.  Run: ``python -m B_PILOT.queue_runner [<beamline>]``.
"""
from __future__ import annotations

import os
import sys
import time

try:
    import fcntl
except ImportError:
    fcntl = None

from . import command_builder
from . import config
from . import det_startup_state
from . import kernel_session as ks
from . import midas_bridge
from . import queue_store as qs


def _acquire_singleton(beamline: str):
    """Hold an exclusive lock so only one runner exists per beamline; else None."""
    path = os.path.join(ks.paths(beamline)["dir"], "queue_runner.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "w")
    if fcntl is None:
        return fh
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def _wait_reply(kc, msg_id: str, cf: str):
    """Block until the execute_reply for `msg_id`; return ok(bool) or None if kernel died."""
    while True:
        if not ks.is_alive(cf):
            return None
        try:
            msg = kc.get_shell_msg(timeout=1.0)
        except Exception:  # noqa: BLE001  (queue.Empty on timeout)
            continue
        if (
            msg.get("msg_type") == "execute_reply"
            and msg.get("parent_header", {}).get("msg_id") == msg_id
        ):
            return msg.get("content", {}).get("status") == "ok"


def _run_cell(kc, code: str, cf: str):
    """Execute one cell and block for its reply; True/False (ok), or None if
    the kernel died. Split out so det_startup and the real plan run as two
    separate cells -- each lands as its own history/plan-history entry
    instead of collapsing into one under det_startup's name."""
    try:
        msg_id = kc.execute(code, silent=False, store_history=True)
    except Exception:  # noqa: BLE001
        return False
    return _wait_reply(kc, msg_id, cf)


def main(argv: list[str]) -> int:
    beamline = argv[1] if len(argv) > 1 else config.get("beamline")
    lock = _acquire_singleton(beamline)
    if lock is None:
        return 0  # another runner is already active

    # QLocalSocket's blocking waitFor*() calls (midas_bridge._send_live_pv)
    # need a QCoreApplication instance to exist — no event loop is entered,
    # this process otherwise stays plain-Python/no-Qt as documented above.
    # Must be kept alive (bound to a name) for the life of main(): an
    # unassigned QCoreApplication(...) has no Python reference anywhere and
    # gets garbage-collected immediately, tearing down the app's socket-
    # notifier machinery right after it's created.
    from PyQt5 import QtCore
    _qt_app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication(sys.argv)

    cf = ks.connection_file(beamline)
    try:
        from jupyter_client import BlockingKernelClient

        kc = BlockingKernelClient()
        kc.load_connection_file(cf)
        # shell+control only — no heartbeat thread (liveness uses ks.is_alive).
        kc.start_channels(shell=True, iopub=False, stdin=False, hb=False,
                          control=True)
    except Exception:  # noqa: BLE001
        lock.close()
        return 1

    # A leftover 'running' item means a previous runner died — flag it.
    qs.reconcile_stale_running(beamline)

    try:
        while True:
            if not ks.is_alive(cf):
                break
            data = qs.load(beamline)
            if data.get("state") == qs.S_RUNNING:
                nxt = next(
                    (it for it in data["items"] if it["status"] == qs.WAITING), None
                )
                if nxt is None:
                    # Drained: nothing left to run. Go back to paused so a
                    # plan added later doesn't start running on its own --
                    # the user has to press Start/Resume again.
                    qs.set_state(beamline, qs.PAUSED)
                    continue
                qs.set_item_status(beamline, nxt["id"], qs.RUNNING)
                detectors = nxt.get("midas_area_detectors") or []
                midas_bridge.notify_queued_sync(
                    kc,
                    detectors,
                    config.get("midas_bridge_enabled"),
                )
                startup = det_startup_state.build_startup_commands(
                    beamline, detectors
                )
                if startup:
                    ok = _run_cell(kc, startup, cf)
                    if ok is None:
                        break  # kernel died mid-plan; leave item as-is and exit
                    if not ok:
                        qs.set_item_status(beamline, nxt["id"], qs.ERROR)
                        qs.set_state(beamline, qs.PAUSED)  # stop on error
                        continue
                # The stored command keeps its import line (the queue
                # panel displays it); whether it is sent is decided here,
                # at dispatch -- see command_builder.for_console.
                ok = _run_cell(kc, command_builder.for_console(nxt["command"]), cf)
                if ok is None:
                    break  # kernel died mid-plan; leave item as-is and exit
                qs.set_item_status(
                    beamline, nxt["id"], qs.DONE if ok else qs.ERROR
                )
                if not ok:
                    qs.set_state(beamline, qs.PAUSED)  # stop on error
                continue
            time.sleep(1.0)
    finally:
        try:
            kc.stop_channels()
        except Exception:  # noqa: BLE001
            pass
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
