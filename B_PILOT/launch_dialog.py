"""Modal dialog gating "Launch IPython": confirm the DM experiment and setup
file before a kernel starts, so the experiment banner shown once it's running
(see :mod:`main_window`) is never a guess.

If the active profile sets ``experiment_data_root``, this is also the one
place a user is asked to confirm an experiment name B-PILOT can't find a DM
folder for -- accepting files it as a **local/temporary** experiment, which
just means "no matching folder exists yet" and lets
:func:`experiment_history.experiment_dir` fall back to its old,
B-PILOT-owned location. See that function's docstring for the actual rule:
B-PILOT never creates the top-level experiment folder itself.
"""
from __future__ import annotations

import os

from PyQt5 import QtWidgets

from . import config
from . import experiment_history as _eh

_DEFAULT_SETUP_FILE = "exp_setup.yml"


def _dm_experiment_dir(experiment: str) -> str | None:
    """Path where the beamline's data-management workflow is expected to
    have already created this experiment's folder — created by DM, never by
    B-PILOT (see :func:`experiment_history.experiment_dir`'s docstring for
    the same rule enforced at the storage layer).

    ``None`` if this profile has no ``experiment_data_root`` configured —
    nothing to check an experiment name against (e.g. `demo`/`s3idc`).
    """
    root = (config.get("experiment_data_root") or "").strip()
    if not root:
        return None
    return os.path.join(os.path.expanduser(root), experiment)


class LaunchDialog(QtWidgets.QDialog):
    """Ask for the DM experiment name + setup file before starting a kernel.

    Pre-fills from the last-used values in config. Experiment cannot be left
    blank — it drives the beamline account's data/log paths (see
    ``instrument/devices/global_variables.py``) and is what the console
    banner displays once the kernel is up, so a silent stale value is a real
    risk on the beamline.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Launch IPython")
        self.setModal(True)

        lay = QtWidgets.QVBoxLayout(self)

        info = QtWidgets.QLabel(
            "Confirm the experiment for this session. This name is recorded "
            "to dm_experiment.txt and determines where Bluesky reads/writes "
            "data and session logs."
        )
        info.setWordWrap(True)
        lay.addWidget(info)

        form = QtWidgets.QFormLayout()
        self._experiment = QtWidgets.QLineEdit(config.get("dm_experiment") or "")
        self._experiment.setToolTip(
            "DM experiment name. Recorded to user_defaults/dm_experiment.txt "
            "by the embedded starter script."
        )
        form.addRow("Experiment:", self._experiment)

        self._setup_file = QtWidgets.QLineEdit(
            config.get("setup_file") or _DEFAULT_SETUP_FILE
        )
        self._setup_file.setToolTip(f"Setup YAML (default {_DEFAULT_SETUP_FILE}).")
        form.addRow("Setup file:", self._setup_file)
        lay.addLayout(form)

        self._buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        lay.addWidget(self._buttons)

        self._experiment.textChanged.connect(self._update_ok_enabled)
        self._update_ok_enabled()
        self._experiment.setFocus()
        self._experiment.selectAll()

    def _update_ok_enabled(self) -> None:
        ok_btn = self._buttons.button(QtWidgets.QDialogButtonBox.Ok)
        ok_btn.setEnabled(bool(self._experiment.text().strip()))

    def accept(self) -> None:
        experiment = self.experiment()
        exp_dir = _dm_experiment_dir(experiment)
        if experiment and exp_dir and not os.path.isdir(exp_dir):
            # experiment_dir() applies the exact same "DM folder doesn't
            # exist" check internally (see its docstring), so at this point
            # it already resolves to the local/temporary fallback location
            # -- reuse it rather than re-deriving the path here.
            beamline = config.get("beamline") or ""
            local_dir = _eh.experiment_dir(beamline, experiment)
            ans = QtWidgets.QMessageBox.question(
                self,
                "Experiment not found",
                f"No existing experiment folder for {experiment!r}:\n\n{exp_dir}\n\n"
                "That folder is created by the beamline's data-management "
                "workflow — B-PILOT never creates it itself, so double-check "
                "the experiment name first.\n\n"
                "Create this as a LOCAL/TEMPORARY experiment instead? Its "
                "records (kernel history, report, AutoPILOT chats) will be "
                f"kept only under this workstation's own\n{local_dir}\n"
                "rather than alongside the real experiment data — typically "
                "used for testing.\n\n"
                "Choose No to go back and re-check the experiment name.",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if ans != QtWidgets.QMessageBox.Yes:
                return
        super().accept()

    def experiment(self) -> str:
        return self._experiment.text().strip()

    def setup_file(self) -> str:
        return self._setup_file.text().strip() or _DEFAULT_SETUP_FILE
