#!/usr/bin/env python3
"""
Самостоятельный сервис: фоновый сборщик + HTTP-API + служебный дашборд.
Вся логика — в index_core.py; здесь только отдача наружу.

Запуск:  uvicorn app:app --host 127.0.0.1 --port 8787
         (на проде — за reverse-proxy трекера, см. INTEGRATION.md)

Эндпоинты (полный контракт — в API.md):
  GET /               служебный дашборд (для проверки; вкладку трекер рисует сам)
  GET /api/latest     последнее значение индекса по всем монетам (JSON)
  GET /api/stats      покрытие/статистика (JSON)
  GET /api/index.csv  индекс + биржевые средние за диапазон (CSV)
  GET /api/ticks.csv  сырые 15с-цены за диапазон (CSV)
  GET /api/db         консистентный снимок всей базы (.db)
"""

import csv
import io
import os
from contextlib import asynccontextmanager

import asyncio
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, StreamingResponse,
)
from starlette.background import BackgroundTask

import index_core as core


@asynccontextmanager
async def lifespan(app: FastAPI):
    core.init_db()
    task = asyncio.create_task(core.collector_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="MOEX Crypto Index Replicator", lifespan=lifespan)

# Read-only рыночные данные — разрешаем кросс-доменные GET, чтобы фронт трекера мог
# ходить к API даже если он на другом origin. За reverse-proxy (тот же домен) не мешает.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"],
)


# ============================== API =============================
@app.get("/api/latest")
def api_latest():
    return core.fetch_latest()


@app.get("/api/stats")
def api_stats():
    return core.fetch_stats()


