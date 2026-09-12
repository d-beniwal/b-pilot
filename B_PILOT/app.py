"""Application entry point for the B-PILOT Qt plan-runner GUI.

Run it (from the ``b-pilot/`` directory) via the top-level launcher::

    conda activate bpilot-mpe
    python launch.py

or as a module::

    python -m B_PILOT

or directly::

    python B_PILOT/app.py
"""
from __future__ import annotations

import os
import sys
import traceback

# qtconsole/qtpy binding must be pinned before any qtconsole import happens.
os.environ.setdefault("QT_API", "pyqt5")

from PyQt5 import QtCore  # noqa: E402
from PyQt5 import QtWidgets  # noqa: E402


def _install_excepthook() -> None:
    """Log uncaught exceptions instead of letting a slot error kill the app."""
    def _hook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        sys.stderr.write(msg)
        try:
            if QtWidgets.QApplication.instance() is not None:
                QtWidgets.QMessageBox.critical(
                    None,
                    "Plan Runner — unexpected error",
                    f"{exc_type.__name__}: {exc}",
                )
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = _hook


def _raise_fd_limit() -> None:
    """Raise the open-files soft limit so a long GUI session can't exhaust fds.

    Each kernel connection opens several ZMQ sockets; over many launch/attach/
    shutdown cycles a low soft limit (macOS defaults to 256) leads to
    'Too many open files'.  Bump the soft limit toward the hard limit.
    """
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 8192 if hard == resource.RLIM_INFINITY else min(hard, 8192)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except Exception:  # noqa: BLE001  (non-POSIX or not permitted)
        pass


def main() -> None:
    """Build the QApplication, apply the theme, and show the main window."""
    _install_excepthook()
    _raise_fd_limit()
    try:
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    except Exception:  # noqa: BLE001
        pass

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("MPE Plan Runner")

    # Support both `python -m B_PILOT` (package) and `python app.py` (script).
    if __package__:
        from . import config
        from . import style as S
        from .main_window import MainWindow
    else:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from B_PILOT import config
        from B_PILOT import style as S
        from B_PILOT.main_window import MainWindow

    S.set_scale(config.get("ui_scale"))
    S.apply_theme(app, config.get("theme"), config.get("font_family"))

    win = MainWindow()
    # On Cmd-Q / app quit, keep or kill the kernel per the keep_kernel_on_exit
    # config (so a running session can be reattached next launch).
    app.aboutToQuit.connect(win.console.close_session)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
