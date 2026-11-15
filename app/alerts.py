"""Risk-change alerts (bonus): scheduled job + POST /alerts/check.

Signals:
  1. score_change: a hub's hazard/composite score moved >= ALERT_SCORE_DELTA points, or its level changed,
     vs the last stored snapshot. (Historical scores move when data is refreshed, the baseline window rolls,
     FEMA adds/corrects declarations, or the scoring config changes.)
  2. nws_alert: a new Severe/Extreme NWS alert at a hub (live signal), sent once per NWS alert id.
Each alert is logged in the `alerts` table and POSTed as JSON to ALERT_WEBHOOK_URL (if set).
"""
from __future__ import annotations

import logging
import os

import httpx

from app import tools
from app.config import load_hubs
from app.data import nws
from app import state
from app.db import now_iso
from app.scoring import COMPOSITE, HAZARDS, top_drivers

log = logging.getLogger("alerts")
_scheduler = None


def _delta_threshold() -> float:
    return float(os.getenv("ALERT_SCORE_DELTA", "5"))


def score_changes(prev: dict, current: dict, threshold: float) -> list[dict]:
    """Pure diff: prev/current map (hub, hazard) -> (score, level). Returns change events."""
    events = []
    for key, (score, level) in current.items():
        if key not in prev:
            continue
        old_score, old_level = prev[key]
        delta = round(score - old_score, 1)
        if abs(delta) >= threshold or level != old_level:
            events.append({"hub_id": key[0], "hazard": key[1], "old_score": old_score, "new_score": score,
                           "delta": delta, "old_level": old_level, "new_level": level,
                           "level_changed": level != old_level})
    return events


def slack_text(payload: dict) -> str:
    """One readable line for Slack (incoming webhooks need `text`; they reject arbitrary JSON)."""
    if payload.get("type") == "nws_alert":
        n = payload.get("nws", {})
        return f":cloud_with_lightning: NWS {n.get('severity')} *{n.get('event')}* at the {payload.get('city')} hub. {n.get('headline') or ''}".strip()
    lvl = (f", {payload['old_level']} -> {payload['new_level']}" if payload.get("level_changed") else "")
    drivers = "; ".join(f"{d['label']}: {d['value']} {d['unit']}" for d in payload.get("drivers", [])[:3])
    return (f":warning: *{payload.get('city')} ({payload.get('hub_id')})* {payload.get('hazard')} risk "
            f"{payload.get('old_score')} -> {payload.get('new_score')} ({payload.get('delta'):+.1f}{lvl}). "
            f"Top drivers: {drivers}")


def _webhook_body(url: str, payload: dict) -> dict:
    fmt = os.getenv("ALERT_WEBHOOK_FORMAT", "auto").lower()
    if fmt == "slack" or (fmt == "auto" and url.startswith("https://hooks.slack.com/")):
        return {"text": slack_text(payload)}
    return payload


def _deliver(payload: dict) -> tuple[bool, str | None]:
    url = os.getenv("ALERT_WEBHOOK_URL")
    if not url:
        return False, "ALERT_WEBHOOK_URL not set (logged only)"
    try:
        r = httpx.post(url, json=_webhook_body(url, payload), timeout=10)
        r.raise_for_status()
        return True, None
    except Exception as e:
        return False, str(e)[:300]


def current_scores() -> tuple[dict, dict]:
    """(scores by hub, {(hub, hazard): (score, level)}) recomputed from the cache with the current config."""
    scores = tools.all_scores(force=True)
    current = {}
    for hid, s_ in scores.items():
        current[(hid, COMPOSITE)] = (s_.composite, s_.level)
        for hz in HAZARDS:
            current[(hid, hz)] = (s_.hazards[hz].score, s_.hazards[hz].level)
    return scores, current


class DataNotFresh(Exception):
    """The local cache wasn't refreshed recently: comparing it with the durable snapshot could fire
    false alerts (e.g. right after a restart restored the older seed)."""


def max_data_age_hours() -> float:
    return float(os.getenv("ALERT_MAX_DATA_AGE_H", "12"))


def ensure_fresh() -> None:
    from app.data import refresh as refresh_mod
    if refresh_mod.is_running():
        raise DataNotFresh("A data refresh is in progress; check again when it has finished.")
    age = refresh_mod.data_age_hours()
    if age is None:
        raise DataNotFresh("No successful data refresh in this cache yet (e.g. after a restart). "
                           "Run POST /refresh first, or POST /alerts/run to do both.")
    if age > max_data_age_hours():
        raise DataNotFresh(f"Data is {age:.1f} h old (limit {max_data_age_hours():g} h). Run POST /refresh first.")


def check(dry_run: bool = False, include_nws: bool = True, require_fresh: bool = True) -> dict:
    if require_fresh:
        ensure_fresh()
    ts = now_iso()
    scores, current = current_scores()
    hubs = {h.id: h for h in load_hubs()}
    prev_at, prev = state.last_snapshot()          # durable store (Postgres in production)
    events: list[dict] = []
    for ev in score_changes(prev, current, _delta_threshold()):
        ev |= {"type": "score_change", "city": hubs[ev["hub_id"]].city,
               "drivers": top_drivers(scores[ev["hub_id"]], ev["hazard"], n=3)}
        events.append(ev)

    if include_nws:
        for h in hubs.values():
            for a in nws.active_alerts(h)["alerts"]:
                if a.get("severity") in ("Severe", "Extreme") and a.get("id") \
                        and not state.nws_alert_seen(h.id, a["id"]):
                    events.append({"type": "nws_alert", "hub_id": h.id, "city": h.city, "nws": a})

    if not dry_run:
        state.save_snapshot(ts, [(k[0], k[1], v[0], v[1]) for k, v in current.items()])
        for ev in events:
            payload = {"source": "weather-risk-agent", "generated_at": ts, **ev}
            ok, err = _deliver(payload)
            ref = ev["nws"]["id"] if ev["type"] == "nws_alert" else None
            state.log_alert(ts, ev["hub_id"], ev["type"], payload, ref, ok, err)
    return {"checked_at": ts, "previous_snapshot_at": prev_at, "baseline": not prev, "dry_run": dry_run,
            "events": events, "score_changes": sum(e["type"] == "score_change" for e in events),
            "nws_alerts": sum(e["type"] == "nws_alert" for e in events)}


def refresh_then_check(force: bool = False) -> dict:
    """The scheduled flow: refresh the cache, then compare with the durable snapshot.
    If the refresh failed, the comparison is skipped (no alerts from stale data)."""
    from app.data.refresh import refresh
    r = refresh(force=force)
    summary = {k: r.get(k) for k in ("status", "ok", "started_at", "finished_at", "window")}
    if not r.get("ok"):
        errors = [e for e in r.get("report", []) if "weather_error" in e or "fema_error" in e]
        return {"refresh": summary, "refresh_errors": errors, "check": None,
                "error": "refresh failed or already running; comparison skipped"}
    return {"refresh": summary, "check": check()}


def start_scheduler() -> None:
    global _scheduler
    # Opt-in fallback for always-on hosts. Off by default: on a free host that sleeps, an external
    # scheduler (GitHub Actions, .github/workflows/alert-check.yml) is reliable; running both would double-check.
    hours = float(os.getenv("ALERT_SCHEDULE_HOURS", "0"))
    if hours <= 0 or _scheduler is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(lambda: log.info("scheduled refresh + alert check: %s", refresh_then_check()),
                       "interval", hours=hours, id="alert-check", max_instances=1, coalesce=True)
    _scheduler.start()


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
