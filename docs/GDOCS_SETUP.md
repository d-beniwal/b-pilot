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
| Figures | published | published (embedded in the document) |
| Link | unguessable URL you control | Google sharing link |

The revision-history point is the one to think hardest about. Anyone with the
link can open the document's revision history, so something you published and
then hid is still reachable. If that matters for your data, use the viewer
service.

## How figures get there

B-PILOT builds a **`.docx`** locally (`B_PILOT/report_docx.py`) and uploads
that for conversion. Images inside a `.docx` are real files in the archive, so
Drive's converter turns them into inline pictures in the resulting Doc. The
two simpler payloads both fail: Markdown carries relative `figures/*.png`
paths that mean nothing to Drive, and the app's HTML export takes its colours
from the session theme, so a dark session would publish near-white text.

If `python-docx` is not installed, the backend falls back to uploading the
Markdown — the text still publishes, the figures do not.

## One-time setup

### 1. Install the client libraries

They are **not** in the pinned beamline environment and are deliberately not in
`environments/bpilot_mpe_dev.yml`:

```bash
conda activate bpilot_mpe_dev
pip install -r environments/gdocs-optional.txt
```

That includes `python-docx`, which is what puts the figures in the document.

B-PILOT runs perfectly well without them — the backend is simply not offered,
and Configuration says why. **Do not install these on redwood** until the
workflow is proven on a workstation: the dependency closure includes compiled
wheels (`cffi`, `cryptography`), and pip-installed compiled packages in that
conda environment are what broke the GUI launch there once before.

### 2. Create an OAuth client

1. Go to <https://console.cloud.google.com/> and create a project (any name).
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **Google Auth Platform → Audience** (older consoles: **APIs & Services →
   OAuth consent screen**) → **External** → fill in the required fields.
   **Under "Test users", add the exact Google address you will sign in with.**
   Skipping this is the most common setup failure: consent is refused with
   *"has not completed the Google verification process… can only be accessed by
   developer-approved testers"*, before B-PILOT is involved at all.
   - While publishing status is **Testing**, Google expires refresh tokens after
     **7 days**, so you will have to press Connect again about weekly. It fails
     visibly (the status chip says to reconnect), not silently.
   - To avoid that, set publishing status to **In production**. `drive.file` is
     a narrow per-file scope and generally does not require the verification
     review broader Drive scopes do, so this usually takes effect immediately.
     If the console asks you to submit for verification instead, stay in
     Testing.
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

**Consent is refused: "can only be accessed by developer-approved testers".**
Your account is not in **Test users** for the project (step 2.3), or the browser
signed you in as a *different* Google account than the one you added. Add the
exact address, and pick it deliberately in the account chooser — do the Connect
in a private window if the chooser is being skipped.

**"Not connected to Google — reconnect in Configuration".** The stored token
could not be refreshed: it was revoked, the machine is offline, the clock is
badly skewed — or, most often, the app is in **Testing** publishing status and
Google expired the refresh token after 7 days. Press Connect again, or move the
app to production (step 2.3).

**The document is not updating.** Check the status chip beside the Share
button. Remember the 30-second floor, and that an unchanged report makes no
request at all.
