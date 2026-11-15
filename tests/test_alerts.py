import json

import httpx
import respx

from app import alerts, tools
from app.db import connect
from tests.test_tools import seeded  # noqa: F401


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
    with connect() as c:
        assert c.execute("SELECT count(*) FROM alerts WHERE delivered=1").fetchone()[0] == 1 + second["score_changes"]


def test_dry_run_writes_nothing(seeded, monkeypatch):  # noqa: F811
    monkeypatch.setattr(alerts.nws, "active_alerts", lambda h: {"alerts": []})
    alerts.check(dry_run=True)
    with connect() as c:
        assert c.execute("SELECT count(*) FROM scores_snapshot").fetchone()[0] == 0
