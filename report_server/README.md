# B-PILOT report viewer service

A small read-only web service that mirrors an experiment report so people who
are not at the beamline can follow it live. B-PILOT pushes to it; collaborators
open an unguessable link.

> **Never install this into `bpilot_mpe_dev`.** It has its own dependencies and
> its own environment. The beamline env is carefully pinned, can only be
> conda-solved on the APS subnet, and needs none of this to run B-PILOT. This
> package also never imports `B_PILOT` — see `PROTOCOL.md` for why.

## What it guarantees

- **Readers cannot reach the workstation.** Traffic is outbound from the
  beamline only; the workstation opens no port. Reader routes here are GET-only
  and take no query parameters, no body and no cookies, so "read-only" is a
  property of the routing table, not a setting.
- **Hidden stays hidden.** A figure is served only while the current document
  references it, so hiding an entry in B-PILOT takes its pixels offline too.
- **Untrusted text stays inert.** Report bodies contain kernel output, pasted
  notes and LLM prose. The renderer escapes everything, emits a fixed tag
  allowlist, never produces a link from report content, and only ever points an
  `<img>` at a validated local figure. The page ships a CSP with no
  `unsafe-inline`. See `tests/test_security.py`.

## Running it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

export BPILOT_REPORT_PUSH_TOKENS="$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
export BPILOT_REPORT_DATA=/var/lib/bpilot-reports

uvicorn report_server.app:app --host 127.0.0.1 --port 8080 --log-level warning
```

Run it behind a TLS-terminating reverse proxy. Give B-PILOT the public base URL
(Configuration → Reports → *Service URL*) and the same token as
`BPILOT_REPORT_SYNC_TOKEN` in the workstation's environment.

`--log-level warning` is deliberate: the default access log records full paths,
and although the URL splits the secret out from `view_id`, the secret is still
in the path. If you want access logs, filter them.

### Environment

| Variable | Meaning |
|---|---|
| `BPILOT_REPORT_PUSH_TOKENS` | Comma-separated accepted push tokens. One per workstation or staff member, so one can be withdrawn without re-keying everyone. Required. |
| `BPILOT_REPORT_DATA` | Storage directory (default `./data`). |

### Storage

One directory per view: `report.md`, `meta.json`, `figures/`. No database.
Retention is "keep the last state indefinitely", so a link keeps working as a
record after the beamtime. To retire one, use *Stop sharing* in B-PILOT (which
calls `POST /push/{view_id}/revoke`), or delete the directory.

## Tests

```bash
python -m report_server.tests.test_security     # or: pytest report_server/tests
```

## The link is a bearer credential

Anyone the URL is forwarded to can read the report until it is rotated, and it
lands in browser history. That is the right trade for collaborators watching a
run; it is **not** appropriate for embargoed data unless you accept it.
B-PILOT's Report panel offers *Copy link*, *New link* (rotate) and *Stop
sharing*.
