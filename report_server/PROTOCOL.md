# Push protocol (normative)

This is the contract between B-PILOT's `B_PILOT/report_sync.py` and this
service. It is the only thing the two share.

**The service must never import `B_PILOT`.** That is the whole point of the
wire format below: what crosses is a *rendered document*, not the report's own
`report.jsonl` schema. A change to `report_builder`'s entry kinds, ordering,
edit overlays or exclusion logic therefore cannot require a coordinated service
deploy — which matters because the service is deployed on someone's VM and the
client is running a beamtime at 3 a.m.

## Transport

All requests are outbound from the workstation. The service never connects to
a workstation and the workstation opens no port.

## Authentication

Push routes require `Authorization: Bearer <token>` where the token is one of
the comma-separated values in the service's `BPILOT_REPORT_PUSH_TOKENS`. The
client reads its token from `BPILOT_REPORT_SYNC_TOKEN` in its own environment —
never from a config file, because B-PILOT's profiles are committed to git and
the beamline runs on shared accounts.

Reader routes require only the URL. `view_id` is a short non-secret handle
(safe in logs, stable across a rotation); `secret` is 192 bits. An unknown
`view_id` and a wrong `secret` both return **404**, identically, so a probe
cannot enumerate views.

## Push endpoints

### `POST /push/{view_id}`

Body: JSON, optionally `Content-Encoding: gzip` (the client always gzips).
Max 5 MB decompressed.

```jsonc
{
  "schema": 1,               // int; see forward-compatibility below
  "view_id": "k3n7q2",
  "secret": "<32 chars>",    // the client owns the secret and declares it here
  "beamline": "20ide",
  "experiment": "HEDM Ni625",
  "title": "",               // optional display title; blank => experiment name
  "generated_at": 1757280000.0,
  "tz": "CDT",               // the document's timestamps are beamline-local
  "tz_offset_s": -18000,     // and carry no marker, so the page labels them
  "sha256": "<hex>",         // digest of `markdown`; drives the reader's poll
  "markdown": "# ...",       // the whole document, byte-identical to Export → Markdown
  "figures": ["figures/fig_20260908_101112.png"],
  "manifest": [              // OPTIONAL, best-effort, never rendered from
    {"id": "...", "ts": 0.0, "kind": "run", "title": "", "plan_name": "", "ok": true}
  ]
}
```

Response `200 {"ok": true, "sha256": "..."}`.

### `POST /push/{view_id}/figures/{name}`

Raw image bytes. `name` must match `^fig_[A-Za-z0-9_-]+\.(png|jpe?g)$`.
`X-Content-Sha256` is advisory. Max 8 MB.

Figures are pushed **before** the document that references them, so a reader
never loads a page whose images 404.

### `GET /push/{view_id}/figures`

`[{"name": ..., "sha256": ...}]` — lets a restarted client skip re-uploading.

### `POST /push/{view_id}/revoke`

Deletes the view and its figures. Idempotent; a already-absent view is success.

## Reader endpoints — GET only

| Route | Returns |
|---|---|
| `/r/{view_id}/{secret}/` | the page (**trailing slash required** so relative `figures/…` resolve) |
| `/r/{view_id}/{secret}/fragment` | the rendered body only, for live refresh |
| `/r/{view_id}/{secret}/version` | `{"sha256", "received_at", "age"}` |
| `/r/{view_id}/{secret}/figures/{name}` | one figure |

These accept no query parameters, no body and no cookies. There is no route by
which a reader can change anything.

## Two rules that carry weight

**A figure is served only while the current `report.md` references it.** The
client derives the figures it uploads by scanning the *rendered* document, so
hiding an entry drops its image line — and this check makes that hiding take
the pixels offline instantly, with no delete protocol and no GC. Without it,
pasting a screenshot and then hiding it would leave it readable forever.

**Snapshots, not deltas.** Each push replaces the stored document. That makes
failure recovery free: a failed push is retried with newer content, and there
are no sequence numbers, gap detection or resync path to diverge. The cost is
re-sending the whole document, which gzip plus the client's SHA-256
short-circuit reduces to near nothing — an unchanged render makes no request
at all.

## Forward compatibility

A service seeing a `schema` it does not know **must still render `markdown`**
and must ignore `manifest`. `manifest` exists so a table of contents or run
counter can be added later without a new wire format; it is never load-bearing.
