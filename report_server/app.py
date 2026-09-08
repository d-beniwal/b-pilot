"""The B-PILOT report viewer service.

Two halves that never mix:

* **Push routes** (``/push/...``) accept a report from a B-PILOT workstation.
  Every one requires a bearer token from ``BPILOT_REPORT_PUSH_TOKENS``.
* **Reader routes** (``/r/{view_id}/{secret}/...``) serve it. They are
  **GET-only, take no query parameters, no body and no cookies**, and there is
  no route by which a reader can alter anything. "Read-only" is therefore a
  property of the routing table rather than a setting someone can get wrong --
  which is what lets the workstation-side documentation promise that a remote
  viewer cannot reach the instrument even in principle.

The URL splits the credential in two: ``view_id`` is a short, non-secret handle
safe to put in access logs and to keep across a rotation, and ``secret`` is the
192-bit part. Without the split, every log line and every outbound ``Referer``
would carry a live credential.

An unknown view and a wrong secret both return **404**, deliberately and
identically, so a probe cannot learn which view ids exist.

This package must never import ``B_PILOT``. The whole reason the wire format is
rendered Markdown rather than the report's own JSONL is that the service does
not need to know the entry schema -- so a ``report_builder`` change never
forces a service deploy in the middle of a beamtime. An import would quietly
give that away. See ``PROTOCOL.md``.
"""
from __future__ import annotations

import gzip
import hmac
import json
import os
import time

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import render, storage

HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="B-PILOT report viewer", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

# No 'unsafe-inline' anywhere: render.py emits classes, never style attributes,
# and the only script is a static same-origin file. img-src 'self' is a second
# line of defence behind the renderer's refusal to emit a remote image src --
# a beacon in a pasted note must not be able to phone home with a reader's IP.
CSP = (
    "default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
READER_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
    # The secret is in the path, so it must not ride out in a Referer header.
    "Referrer-Policy": "no-referrer",
    # A lab record should not be left behind in a shared browser's disk cache.
    "Cache-Control": "no-store",
}


# ── push authentication ──────────────────────────────────────────────────────

def _push_tokens() -> list:
    """Accepted push tokens, comma-separated in ``BPILOT_REPORT_PUSH_TOKENS``.

    A list rather than one value so each workstation or staff member can carry
    their own, and one can be withdrawn without re-keying everybody.
    """
    raw = os.environ.get("BPILOT_REPORT_PUSH_TOKENS") or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def _require_push(authorization: str | None) -> None:
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    tokens = _push_tokens()
    if not tokens:
        raise HTTPException(status_code=503, detail="service has no push tokens configured")
    # compare_digest against each, and never short-circuit the loop, so the
    # response time does not depend on which token matched or how far it got.
    ok = False
    for token in tokens:
        if hmac.compare_digest(token, supplied):
            ok = True
    if not ok:
        raise HTTPException(status_code=401, detail="bad push token")


