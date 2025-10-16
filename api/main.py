# main.py
import os
import time
import json
import hashlib
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from openai import OpenAI

# ---- Config ----
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_REQ_PER_MIN = int(os.getenv("MAX_REQ_PER_MIN", "60"))
WINDOW_SEC = 60

EXT_IDS = [s.strip() for s in os.getenv("EXT_IDS", "").split(",") if s.strip()]
ALLOWED_ORIGINS = [f"chrome-extension://{eid}" for eid in EXT_IDS] or ["*"]

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "changeme")
IP_SALT = os.getenv("IP_SALT", "pepper")

client = OpenAI()

app = FastAPI(title="summarize-selection")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

# ---- psycopg (binary) async pool ----
from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

POOL: Optional[AsyncConnectionPool] = None

DDL = """
CREATE TABLE IF NOT EXISTS pings (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  user_id TEXT NOT NULL,
  action TEXT NOT NULL,
  ext_version TEXT,
  ua TEXT,
  ip_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_pings_ts ON pings (ts);
CREATE INDEX IF NOT EXISTS idx_pings_user_ts ON pings (user_id, ts);
CREATE INDEX IF NOT EXISTS idx_pings_action_ts ON pings (action, ts);
"""

@app.on_event("startup")
async def init_db():
    global POOL
    if not DATABASE_URL:
        return
    # Note: psycopg uses $PG* vars in the DSN; we pass DATABASE_URL directly.
    POOL = AsyncConnectionPool(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=5,
        kwargs={"row_factory": dict_row},
    )
    async with POOL.connection() as conn:
        async with conn.transaction():
            await conn.execute(DDL)

async def get_pool() -> AsyncConnectionPool:
    if POOL is None:
        raise HTTPException(500, "Database not initialized")
    return POOL

def require_admin(req: Request):
    if req.headers.get("x-admin-token") != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

def hash_ip(ip: str) -> str:
    return hashlib.sha256(f"{IP_SALT}:{ip}".encode()).hexdigest()

# ---- Middleware ----
@app.middleware("http")
async def timing_header(request: Request, call_next):
    t0 = time.time()
    resp = await call_next(request)
    resp.headers["X-Response-Time-ms"] = str(int((time.time() - t0) * 1000))
    return resp

# ---- Rate Limit ----
_buckets: defaultdict[str, deque] = defaultdict(deque)

def allow_ip(ip: str) -> bool:
    now = time.time()
    q = _buckets[ip]
    while q and now - q[0] > WINDOW_SEC:
        q.popleft()
    if len(q) >= MAX_REQ_PER_MIN:
        return False
    q.append(now)
    return True

# ---- Models ----
class SummarizeRequest(BaseModel):
    text: str = Field(min_length=1)
    tone: str = Field(default="precise", pattern=r"^[a-zA-Z\- ]{1,32}$")
    maxSentences: int = Field(default=3, ge=1, le=10)

class SummarizeResponse(BaseModel):
    summary: str

class Ping(BaseModel):
    id: str
    action: str = "summary"     # 'install' | 'successful_summary' | 'error' | 'summary'
    ext_version: Optional[str] = None

# ---- Utils ----
def chunk(text: str, max_chars: int = 6000) -> List[str]:
    t = text.strip()
    if len(t) <= max_chars:
        return [t]
    parts: List[str] = []
    start = 0
    while start < len(t):
        end = min(len(t), start + max_chars)
        cut = max(t.rfind("\n\n", start, end), t.rfind(". ", start, end))
        if cut == -1 or cut <= start + int(max_chars * 0.4):
            cut = end
        parts.append(t[start:cut].strip())
        start = cut
    return [p for p in parts if p]

def summarize_chunk(txt: str, tone: str, max_sent: int) -> str:
    prompt = (
        f"Summarize the following text in at most {max_sent} sentences. "
        f"Tone: {tone}. Focus on key facts. Avoid fluff.\n\nTEXT:\n{txt}"
    )
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip()

# ---- Routes ----
@app.get("/")
def root():
    return {
        "ok": True,
        "service": "summarize-selection",
        "docs": "/docs",
        "analytics": "/analytics/now",
        "dashboard": "/dashboard",
    }

@app.post("/summarize", response_model=SummarizeResponse)
async def summarize(req: SummarizeRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not allow_ip(ip):
        raise HTTPException(status_code=429, detail="Too many requests")

    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    try:
        pieces = chunk(text)
        if len(pieces) == 1:
            out = summarize_chunk(pieces[0], req.tone, req.maxSentences)
        else:
            partials = [summarize_chunk(p, req.tone, req.maxSentences) for p in pieces]
            stitched = "\n\n".join(partials)
            out = summarize_chunk(
                f"Combine and condense these partial summaries into at most {req.maxSentences} sentences:\n\n{stitched}",
                req.tone,
                req.maxSentences,
            )
        return {"summary": out or "(no summary produced)"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Summarization failed: {e}")

# ---- Ingest ----
@app.post("/ping")
async def ping(req: Ping, request: Request, pool: AsyncConnectionPool = Depends(get_pool)):
    try:
        ip = request.client.host or "0.0.0.0"
        ua = request.headers.get("user-agent", "")
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO pings (user_id, action, ext_version, ua, ip_hash) VALUES (%s, %s, %s, %s, %s)",
                (req.id, req.action, req.ext_version, ua, hash_ip(ip)),
            )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"ping failed: {e}")

