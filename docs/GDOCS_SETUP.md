# Publishing the report to a Google Doc

An alternative to hosting `report_server/`. B-PILOT creates one Google Doc per
shared experiment, keeps it up to date, and shares it read-only by link. You
need a Google account and nothing else — no VM, no TLS certificate, no reverse
proxy, no firewall rule.

## Decide whether you want this

| | Viewer service (`report_server/`) | Google Doc |
|---|---|---|
| Hosting | a host, TLS, a reverse proxy | none |
| Updates | live, readers watch the page | on refresh, at most one per 30 s |
| Hiding an entry | figures go offline instantly | applied on the next update; **Drive keeps earlier revisions** |
| Figures | published | **not yet** — see "Figures" below |
| Link | unguessable URL you control | Google sharing link |

The revision-history point is the one to think hardest about. Anyone with the
link can open the document's revision history, so something you published and
then hid is still reachable. If that matters for your data, use the viewer
service.

## Figures (current limitation)

Figures are **not carried into the document yet**. The payload is Markdown,
which Drive converts to a Doc directly, and a relative `figures/*.png` path
means nothing to Drive — the converted document shows the alt text and no
image. Text publishes faithfully.

Resolving this needs one manual check that has not been run: export a report
with a figure as HTML (Report panel → Export → HTML), upload the `.html` to
Drive, open it as a Google Doc, and see whether the embedded images survive
the conversion.

- **They survive** → the payload switches to HTML and figures come for free.
- **They do not** → each figure is uploaded to Drive separately and the links
  rewritten, which makes them links rather than inline pixels.

See the `PENDING` note at the top of `B_PILOT/report_gdocs.py`.

## One-time setup

### 1. Install the client libraries

They are **not** in the pinned beamline environment and are deliberately not in
`environments/bpilot_mpe_dev.yml`:

```bash
conda activate bpilot_mpe_dev
pip install -r environments/gdocs-optional.txt
```

B-PILOT runs perfectly well without them — the backend is simply not offered,
and Configuration says why. **Do not install these on redwood** until the
workflow is proven on a workstation: the dependency closure includes compiled
wheels (`cffi`, `cryptography`), and pip-installed compiled packages in that
conda environment are what broke the GUI launch there once before.

### 2. Create an OAuth client

1. Go to <https://console.cloud.google.com/> and create a project (any name).
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **APIs & Services → OAuth consent screen** → External → fill in the required
   fields → add your own Google account under **Test users**. You do not need
   to publish or verify the app; a test user can authorise it indefinitely for
   this scope.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** →
   application type **Desktop app**.
5. Download the JSON. Keep it somewhere private, e.g.
   `~/.config/bpilot/gdocs_client.json`, mode `600`.

### 3. Point B-PILOT at it

```bash
export BPILOT_GDOCS_CREDENTIALS=~/.config/bpilot/gdocs_client.json
```

Put this on the same shell line (or in the same `~/.bashrc`) that launches
B-PILOT, next to `ARGO_API_KEY`. It lives in the environment rather than the
profile on purpose: `active_config.json` is committed and beamline accounts are
shared, so a profile that named a credentials file would arm every checkout of
that profile on machines that never chose to publish anything.

### 4. Connect the account

**Configuration → Reports → Remote viewer**:

1. Tick *Mirror this report to a remote viewer*.
2. Set **Publish to:** *Google Doc (no hosting needed)*.
3. Press **Connect Google account…** — a browser opens once. Approve.
4. Optionally paste a **Drive folder id** (the last segment of a Drive folder's
   URL) to keep the documents together.
5. Save.

The status line turns green when all three arming conditions are met: the
checkbox, the credentials variable, and a connected account.

## Using it

Report panel → **🌐 Share…** on the experiment you want published. You get a
link. From then on the document updates itself while B-PILOT runs, whether or
not the Report dock is open.

The share button then offers:

- **Copy link**
- **New link** — creates a *new* document and un-shares the old one. A Doc's
  URL is its file id, so there is no way to keep one document and invalidate
  its link. The old document stays in your Drive as a record.
- **Stop sharing** — removes the sharing grant. **The document is kept**: it is
  your record of the beamtime, and deleting it to achieve access control would
  destroy data.

## What the authorisation actually grants

The scope is `drive.file` and nothing wider. That grants access **only to files
this application itself created** — not your existing Drive contents. It is the
difference between a stolen token exposing the report documents and a stolen
token exposing everything you own, which is why it must stay that way.

The refresh token is stored at `~/.bluesky_pilot/gdocs_token.json`, mode `600`.
On a shared beamline account, anyone with that account can read it — which is
the argument for connecting on a personal workstation and, for now, not putting
a token on redwood at all. **Disconnect** removes the local token; it does not
revoke the grant Google-side. To revoke it properly, use
<https://myaccount.google.com/permissions>.

## Troubleshooting

**The backend is missing from the dropdown.** The client libraries did not
import. The status line shows the actual import error.

**"BPILOT_GDOCS_CREDENTIALS is not set".** It must be exported *before*
B-PILOT starts — a variable set afterwards is not visible to the running
process.

**"Not connected to Google — reconnect in Configuration".** The stored token
could not be refreshed: it was revoked, the machine is offline, or the clock is
badly skewed. Press Connect again.

**The document is not updating.** Check the status chip beside the Share
button. Remember the 30-second floor, and that an unchanged report makes no
request at all.
