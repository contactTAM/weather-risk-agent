"""FastAPI app. The chat UI (static/) talks only to these endpoints."""
from __future__ import annotations

import hmac
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import agent, tools
from app.config import ROOT, llm_model, load_hubs
from app.data.refresh import refresh
from app.db import connect
from app.schemas import AgentAnswer, ChatRequest

_refresh_lock = threading.Lock()
_refresh_state: dict = {"running": False, "last_report": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    llm_model()  # fail fast with a clear message if LLM_MODEL is not configured
    from app.data.seed import ensure_db
    ensure_db()  # fresh clone / cloud deploy: restore the shipped data snapshot
    from app import alerts
    alerts.start_scheduler()
    yield
    alerts.stop_scheduler()


app = FastAPI(title="Weather Risk Intelligence Agent", version="0.1.0", lifespan=lifespan)


# ---------- protection for a public deploy (off when ACCESS_CODE is unset, e.g. locally) ----------
def require_access(request: Request) -> None:
    """Endpoints that spend LLM credits or change state need X-Access-Code when ACCESS_CODE is set."""
    code = os.getenv("ACCESS_CODE", "")
    if code and not hmac.compare_digest(request.headers.get("x-access-code", ""), code):
        raise HTTPException(401, "Access code required")


_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(request: Request) -> None:
    """Simple in-memory sliding window per client IP (single instance is enough here)."""
    limit = int(os.getenv("CHAT_RATE_LIMIT", "20"))
    window = int(os.getenv("CHAT_RATE_WINDOW_S", "600"))
    if limit <= 0:
        return
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else "unknown"))
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, f"Rate limit: {limit} questions per {window // 60} minutes. Try again later.")
    q.append(now)


@app.get("/health")
def health() -> dict:
    with connect() as c:
        days = c.execute("SELECT count(*) FROM daily_weather").fetchone()[0]
        hubs_with_data = c.execute("SELECT count(*) FROM weather_meta").fetchone()[0]
        fetched = c.execute("SELECT max(fetched_at) FROM weather_meta").fetchone()[0]
    return {"status": "ok" if hubs_with_data == len(load_hubs()) else "degraded",
            "hubs_configured": len(load_hubs()), "hubs_with_weather": hubs_with_data,
            "daily_rows": days, "weather_fetched_at": fetched, "model": llm_model(),
            "refresh_running": _refresh_state["running"], "access_code_required": bool(os.getenv("ACCESS_CODE")),
            "commit": (os.getenv("RENDER_GIT_COMMIT") or "")[:7] or None,
            "state_backend": __import__("app.state", fromlist=["x"]).backend_name()}


@app.post("/chat", response_model=AgentAnswer, dependencies=[Depends(require_access), Depends(rate_limit)])
def chat(req: ChatRequest) -> AgentAnswer:  # sync def -> FastAPI runs it in a threadpool
    return agent.chat(req.session_id, req.message)


@app.get("/hubs")
def hubs(region: str | None = None) -> dict:
    out = tools.run_tool("list_hubs", {"region": region})
    if "error" in out:
        raise HTTPException(422, out)
    return out


@app.get("/scores")
def scores(hazard: Literal["composite", "winter", "wind", "flood", "heat"] = "composite",
           region: str | None = None, top_n: int | None = Query(None, ge=1, le=14)) -> dict:
    out = tools.run_tool("rank_hubs", {"hazard": hazard, "region": region, "top_n": top_n})
    if "error" in out:
        raise HTTPException(422, out)
    return out


@app.get("/hubs/{hub_id}/breakdown")
def breakdown(hub_id: str) -> dict:
    out = tools.run_tool("get_hub_risk_breakdown", {"hub_id": hub_id})
    if out.get("error") == "hub_not_found":
        raise HTTPException(404, out)
    return out


@app.get("/hubs/{hub_id}/alerts")
def hub_alerts(hub_id: str) -> dict:
    out = tools.run_tool("get_active_alerts", {"hub_id": hub_id})
    if out.get("error") == "hub_not_found":
        raise HTTPException(404, out)
    return out


def _do_refresh(force: bool) -> None:
    with _refresh_lock:
        _refresh_state["running"] = True
        try:
            _refresh_state["last_report"] = refresh(force=force)
            tools.invalidate_scores()
        finally:
            _refresh_state["running"] = False


@app.post("/refresh", status_code=202, dependencies=[Depends(require_access)])
def post_refresh(background: BackgroundTasks, force: bool = False) -> dict:
    if _refresh_state["running"]:
        return {"status": "already_running"}
    background.add_task(_do_refresh, force)
    return {"status": "started", "force": force, "note": "Poll GET /refresh for the report."}


@app.get("/refresh")
def get_refresh() -> dict:
    return _refresh_state


@app.post("/alerts/check", dependencies=[Depends(require_access)])
def alerts_check(dry_run: bool = False) -> dict:
    from app import alerts
    return alerts.check(dry_run=dry_run)


@app.get("/alerts")
def alerts_list(limit: int = Query(20, ge=1, le=200)) -> dict:
    from app import state
    return {"alerts": state.list_alerts(limit)}


# Local webhook receiver so the alert flow can be demoed without an external service:
# set ALERT_WEBHOOK_URL=http://localhost:8000/alerts/echo
_echo: list[dict] = []


@app.post("/alerts/echo")
def alerts_echo(payload: dict) -> dict:
    _echo.append(payload)
    del _echo[:-50]
    return {"received": True}


@app.get("/alerts/echo")
def alerts_echo_list() -> dict:
    return {"received": _echo}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