async def _body(request: Request) -> bytes:
    """Request body, transparently gunzipped, size-capped.

    Starlette does not decode ``Content-Encoding`` on requests, so this is
    where the client's gzip is undone. The cap is checked on both the
    compressed and the decompressed size: a small compressed body can expand
    enormously, and a service that fills its own disk because someone's kernel
    entered an error loop is not much use.
    """
    raw = await request.body()
    if len(raw) > storage.MAX_DOC_BYTES:
        raise HTTPException(status_code=413, detail="body too large")
    if (request.headers.get("content-encoding") or "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            raise HTTPException(status_code=400, detail="bad gzip body")
        if len(raw) > storage.MAX_DOC_BYTES:
            raise HTTPException(status_code=413, detail="body too large")
    return raw


# ── push routes ──────────────────────────────────────────────────────────────

@app.post("/push/{view_id}")
async def push_report(view_id: str, request: Request, authorization: str = Header(None)):
    _require_push(authorization)
    try:
        envelope = json.loads((await _body(request)).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="body is not JSON")
    if not isinstance(envelope, dict) or not envelope.get("secret"):
        raise HTTPException(status_code=400, detail="envelope missing secret")
    if not storage.save_push(view_id, envelope):
        raise HTTPException(status_code=400, detail="bad view id")
    return {"ok": True, "sha256": envelope.get("sha256")}


@app.post("/push/{view_id}/figures/{name}")
async def push_figure(
    view_id: str, name: str, request: Request, authorization: str = Header(None)
):
    _require_push(authorization)
    if storage.load_meta(view_id) is None:
        # Figures are pushed before the document that references them, so on a
        # brand-new view there is nothing yet. Accept them into the directory.
        directory = os.path.join(storage.data_root(), view_id)
        if not storage.VIEW_ID.match(view_id):
            raise HTTPException(status_code=400, detail="bad view id")
        os.makedirs(os.path.join(directory, "figures"), exist_ok=True)
    blob = await request.body()
    if len(blob) > storage.MAX_FIGURE_BYTES:
        raise HTTPException(status_code=413, detail="figure too large")
    if not storage.save_figure(view_id, name, blob):
        raise HTTPException(status_code=400, detail="bad figure name")
    return {"ok": True, "name": name}


@app.get("/push/{view_id}/figures")
async def list_figures(view_id: str, authorization: str = Header(None)):
    _require_push(authorization)
    return JSONResponse(storage.list_figures(view_id))


@app.post("/push/{view_id}/revoke")
async def revoke(view_id: str, authorization: str = Header(None)):
    _require_push(authorization)
    storage.revoke(view_id)
    return {"ok": True}


# ── reader routes (GET only, no input accepted) ──────────────────────────────

def _view_or_404(view_id: str, secret: str) -> dict:
    """The view's metadata, or 404 -- identically for unknown id and bad secret."""
    if not storage.check_secret(view_id, secret):
        raise HTTPException(status_code=404, detail="not found")
    return storage.load_meta(view_id) or {}


def _age(meta: dict) -> str:
    received = meta.get("received_at") or 0
    seconds = max(0, int(time.time() - received))
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


@app.get("/r/{view_id}/{secret}/", response_class=HTMLResponse)
async def page(view_id: str, secret: str):
    meta = _view_or_404(view_id, secret)
    body = render.render(storage.read_markdown(view_id))
    title = meta.get("title") or meta.get("experiment") or "Experiment report"
    stamp = time.strftime("%H:%M", time.localtime(meta.get("received_at") or time.time()))
    tz = meta.get("tz") or ""
    html_doc = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{render._esc(title)}</title>
<link rel="stylesheet" href="/static/report.css">
</head><body>
<header class="bar">
  <div class="who">
    <strong>{render._esc(title)}</strong>
    <span class="sub">{render._esc(meta.get('beamline') or '')} &middot;
      read-only mirror</span>
  </div>
  <div class="when">
    <span id="stamp">updated {stamp}</span>
    <span class="sub" id="age">{_age(meta)}</span>
  </div>
</header>
<main id="report">{body}</main>
<footer class="foot">
  Times are beamline local{f" ({render._esc(tz)})" if tz else ""}.
  This page is a read-only copy pushed from B-PILOT; editing it is not possible
  and nothing here reaches the instrument.
</footer>
<script src="/static/live.js"></script>
</body></html>"""
    return HTMLResponse(html_doc, headers=READER_HEADERS)


@app.get("/r/{view_id}/{secret}/fragment", response_class=HTMLResponse)
async def fragment(view_id: str, secret: str):
    _view_or_404(view_id, secret)
    return HTMLResponse(render.render(storage.read_markdown(view_id)), headers=READER_HEADERS)


@app.get("/r/{view_id}/{secret}/version")
async def version(view_id: str, secret: str):
    """~80 bytes the page polls to decide whether to re-fetch.

    Polling rather than SSE, deliberately: a reverse proxy with nginx's default
    ``proxy_buffering on`` turns an event stream into nothing at all, and a
    polling fallback would have had to exist regardless. For a handful of
    collaborators this is strictly less to go wrong.
    """
    meta = _view_or_404(view_id, secret)
    return JSONResponse(
        {"sha256": meta.get("sha256") or "", "received_at": meta.get("received_at") or 0,
         "age": _age(meta)},
        headers=READER_HEADERS,
    )


@app.get("/r/{view_id}/{secret}/figures/{name}")
async def figure(view_id: str, secret: str, name: str):
    _view_or_404(view_id, secret)
    # A figure is served only while the current document still references it,
    # so hiding an entry in B-PILOT takes its pixels offline immediately --
    # see storage.references_figure.
    if not storage.references_figure(view_id, name):
        raise HTTPException(status_code=404, detail="not found")
    path = storage.figure_path(view_id, name)
    if not path:
        raise HTTPException(status_code=404, detail="not found")
    media = "image/png" if name.lower().endswith(".png") else "image/jpeg"
    return FileResponse(path, media_type=media, headers=READER_HEADERS)


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.get("/healthz")
async def healthz():
    return {"ok": True}
