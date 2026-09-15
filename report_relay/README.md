# Report relay

Publishes reports to Google Docs on behalf of a beamline workstation that has
**no route to the internet**.

```
BEAMLINE MACHINE            SHARED STORAGE           RELAY HOST
(no internet)               (both mount it)          (internet)

B-PILOT ──outbox backend──► outbox/<view_id>/   ──►  report_relay
  (stdlib only)               report.md              builds .docx
                              figures/               publishes to Drive
                              meta.json              writes published.json ──┐
                                                                             │
                            the link travels back ◄────────────────────────┘
```

## Why an outbox rather than reading the report directly

The raw `report.jsonl` contains hidden entries and excluded plans. They are
filtered out at *render* time on the beamline side. A relay that rendered the
record itself would have to reproduce that filtering exactly, on another
machine, with its own copy of the config — and any drift publishes something
the user deliberately hid. The outbox carries an **already-filtered rendered
document**; the relay ships only what it is handed and never re-renders.

It imports exactly two B-PILOT modules — `report_docx` and `report_drive` —
and that is asserted by `scripts/verify_report_relay.py`.

## Beamline side

Nothing to install. Set `BPILOT_REPORT_OUTBOX` to the shared directory, and in
Configuration → Reports choose **Shared folder — a relay publishes it**.

```bash
export BPILOT_REPORT_OUTBOX=/shared/bpilot-outbox
```

## Relay side

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r report_relay/requirements.txt

# The relay is headless, so it cannot run the browser consent flow. Authorise
# on a machine that has a browser (B-PILOT: Configuration -> Reports ->
# Connect Google account), then copy the token across:
#   scp ~/.bluesky_pilot/gdocs_token.json relay-host:~/.bluesky_pilot/
chmod 600 ~/.bluesky_pilot/gdocs_token.json

python -m report_relay.relay --outbox /shared/bpilot-outbox
```

| Flag | Meaning |
|---|---|
| `--outbox` | shared directory to watch (required) |
| `--token` | OAuth token file (default `~/.bluesky_pilot/gdocs_token.json`) |
| `--state` | relay state file (default `<outbox>/.relay-state.json`) |
| `--folder` | optional Drive folder id to create documents in |
| `--interval` | seconds between passes (default 30) |
| `--once` | one pass then exit — for cron, or for testing |

### State

`<outbox>/.relay-state.json` maps each view to its document id, the digest last
published and the generation last seen. A restart re-publishes nothing.
**Losing it means the relay creates fresh documents and the old links go
stale** — recoverable, but back it up alongside the outbox.

### Running it for real

Use a systemd unit or a `screen` session; it is a plain foreground loop. One
pass per 30 s is plenty — the beamline side already collapses bursts, and an
unchanged report costs one digest comparison and no API call.

## Security notes

- The token grants `drive.file` only: access to documents this application
  created, not the rest of the Drive. That bound matters most here, because a
  copied token sits on a shared relay host.
- Anyone who can write to the outbox can cause a document to be published.
  Give the directory the same permissions you would give the report itself.
- `--verbose` logs document URLs. They are bearer links; treat the log
  accordingly.
