"""FastAPI app: webhooks, health, stats, exports, status dashboard."""
import hashlib
import hmac
import io
import json
import os
import time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import ValidationError

from .models import MODELS
from .pipeline import COLS, Pipeline, from_env

DASH = """<!doctype html><meta charset=utf-8><title>Order Ops</title>
<style>body{font:14px system-ui;margin:2rem auto;max-width:900px;padding:0 1rem}
.c{display:inline-block;margin:0 1rem 1rem 0;padding:.6rem 1rem;border:1px solid #8884;border-radius:8px}
.c b{display:block;font-size:1.6rem}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #8883;padding:4px 8px;text-align:left}.bad{color:#c0392b}</style>
<h1>Order Ops status</h1><div id=cards></div><h3>Flags</h3><div id=flags></div>
<h3>Recent events</h3><table id=t></table>
<p><a href=/export.xlsx>XLSX</a> · <a href=/export.csv>CSV</a> · <a href=/health>health</a></p>
<script>
const $=id=>document.getElementById(id);
function td(r,v){const e=document.createElement('td');e.textContent=v??'';r.appendChild(e)}
async function tick(){try{const s=await (await fetch('/api/stats')).json();
$('cards').replaceChildren(...[['events',s.total],['notified',s.notified],['avg risk',s.avg_risk],
['DLQ open',s.dlq_open]].map(([k,v])=>{const d=document.createElement('div');d.className='c'+(k=='DLQ open'&&v?' bad':'');
d.textContent=k;const b=document.createElement('b');b.textContent=v;d.prepend(b);return d}));
$('flags').textContent=Object.entries(s.flags).map(([k,v])=>k+': '+v).join('  ·  ')||'none';
$('t').replaceChildren(...s.recent.map(e=>{const r=document.createElement('tr');
[e.created_at,e.kind,e.ref,e.email,e.total,e.risk,e.flags,e.status].forEach(v=>td(r,v));return r}))}catch(e){}}
tick();setInterval(tick,3000)</script>"""


def create_app(pipeline: Pipeline | None = None) -> FastAPI:
    p = pipeline or from_env()
    app = FastAPI(title="Order Ops Automation")
    started = time.time()
    secret = os.getenv("OPS_WEBHOOK_SECRET", "")

    @app.post("/webhook/{kind}")
    async def webhook(kind: str, request: Request, idempotency_key: str | None = Header(None),
                      x_signature: str | None = Header(None)):
        if kind not in MODELS:
            raise HTTPException(404, f"unknown kind '{kind}'")
        body = await request.body()
        if secret:
            good = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(good, x_signature or ""):
                p.store.audit("webhook", "bad_signature", "")
                raise HTTPException(401, "bad signature")
        try:
            raw = json.loads(body)
            res = await run_in_threadpool(p.handle, kind, raw, idempotency_key)
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON")
        except ValidationError as e:
            p.store.audit("webhook", "rejected", "", {"kind": kind, "errors": len(e.errors())})
            return JSONResponse({"status": "rejected", "errors": json.loads(e.json(include_url=False, include_context=False))}, 422)
        return JSONResponse(res, 200 if res["status"] == "done" else 202)

    @app.get("/health")
    def health():
        try:
            return {"status": "ok", "dlq_open": len(p.store.dlq_open()), "uptime_s": round(time.time() - started)}
        except Exception as e:
            return JSONResponse({"status": "down", "error": str(e)}, 503)

    @app.get("/api/stats")
    def stats():
        return p.store.stats()

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return DASH

    @app.get("/export.csv")
    def export_csv():
        if not os.path.exists(p.export_csv):
            raise HTTPException(404, "no export yet")
        return FileResponse(p.export_csv, media_type="text/csv")

    @app.get("/export.xlsx")
    def export_xlsx():
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(COLS)
        for e in reversed(p.store.events()):
            ws.append([e.get(c) for c in COLS])
        buf = io.BytesIO()
        wb.save(buf)
        return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": "attachment; filename=orders.xlsx"})

    return app


def app_factory() -> FastAPI:  # uvicorn opsauto.app:app_factory --factory
    return create_app()
