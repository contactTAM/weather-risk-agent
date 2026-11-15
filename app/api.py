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
from app.db import connect
from app.schemas import AgentAnswer, ChatRequest

_run_state: dict = {"running": False, "last": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    llm_model()  # fail fast with a clear message if LLM_MODEL is not configured
    from app.data.seed import ensure_db
    ensure_db()  # fresh clone / cloud deploy: restore the shipped data snapshot
    from app.data import refresh as refresh_mod
    if refresh_mod.window_changed():  # e.g. cold start after the January roll: fetch the new window
        threading.Thread(target=refresh_mod.refresh, daemon=True, name="startup-refresh").start()
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
            "refresh_running": __import__("app.data.refresh", fromlist=["x"]).is_running(), "access_code_required": bool(os.getenv("ACCESS_CODE")),
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


@app.post("/refresh", status_code=202, dependencies=[Depends(require_access)])
def post_refresh(background: BackgroundTasks, force: bool = False) -> dict:
    from app.data import refresh as refresh_mod
    if refresh_mod.is_running():
        return {"status": "already_running"}
    background.add_task(refresh_mod.refresh, None, force)
    return {"status": "started", "force": force, "note": "Poll GET /refresh until running is false."}


@app.get("/refresh")
def get_refresh() -> dict:
    from app.data import refresh as refresh_mod
    return {"running": refresh_mod.is_running(), "last": refresh_mod.last_result(),
            "last_successful_refresh": refresh_mod.last_successful_refresh()}


@app.post("/alerts/check", dependencies=[Depends(require_access)])
def alerts_check(dry_run: bool = False) -> dict:
    """Compare current scores with the durable snapshot. 409 if the local data isn't fresh."""
    from app import alerts
    try:
        return alerts.check(dry_run=dry_run)
    except alerts.DataNotFresh as e:
        raise HTTPException(409, str(e))


def _run_refresh_then_check(force: bool) -> None:
    from app import alerts
    _run_state["running"] = True
    try:
        _run_state["last"] = alerts.refresh_then_check(force=force)
    finally:
        _run_state["running"] = False


@app.post("/alerts/run", status_code=202, dependencies=[Depends(require_access)])
def alerts_run(background: BackgroundTasks, force: bool = False) -> dict:
    """Refresh, then compare, in the background (one call). Poll GET /alerts/run."""
    if _run_state["running"]:
        return {"status": "already_running"}
    background.add_task(_run_refresh_then_check, force)
    return {"status": "started"}


@app.get("/alerts/run")
def alerts_run_status() -> dict:
    return _run_state


@app.get("/alerts/status")
def alerts_status() -> dict:
    """Durable state + data freshness, for monitoring and for checking that state survives restarts."""
    from app import alerts, state
    from app.config import analysis_window
    from app.data import refresh as refresh_mod
    age = refresh_mod.data_age_hours()
    return {**state.status(), "window": analysis_window().label,
            "last_successful_refresh": refresh_mod.last_successful_refresh(),
            "data_age_hours": round(age, 2) if age is not None else None,
            "data_fresh": age is not None and age <= alerts.max_data_age_hours(),
            "refresh_running": refresh_mod.is_running()}


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
