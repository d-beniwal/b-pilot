"""Watch an outbox on shared storage and publish each report to Google Docs.

For a beamline workstation with no route to the internet. B-PILOT's ``outbox``
backend writes the rendered report and its figures to a directory both machines
can see; this daemon runs somewhere that *can* reach Google, and does the
publishing.

    python -m report_relay.relay --outbox /shared/bpilot-outbox

**It publishes only what it is handed.** It never reads ``report.jsonl`` and
never re-renders anything. Hidden entries and excluded plans are filtered out
on the beamline side at render time, and reproducing that filtering here --
with a second copy of the config, on another machine -- is exactly how you end
up publishing something the user hid. The outbox contains an already-filtered
document; this turns it into a ``.docx`` and uploads it. That is the whole job.

**State lives in one JSON file**, not in Drive. For each view it remembers the
document id, the digest last published and the generation last seen, so a
restart re-publishes nothing and a crash mid-run costs at most one duplicate
upload. Losing the state file means the relay creates fresh documents and the
old links go stale -- recoverable, but back it up with the outbox.

**Failures are per-view and never fatal.** One experiment whose figure is
corrupt must not stop the others from updating, and a Google outage must not
kill the daemon -- it logs, backs off, and carries on.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

# The relay lives beside B_PILOT in the same checkout and reuses exactly two of
# its modules, both deliberately dependency-light: report_docx (io/os/re +
# python-docx) and report_drive (the Google client, parameterised rather than
# config-driven). Neither pulls in Qt or a profile. Duplicating them here was
# the alternative, and a second copy of the Drive logic drifting from the first
# is a worse problem than this import.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from B_PILOT import report_docx  # noqa: E402
from B_PILOT import report_drive as dr  # noqa: E402

LOG = logging.getLogger("report_relay")

DEFAULT_INTERVAL_S = 30.0
STATE_SCHEMA = 1


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 -- absent, truncated, or mid-write
        return None


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class State:
    """What the relay has already published, keyed by view id."""

    def __init__(self, path: str) -> None:
        self.path = path
        data = _read_json(path) or {}
        self.views: dict = data.get("views") or {}

    def get(self, view_id: str) -> dict:
        return self.views.get(view_id) or {}

    def put(self, view_id: str, **fields) -> None:
        entry = dict(self.views.get(view_id) or {})
        entry.update(fields)
        self.views[view_id] = entry
        self.save()

    def drop(self, view_id: str) -> None:
        self.views.pop(view_id, None)
        self.save()

    def save(self) -> None:
        _write_json(self.path, {"schema": STATE_SCHEMA, "views": self.views})


class Relay:
    def __init__(self, outbox: str, client: dr.DriveClient, state: State) -> None:
        self.outbox = outbox
        self.client = client
        self.state = state

    # ── one view ─────────────────────────────────────────────────────────────

    def _publish(self, view_id: str, directory: str, meta: dict) -> None:
        known = self.state.get(view_id)
        digest = meta.get("sha256") or ""
        generation = int(meta.get("generation") or 0)

        # A bumped generation means the user pressed "New link": retire the old
        # document and publish into a fresh one, because a Doc's URL is its
        # file id and there is no other way to invalidate a link.
        rotated = known.get("generation") is not None and generation > int(known["generation"])
        file_id = "" if rotated else (known.get("gdoc_id") or "")

        if file_id and digest and digest == known.get("sha256"):
            return  # nothing the reader would see has changed

        markdown_path = os.path.join(directory, "report.md")
        try:
            with open(markdown_path, encoding="utf-8") as fh:
                markdown = fh.read()
        except OSError as exc:
            LOG.warning("%s: cannot read report.md (%s)", view_id, exc)
            return

        try:
            payload = report_docx.build(
                markdown,
                base_dir=directory,
                title=meta.get("title") or meta.get("experiment") or view_id,
            )
            mimetype = dr.DOCX_MIME
        except Exception as exc:  # noqa: BLE001 -- a malformed record
            LOG.warning("%s: could not build the .docx (%s) -- sending Markdown", view_id, exc)
            payload, mimetype = markdown.encode("utf-8"), dr.MARKDOWN_MIME

        if rotated and known.get("gdoc_id"):
            outcome, message = self.client.retire(known["gdoc_id"])
            if outcome != dr.OK:
                LOG.warning("%s: could not retire the old document (%s)", view_id, message)
            else:
                LOG.info("%s: retired the previous document", view_id)

        if not file_id:
            title = meta.get("title") or meta.get("experiment") or view_id
            beamline = meta.get("beamline") or ""
            file_id, url, error = self.client.create_shared(
                f"{title} ({beamline})" if beamline else title
            )
            if error:
                LOG.warning("%s: could not create the document (%s)", view_id, error)
                return
            LOG.info("%s: created %s", view_id, url)
            self.state.put(view_id, gdoc_id=file_id, url=url, sha256="", generation=generation)
            # Hand the link back across the shared folder, so it shows up in
            # B-PILOT's Report panel even though B-PILOT cannot reach Google.
            _write_json(
                os.path.join(directory, "published.json"),
                {"url": url, "gdoc_id": file_id, "at": time.time()},
            )

        outcome, message = self.client.publish(file_id, payload, mimetype)
        if outcome == dr.OK:
            self.state.put(view_id, gdoc_id=file_id, sha256=digest, generation=generation,
                           published_at=time.time())
            LOG.info("%s: published (%d bytes)", view_id, len(payload))
            return

        if outcome == dr.REVOKED:
            # Somebody deleted the document out from under us. Forget it; the
            # next pass creates a new one rather than retrying forever.
            LOG.warning("%s: the document is gone (%s) -- will recreate", view_id, message)
            self.state.drop(view_id)
            return
        LOG.warning("%s: publish failed (%s): %s", view_id, outcome, message)

    def _revoke(self, view_id: str, directory: str) -> None:
        known = self.state.get(view_id)
        file_id = known.get("gdoc_id")
        if file_id:
            outcome, message = self.client.retire(file_id)
            if outcome != dr.OK:
                # Leave the marker in place and try again next pass: claiming a
                # link is dead when it is not is the one lie to avoid.
                LOG.warning("%s: revoke failed (%s) -- will retry", view_id, message)
                return
            LOG.info("%s: un-shared %s", view_id, file_id)
        self.state.drop(view_id)
        for name in ("revoked.json", "published.json", "meta.json", "report.md"):
            try:
                os.unlink(os.path.join(directory, name))
            except OSError:
                pass
        try:
            os.rmdir(os.path.join(directory, "figures"))
        except OSError:
            pass
        try:
            os.rmdir(directory)
        except OSError:
            pass  # not empty, or gone already -- neither is a problem

    # ── one pass ─────────────────────────────────────────────────────────────

    def scan(self) -> None:
        try:
            entries = sorted(os.listdir(self.outbox))
        except OSError as exc:
            LOG.warning("cannot read the outbox %s (%s)", self.outbox, exc)
            return

        for view_id in entries:
            directory = os.path.join(self.outbox, view_id)
            if not os.path.isdir(directory):
                continue
            try:
                if os.path.isfile(os.path.join(directory, "revoked.json")):
                    self._revoke(view_id, directory)
                    continue
                meta = _read_json(os.path.join(directory, "meta.json"))
                if not meta:
                    continue  # not shared yet, or mid-write -- next pass
                self._publish(view_id, directory, meta)
            except Exception:  # noqa: BLE001 -- one bad view must not stop the rest
                LOG.exception("%s: unhandled error", view_id)

    def run(self, interval: float) -> None:
        LOG.info("watching %s every %.0fs", self.outbox, interval)
        while True:
            self.scan()
            time.sleep(interval)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--outbox", required=True, help="shared outbox directory to watch")
    parser.add_argument("--token", default=os.path.expanduser("~/.bluesky_pilot/gdocs_token.json"),
                        help="Google OAuth token file (authorise elsewhere and copy it here)")
    parser.add_argument("--state", default="", help="relay state file (default: <outbox>/.relay-state.json)")
    parser.add_argument("--folder", default="", help="optional Drive folder id to create documents in")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    parser.add_argument("--once", action="store_true", help="one pass, then exit (for cron or testing)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    if not dr.available():
        LOG.error("Google client libraries are missing: %s", dr.MISSING_REASON)
        LOG.error("pip install -r report_relay/requirements.txt")
        return 2
    if not report_docx.available():
        LOG.warning("python-docx is missing (%s) -- figures will NOT be published",
                    report_docx.MISSING_REASON)
    if not os.path.isdir(args.outbox):
        LOG.error("outbox does not exist: %s", args.outbox)
        return 2
    if not os.path.isfile(args.token):
        LOG.error("no Google token at %s", args.token)
        LOG.error("authorise on a machine with a browser, then copy the file here (mode 600)")
        return 2

    client = dr.DriveClient(token_path=args.token, folder_id=args.folder)
    if client.service() is None:
        LOG.error("the token at %s could not be refreshed -- re-authorise and copy it again",
                  args.token)
        return 2

    state = State(args.state or os.path.join(args.outbox, ".relay-state.json"))
    relay = Relay(args.outbox, client, state)

    if args.once:
        relay.scan()
        return 0
    try:
        relay.run(args.interval)
    except KeyboardInterrupt:
        LOG.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
