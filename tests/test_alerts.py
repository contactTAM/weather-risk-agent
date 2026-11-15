import json

import httpx
import respx

from app import alerts, tools
from tests.test_tools import seeded  # noqa: F401


def _mark_fresh():
    """These tests are about the diff logic: record a successful refresh so the freshness guard passes."""
    from app.data.refresh import record_refresh
    from app.db import now_iso
    record_refresh(now_iso(), now_iso(), True, "2021-2025", [])


def test_score_changes_diff():
    prev = {("A", "heat"): (30.0, "low"), ("B", "heat"): (50.0, "moderate"), ("C", "heat"): (40.0, "moderate")}
    cur = {("A", "heat"): (34.0, "moderate"), ("B", "heat"): (56.0, "moderate"), ("C", "heat"): (41.0, "moderate"),
           ("D", "heat"): (90.0, "high")}  # D has no previous snapshot -> not an event
    ev = {e["hub_id"]: e for e in alerts.score_changes(prev, cur, 5)}
    assert set(ev) == {"A", "B"}
    assert ev["A"]["level_changed"] and ev["B"]["delta"] == 6.0


@respx.mock
def test_check_baseline_then_change_then_webhook(seeded, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.test/x")
    hook = respx.post("https://hooks.test/x").mock(return_value=httpx.Response(200))
    sev = {"id": "urn:nws:1", "event": "Hurricane Warning", "severity": "Extreme"}
    monkeypatch.setattr(alerts.nws, "active_alerts", lambda h: {"alerts": [sev] if h.id == "MIA" else []})

    _mark_fresh()
    first = alerts.check()
    assert first["baseline"] and first["score_changes"] == 0 and first["nws_alerts"] == 1

    # change the heat weights -> composite scores move -> alerts fire
    from app.scoring import load_config
    cfg = load_config()
    cfg = json.loads(json.dumps(cfg)); cfg["composite_weights"] = {"winter": 0.0, "wind": 0.0, "flood": 0.0, "heat": 1.0}
    p = tmp_path / "whatif.yaml"; p.write_text(json.dumps(cfg))
    monkeypatch.setenv("SCORING_CONFIG", str(p))
    second = alerts.check()
    assert second["score_changes"] > 0 and second["nws_alerts"] == 0   # same NWS alert not re-sent
    body = json.loads(hook.calls[-1].request.content)
    assert body["type"] == "score_change" and "drivers" in body
    from app import state
    assert sum(a["delivered"] for a in state.list_alerts(200)) == 1 + second["score_changes"]


def test_dry_run_writes_nothing(seeded, monkeypatch):  # noqa: F811
    monkeypatch.setattr(alerts.nws, "active_alerts", lambda h: {"alerts": []})
    _mark_fresh()
    alerts.check(dry_run=True)
    from app import state
    assert state.status()["snapshots_total_rows"] == 0


@respx.mock
def test_slack_webhook_gets_text_other_urls_get_json(monkeypatch):
    payload = {"type": "score_change", "hub_id": "PHX", "city": "Phoenix", "hazard": "composite",
               "old_score": 19.0, "new_score": 34.0, "delta": 15.0, "old_level": "low", "new_level": "moderate",
               "level_changed": True, "drivers": [{"label": "Days with max temp >= 35 C", "value": 137.4, "unit": "days/yr"}]}
    slack = respx.post("https://hooks.slack.com/services/T/B/x").mock(return_value=httpx.Response(200, text="ok"))
    other = respx.post("https://example.test/hook").mock(return_value=httpx.Response(200))
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")
    assert alerts._deliver(payload) == (True, None)
    body = json.loads(slack.calls[0].request.content)
    assert set(body) == {"text"} and "Phoenix" in body["text"] and "19.0 -> 34.0" in body["text"] and "low -> moderate" in body["text"]
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://example.test/hook")
    alerts._deliver(payload)
    assert json.loads(other.calls[0].request.content)["new_score"] == 34.0      # raw JSON unchanged
    nws_text = alerts.slack_text({"type": "nws_alert", "city": "Atlanta",
                                  "nws": {"severity": "Severe", "event": "Freeze Warning", "headline": "until 9 AM"}})
    assert "Freeze Warning" in nws_text and "Atlanta" in nws_text
