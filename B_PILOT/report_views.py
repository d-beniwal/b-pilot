"""Per-beamline store of the *view tokens* that address a shared report.

One experiment that has been shared has one **view**: a short, non-secret
``view_id`` (6 characters) plus a long ``secret`` (32 characters, 192 bits of
entropy). The remote URL is
``<service>/r/<view_id>/<secret>``, and holding that URL is the whole of the
remote reader's authority -- there is no login (see :mod:`report_sync` for why
that is the right shape here, and for the caveats a bearer URL carries).

**Why the token is split in two.** The obvious design puts one long secret in
the path. That secret then lands in the service's access log on every request,
in any reverse proxy's log, and in the ``Referer`` of anything the page links
out to. Splitting it means logs and identity can use ``view_id`` -- which
reveals nothing and is stable across a rotation -- while only ``secret`` is
sensitive. Rotating a link keeps ``view_id`` and mints a new ``secret``, so the
service can revoke the old URL without losing track of which experiment the
view belongs to.

**Sharing is opt-in, per experiment.** A view exists only once the user has
pressed Share on that experiment; :func:`get_view` returning ``None`` is how
every other part of the feature knows to stay silent. Enabling sync in the
profile is not enough, and never publishes anything on its own.

Storage is ``<session_dir>/<beamline>/report_views.json``, beside the kernel's
other per-beamline state. Unlike :mod:`report_store`'s append-only log, this is
a *mutable* record, so it takes the locked read-modify-write + atomic-replace
route that :mod:`det_startup_state` and the retired ``queue_store`` use -- a
rotation is a read, a change, and a write, which is exactly the sequence
``report_store``'s one-``write()``-per-line argument does not cover.

The file is written ``0o600``. On the beamline's shared accounts that buys
little (everyone is ``s20iduser``), but it costs nothing and it is correct on a
personal workstation.

Qt-free and network-free on purpose: the panel reads a URL from here without
touching the worker thread, and the whole module unit-tests without a display,
a socket, or a beamline.
"""
from __future__ import annotations

import json
import os
import secrets
import time

try:
    import fcntl
except ImportError:  # non-POSIX (not expected on beamline Linux/macOS)
    fcntl = None

from . import experiment_history as eh
from . import kernel_session as ks

_FILENAME = "report_views.json"

# 4 url-safe bytes -> 6 characters. Not a secret: it only has to be unique
# within one service, and short enough to read out over a phone call.
_ID_BYTES = 4
# 24 bytes -> 192 bits, 32 characters. This is the credential.
_SECRET_BYTES = 24


def _dir(beamline: str) -> str:
    return ks.paths(beamline)["dir"]


def store_path(beamline: str) -> str:
    return os.path.join(_dir(beamline), _FILENAME)


def _lock_path(beamline: str) -> str:
    return os.path.join(_dir(beamline), "report_views.lock")


def _key(experiment: str) -> str:
    """Views are keyed by the same sanitised name the experiment directory uses.

    Going through :func:`experiment_history._safe_name` rather than the raw
    display name keeps one experiment to one view even if it is later referred
    to with different spacing or punctuation, and matches the directory the
    figures actually live in.
    """
    return eh._safe_name(experiment)


def _load_all(beamline: str) -> dict:
    try:
        with open(store_path(beamline), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("views"), dict):
            return data
    except Exception:  # noqa: BLE001 -- a missing or corrupt store means "nothing shared"
        pass
    return {"views": {}}


def _write_all(beamline: str, data: dict) -> None:
    os.makedirs(_dir(beamline), exist_ok=True)
    path = store_path(beamline)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)  # atomic


def _mutate(beamline: str, fn):
    """Locked read-modify-write over the whole store; `fn(data)` returns a result."""
    os.makedirs(_dir(beamline), exist_ok=True)
    lock = open(_lock_path(beamline), "w")
    try:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load_all(beamline)
        result = fn(data)
        _write_all(beamline, data)
        return result
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
        lock.close()


def get_view(beamline: str, experiment: str) -> dict | None:
    """This experiment's view, or ``None`` if it has never been shared.

    ``None`` is the resting state and the thing every caller should check:
    sharing is opt-in per experiment, so most experiments never get a view.
    """
    view = _load_all(beamline)["views"].get(_key(experiment))
    return dict(view) if isinstance(view, dict) else None


def ensure_view(beamline: str, experiment: str) -> dict:
    """This experiment's view, minting one on first use. Call from Share."""
    def _do(data: dict) -> dict:
        views = data["views"]
        key = _key(experiment)
        existing = views.get(key)
        if isinstance(existing, dict) and existing.get("secret"):
            return dict(existing)
        view = {
            "view_id": secrets.token_urlsafe(_ID_BYTES),
            "secret": secrets.token_urlsafe(_SECRET_BYTES),
            "experiment": experiment,
            "created": time.time(),
        }
        views[key] = view
        return dict(view)

    return _mutate(beamline, _do)


def update_view(beamline: str, experiment: str, fields: dict) -> dict | None:
    """Merge `fields` into this experiment's view. Returns the updated record.

    For target-specific state a sink has to remember -- the Google backend's
    document id, so that :meth:`Sink.url` can answer without a network call and
    the next push updates the same document rather than creating another one.

    Deliberately a merge rather than a replace, and deliberately refusing to
    touch ``view_id``/``secret``: those are minted here and a sink overwriting
    one would break the link the user is already holding.
    """
    protected = {"view_id", "secret"}
    payload = {k: v for k, v in (fields or {}).items() if k not in protected}
    if not payload:
        return get_view(beamline, experiment)

    def _do(data: dict) -> dict | None:
        view = data["views"].get(_key(experiment))
        if not isinstance(view, dict):
            return None
        view.update(payload)
        return dict(view)

    return _mutate(beamline, _do)


def rotate_view(beamline: str, experiment: str) -> dict | None:
    """Mint a new secret, keeping ``view_id``. Returns the new view, or ``None``.

    Keeping the id means the service can retire the old URL while still
    recognising which stored report the new one addresses -- a rotation is not
    a new share, and the reader-facing effect is only that the old link dies.
    """
    def _do(data: dict) -> dict | None:
        view = data["views"].get(_key(experiment))
        if not isinstance(view, dict):
            return None
        view["secret"] = secrets.token_urlsafe(_SECRET_BYTES)
        view["rotated"] = time.time()
        return dict(view)

    return _mutate(beamline, _do)


def forget_view(beamline: str, experiment: str) -> dict | None:
    """Drop this experiment's view locally. Returns what was removed, or ``None``.

    Local only. The caller is responsible for telling the service first -- see
    :func:`report_sync.stop_sharing`, which deliberately does not claim the old
    link is dead if the revoke request did not get through.
    """
    def _do(data: dict) -> dict | None:
        removed = data["views"].pop(_key(experiment), None)
        return dict(removed) if isinstance(removed, dict) else None

    return _mutate(beamline, _do)


def view_url(base_url: str, view: dict | None) -> str:
    """Reader-facing URL for `view`, or ``""`` if there isn't one.

    The trailing slash is load-bearing, not cosmetic. The report's figures are
    referenced relatively (``figures/fig_x.png``, so that an exported ``.md``
    also opens correctly); without the slash a browser resolves those against
    the *parent* of the secret and every image 404s.
    """
    if not (base_url and view and view.get("view_id") and view.get("secret")):
        return ""
    return f"{base_url.rstrip('/')}/r/{view['view_id']}/{view['secret']}/"