# ---- Aggregates ----
async def fetchval(conn, sql: str, params: tuple = ()) -> int:
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        row = await cur.fetchone()
        return (row[0] if row and row[0] is not None else 0)

@app.get("/analytics/now")
async def analytics_now(request: Request, pool: AsyncConnectionPool = Depends(get_pool)):
    require_admin(request)

    now = datetime.utcnow()
    one_min = now - timedelta(minutes=1)
    five_min = now - timedelta(minutes=5)
    day = now - timedelta(days=1)

    async with pool.connection() as conn:
        lifetime_installs = await fetchval(conn,
            "SELECT COUNT(DISTINCT user_id) FROM pings WHERE action='install'")
        installs_24h = await fetchval(conn,
            "SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM pings WHERE action='install' AND ts >= %s) t",
            (day,))
        active_5m = await fetchval(conn,
            "SELECT COUNT(DISTINCT user_id) FROM pings WHERE action='successful_summary' AND ts >= %s",
            (five_min,))
        summaries_1m = await fetchval(conn,
            "SELECT COUNT(*) FROM pings WHERE action='successful_summary' AND ts >= %s",
            (one_min,))
        summaries_5m = await fetchval(conn,
            "SELECT COUNT(*) FROM pings WHERE action='successful_summary' AND ts >= %s",
            (five_min,))
        errors_5m = await fetchval(conn,
            "SELECT COUNT(*) FROM pings WHERE action='error' AND ts >= %s",
            (five_min,))
        # version mix (24h)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COALESCE(ext_version,'unknown') v, COUNT(*) c FROM pings WHERE ts >= %s GROUP BY v ORDER BY c DESC",
                (day,),
            )
            versions = [{"version": r[0], "count": r[1]} async for r in cur]

    error_rate_5m = (errors_5m or 0) / max(1, (summaries_5m or 0) + (errors_5m or 0))

    return JSONResponse({
        "lifetime_installs": int(lifetime_installs or 0),
        "installs_24h": int(installs_24h or 0),
        "active_users_5m": int(active_5m or 0),
        "summaries_per_min": int(summaries_1m or 0),
        "summaries_5m": int(summaries_5m or 0),
        "errors_5m": int(errors_5m or 0),
        "error_rate_5m": round(error_rate_5m, 4),
        "version_mix_24h": versions,
        "as_of_utc": now.isoformat() + "Z"
    })

@app.get("/analytics")
async def analytics_alias(request: Request, pool: AsyncConnectionPool = Depends(get_pool)):
    return await analytics_now(request, pool)

# ---- Dashboard ----
DASHBOARD_HTML = """
<!doctype html><meta charset="utf-8">
<title>Summarize Sidekick – Live</title>
<style>
  body{font-family: system-ui,-apple-system,Segoe UI,Roboto; margin:24px;}
  .grid{display:grid; grid-template-columns: repeat(3, minmax(220px,1fr)); gap:16px;}
  .card{border:1px solid #e5e7eb; border-radius:12px; padding:16px;}
  .k{color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:.06em}
  .v{font-size:28px;font-weight:700;margin-top:6px}
  .small{font-size:12px;color:#6b7280;margin-top:6px}
</style>
<div id="updated" class="small">loading…</div>
<div class="grid">
  <div class="card"><div class="k">Lifetime installs</div><div class="v" id="lifetime"></div></div>
  <div class="card"><div class="k">Installs (24h)</div><div class="v" id="inst24"></div></div>
  <div class="card"><div class="k">Active users (5m)</div><div class="v" id="active5"></div></div>
  <div class="card"><div class="k">Summaries/min (live)</div><div class="v" id="spm"></div></div>
  <div class="card"><div class="k">Summaries (5m)</div><div class="v" id="s5"></div></div>
  <div class="card"><div class="k">Error rate (5m)</div><div class="v" id="err"></div></div>
</div>
<div class="card" style="margin-top:16px">
  <div class="k">Version mix (24h)</div>
  <ul id="ver"></ul>
</div>
<script>
const TOKEN = localStorage.getItem("ADMIN_TOKEN") || prompt("Admin token:");
if (TOKEN) localStorage.setItem("ADMIN_TOKEN", TOKEN);

async function tick(){
  try{
    const r = await fetch("/analytics/now", {headers: {"x-admin-token": TOKEN}});
    if(!r.ok){ document.getElementById('updated').innerText = "Unauthorized / bad token"; return; }
    const d = await r.json();
    const set = (id, val) => document.getElementById(id).innerText = val;
    set('lifetime', d.lifetime_installs);
    set('inst24', d.installs_24h);
    set('active5', d.active_users_5m);
    set('spm', d.summaries_per_min);
    set('s5', d.summaries_5m);
    set('err', (d.error_rate_5m*100).toFixed(1) + "%");
    const ul = document.getElementById('ver'); ul.innerHTML = "";
    (d.version_mix_24h || []).forEach(v => {
      const li = document.createElement('li');
      li.textContent = `${v.version}: ${v.count}`;
      ul.appendChild(li);
    });
    document.getElementById('updated').innerText = "Updated " + new Date(d.as_of_utc).toLocaleTimeString();
  }catch(e){
    document.getElementById('updated').innerText = "Error: " + e.message;
  }
}
tick(); setInterval(tick, 5000);
</script>
"""

@app.get("/dashboard")
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)

# Run local:
# uvicorn main:app --host 0.0.0.0 --port 8000
