"""Risk-change alerts (bonus): scheduled job + POST /alerts/check.

Signals:
  1. score_change: a hub's hazard/composite score moved >= ALERT_SCORE_DELTA points, or its level changed,
     vs the last stored snapshot. (Historical scores move when data is refreshed, the baseline window rolls,
     FEMA adds/corrects declarations, or the scoring config changes.)
  2. nws_alert: a new Severe/Extreme NWS alert at a hub (live signal), sent once per NWS alert id.
Each alert is logged in the `alerts` table and POSTed as JSON to ALERT_WEBHOOK_URL (if set).
"""
from __future__ import annotations

import json
import logging
import os

import httpx

from app import tools
from app.config import load_hubs
from app.data import nws
from app.db import connect, now_iso
from app.scoring import COMPOSITE, HAZARDS, top_drivers

log = logging.getLogger("alerts")
_scheduler = None


def _delta_threshold() -> float:
    return float(os.getenv("ALERT_SCORE_DELTA", "5"))


def _last_snapshot(conn) -> dict[tuple[str, str], tuple[float, str]]:
    row = conn.execute("SELECT max(computed_at) FROM scores_snapshot").fetchone()
    if not row or not row[0]:
        return {}
    return {(r["hub_id"], r["hazard"]): (r["score"], r["level"]) for r in conn.execute(
        "SELECT hub_id, hazard, score, level FROM scores_snapshot WHERE computed_at=?", (row[0],))}


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


def _deliver(payload: dict) -> tuple[bool, str | None]:
    url = os.getenv("ALERT_WEBHOOK_URL")
    if not url:
        return False, "ALERT_WEBHOOK_URL not set (logged only)"
    try:
        r = httpx.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True, None
    except Exception as e:
        return False, str(e)[:300]


def check(dry_run: bool = False, include_nws: bool = True) -> dict:
    ts = now_iso()
    scores = tools.all_scores(force=True)  # recompute from cache with current config
    current = {}
    for hid, s in scores.items():
        current[(hid, COMPOSITE)] = (s.composite, s.level)
        for hz in HAZARDS:
            current[(hid, hz)] = (s.hazards[hz].score, s.hazards[hz].level)

    hubs = {h.id: h for h in load_hubs()}
    events: list[dict] = []
    with connect() as conn:
        prev = _last_snapshot(conn)
        for ev in score_changes(prev, current, _delta_threshold()):
            ev |= {"type": "score_change", "city": hubs[ev["hub_id"]].city,
                   "drivers": top_drivers(scores[ev["hub_id"]], ev["hazard"], n=3)}
            events.append(ev)

        if include_nws:
            for h in hubs.values():
                res = nws.active_alerts(h)
                for a in res["alerts"]:
                    if a.get("severity") not in ("Severe", "Extreme") or not a.get("id"):
                        continue
                    seen = conn.execute("SELECT 1 FROM alerts WHERE kind='nws_alert' AND hub_id=? AND payload LIKE ?",
                                        (h.id, f'%"{a["id"]}"%')).fetchone()
                    if not seen:
                        events.append({"type": "nws_alert", "hub_id": h.id, "city": h.city, "nws": a})

        if not dry_run:
            conn.executemany("INSERT INTO scores_snapshot (computed_at, hub_id, hazard, score, level) VALUES (?,?,?,?,?)",
                             [(ts, k[0], k[1], v[0], v[1]) for k, v in current.items()])
            for ev in events:
                payload = {"source": "weather-risk-agent", "generated_at": ts, **ev}
                ok, err = _deliver(payload)
                conn.execute("INSERT INTO alerts (created_at, hub_id, kind, payload, delivered, delivery_error) VALUES (?,?,?,?,?,?)",
                             (ts, ev["hub_id"], ev["type"], json.dumps(payload), int(ok), err))
    return {"checked_at": ts, "baseline": not prev, "dry_run": dry_run, "events": events,
            "score_changes": sum(e["type"] == "score_change" for e in events),
            "nws_alerts": sum(e["type"] == "nws_alert" for e in events)}


def start_scheduler() -> None:
    global _scheduler
    hours = float(os.getenv("ALERT_SCHEDULE_HOURS", "6"))
    if hours <= 0 or _scheduler is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(lambda: log.info("alert check: %s", {k: v for k, v in check().items() if k != "events"}),
                       "interval", hours=hours, id="alert-check", max_instances=1, coalesce=True)
    _scheduler.start()


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