def _csv_response(rows_iter, header, filename):
    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        for row in rows_iter:
            w.writerow(row)
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
    resp = StreamingResponse(gen(), media_type="text/csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@app.get("/api/index.csv")
def api_index_csv(
    coin: str | None = Query(None),
    frm: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    """Индекс + минутные средние по биржам. Без дат — последние сутки."""
    t_from, t_to = core.resolve_range(frm, to, 86400)
    return _csv_response(core.iter_index_rows(t_from, t_to, coin),
                         core.INDEX_HEADER, "index.csv")


@app.get("/api/ticks.csv")
def api_ticks_csv(
    coin: str | None = Query(None),
    frm: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    """Сырые 15с-цены. Без дат — последний час."""
    t_from, t_to = core.resolve_range(frm, to, 3600)
    return _csv_response(core.iter_ticks_rows(t_from, t_to, coin),
                         core.TICKS_HEADER, "ticks.csv")


@app.get("/api/db")
def api_db():
    """Снимок всей базы SQLite для полной перепроверки офлайн."""
    snap = core.snapshot_db()
    return FileResponse(snap, media_type="application/octet-stream",
                        filename="index_snapshot.db",
                        background=BackgroundTask(os.remove, snap))


# ============================ Дашборд ===========================
@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Криптоиндекс MOEX — реплика (служебный дашборд)</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;
         background:#0f1115; color:#e7e9ee; padding:24px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:#8b93a7; font-size:13px; margin-bottom:20px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr));
          gap:14px; margin-bottom:26px; }
  .card { background:#171a21; border:1px solid #262b36; border-radius:12px; padding:16px; }
  .coin { font-size:13px; color:#8b93a7; letter-spacing:.04em; }
  .val { font-size:26px; font-weight:650; margin:2px 0 10px; font-variant-numeric:tabular-nums; }
  .row { display:flex; justify-content:space-between; font-size:12.5px; color:#b9c0d0;
         font-variant-numeric:tabular-nums; padding:2px 0; }
  .row .w { color:#6f7787; }
  .tools { background:#171a21; border:1px solid #262b36; border-radius:12px; padding:16px; }
  .tools h2 { font-size:14px; margin:0 0 12px; }
  label { font-size:12px; color:#8b93a7; display:block; margin-bottom:4px; }
  input, select { background:#0f1115; color:#e7e9ee; border:1px solid #2c313d;
                  border-radius:8px; padding:7px 9px; font-size:13px; }
  .field { display:inline-block; margin:0 12px 12px 0; vertical-align:top; }
  a.btn { display:inline-block; background:#2b6cff; color:#fff; text-decoration:none;
          padding:8px 14px; border-radius:8px; font-size:13px; margin:4px 8px 4px 0; }
  a.btn.grey { background:#2c313d; }
  .muted { color:#6f7787; font-size:12px; }
  code { background:#0f1115; padding:1px 5px; border-radius:5px; border:1px solid #2c313d; }
</style>
</head>
<body>
  <h1>Криптоиндекс MOEX — реплика</h1>
  <div class="sub">Служебный дашборд для проверки. Веса: Binance 50% · Bybit 20% ·
    OKX 15% · Bitget 15% · шаг 15с · окно 60с · <span id="status">загрузка…</span></div>
  <div class="grid" id="cards"></div>
  <div class="tools">
    <h2>Выгрузка срезов (CSV)</h2>
    <div class="field"><label>Монета</label>
      <select id="coin"><option value="">все</option>
        <option>BTC</option><option>ETH</option><option>SOL</option>
        <option>TRX</option><option>XRP</option></select></div>
    <div class="field"><label>С (UTC)</label><input type="datetime-local" id="from"></div>
    <div class="field"><label>По (UTC)</label><input type="datetime-local" id="to"></div>
    <br>
    <a class="btn" id="dl-index" href="#">Скачать индекс + биржевые средние</a>
    <a class="btn grey" id="dl-ticks" href="#">Скачать сырые цены (15с)</a>
    <a class="btn grey" id="dl-db" href="#">Снимок всей базы (.db)</a>
    <p class="muted">Прямые ссылки: <code>api/latest</code>, <code>api/index.csv</code>,
       <code>api/ticks.csv</code>, <code>api/db</code>, <code>api/stats</code>.</p>
  </div>
<script>
// База API. Пусто = тот же путь, что и страница. За reverse-proxy под подпутём
// (напр. /crypto-index/) оставь пустым, если дашборд отдаётся с того же префикса.
const API_BASE = "";
const u = (p) => API_BASE + p;
const fmt = (x, d=2) => x == null ? "—" :
  Number(x).toLocaleString("en-US", {minimumFractionDigits:d, maximumFractionDigits:d});

async function tick() {
  try {
    const r = await fetch(u("api/latest"), {cache:"no-store"});
    const d = await r.json();
    if (!d.ts) { document.getElementById("status").textContent = "данных пока нет"; return; }
    document.getElementById("status").textContent = "обновлено " + d.iso;
    document.getElementById("cards").innerHTML = d.coins.map(c => `
      <div class="card">
        <div class="coin">${c.coin}/USDT · бирж: ${c.n_exchanges}/4</div>
        <div class="val">${fmt(c.index, c.index<5?4:2)}</div>
        <div class="row"><span>Binance <span class="w">50%</span></span><span>${fmt(c.binance,c.binance<5?4:2)}</span></div>
        <div class="row"><span>Bybit <span class="w">20%</span></span><span>${fmt(c.bybit,c.bybit<5?4:2)}</span></div>
        <div class="row"><span>OKX <span class="w">15%</span></span><span>${fmt(c.okx,c.okx<5?4:2)}</span></div>
        <div class="row"><span>Bitget <span class="w">15%</span></span><span>${fmt(c.bitget,c.bitget<5?4:2)}</span></div>
      </div>`).join("");
  } catch (e) { document.getElementById("status").textContent = "ошибка связи с API"; }
}
function toUnix(v){ return v ? Math.floor(new Date(v + "Z").getTime()/1000) : ""; }
function qs(base){
  const coin=document.getElementById("coin").value;
  const f=toUnix(document.getElementById("from").value);
  const t=toUnix(document.getElementById("to").value);
  const p=new URLSearchParams();
  if(coin)p.set("coin",coin); if(f)p.set("from",f); if(t)p.set("to",t);
  const s=p.toString(); return u(base)+(s?"?"+s:"");
}
function links(){
  document.getElementById("dl-index").href=qs("api/index.csv");
  document.getElementById("dl-ticks").href=qs("api/ticks.csv");
  document.getElementById("dl-db").href=u("api/db");
}
["coin","from","to"].forEach(id=>document.getElementById(id).addEventListener("change",links));
links(); tick(); setInterval(tick, 15000);
</script>
</body>
</html>"""
