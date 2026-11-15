"""Refresh-then-compare flow and restart survival. No network: fetchers and NWS are faked."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import alerts, api, state, tools
from app.config import analysis_window, load_hubs
from app.data import fema, open_meteo
from app.data import refresh as refresh_mod
from app.db import connect

CODE = {"x-access-code": "c0de"}


def fake_om_body(start: str, end: str, snow: float = 2.0) -> dict:
    d0 = datetime.fromisoformat(start)
    days = (datetime.fromisoformat(end) - d0).days + 1
    times = [(d0 + timedelta(days=i)).date().isoformat() for i in range(days)]
    return {"latitude": 40.0, "longitude": -90.0, "elevation": 100.0,
            "daily": {"time": times, "snowfall_sum": [snow if t[5:7] == "01" and t[8:] <= "10" else 0.0 for t in times],
                      "precipitation_sum": [0.0] * days, "temperature_2m_min": [5.0] * days,
                      "temperature_2m_max": [20.0] * days, "wind_gusts_10m_max": [10.0] * days}}


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """Separate cache and state files (so a 'restart' can wipe the cache only), fake fetchers, fake NWS."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cache.sqlite"))
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("ACCESS_CODE", "c0de")
    monkeypatch.setenv("LLM_MODEL", "fake-model")
    monkeypatch.setenv("REFRESH_PAUSE_S", "0")
    calls = {"weather": 0, "fema": 0}
    fema_rows = {"MIA": [{"disasterNumber": 1, "incidentType": "Hurricane", "declarationType": "DR",
                          "declarationDate": "2017-09-10T00:00:00.000Z", "designatedArea": "Miami-Dade"}]}

    def om_fetch(hub, start, end):
        calls["weather"] += 1
        return fake_om_body(start, end)

    def fema_fetch(hub):
        calls["fema"] += 1
        return {"DisasterDeclarationsSummaries": fema_rows.get(hub.id, [])}

    monkeypatch.setattr(open_meteo, "fetch", om_fetch)
    monkeypatch.setattr(fema, "fetch", fema_fetch)
    monkeypatch.setattr(alerts.nws, "active_alerts", lambda h: {"alerts": []})
    tools.invalidate_scores()
    yield {"calls": calls, "fema_rows": fema_rows, "tmp": tmp_path}
    tools.invalidate_scores()


def test_refresh_fetches_weather_only_when_window_changes(offline):
    r1 = refresh_mod.refresh(pause_s=0)
    assert r1["ok"] and offline["calls"]["weather"] == 14 and offline["calls"]["fema"] == 14
    r2 = refresh_mod.refresh(pause_s=0)                       # same window: weather cached, FEMA re-fetched
    assert offline["calls"]["weather"] == 14 and offline["calls"]["fema"] == 28
    assert all(e["weather_days"] == "cached" for e in r2["report"])
    assert not refresh_mod.window_changed()


def test_window_roll_triggers_weather_refetch(offline, monkeypatch):
    refresh_mod.refresh(pause_s=0)
    monkeypatch.setenv("ANALYSIS_END_YEAR", "2026")          # January roll
    assert refresh_mod.window_changed()
    refresh_mod.refresh(pause_s=0)
    assert offline["calls"]["weather"] == 28 and not refresh_mod.window_changed()


def test_check_refuses_without_fresh_refresh(offline, monkeypatch):
    with pytest.raises(alerts.DataNotFresh, match="No successful data refresh"):
        alerts.check()
    old = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat(timespec="seconds")
    refresh_mod.record_refresh(old, old, True, "2021-2025", [])
    with pytest.raises(alerts.DataNotFresh, match="13"):
        alerts.check()
    refresh_mod.record_refresh(old, old, False, "2021-2025", [{"fema_error": "x"}])  # failed runs don't count
    with pytest.raises(alerts.DataNotFresh):
        alerts.check()


def test_refresh_then_check_order_and_skip_on_failure(offline, monkeypatch):
    out = alerts.refresh_then_check()
    assert out["refresh"]["ok"] and out["check"]["baseline"] is True
    # a new FEMA declaration for Miami -> next refresh+check reports the change
    offline["fema_rows"]["MIA"].extend({"disasterNumber": n, "incidentType": "Hurricane", "declarationType": "DR",
                                        "declarationDate": "2024-10-09T00:00:00.000Z", "designatedArea": "Miami-Dade"}
                                       for n in (2, 3))
    out2 = alerts.refresh_then_check()
    changed = {(e["hub_id"], e["hazard"]) for e in out2["check"]["events"]}
    assert ("MIA", "wind") in changed and out2["check"]["baseline"] is False
    # a failing fetch -> comparison is skipped, nothing compared against partial data
    monkeypatch.setattr(fema, "fetch", lambda hub: (_ for _ in ()).throw(RuntimeError("FEMA down")))
    out3 = alerts.refresh_then_check()
    assert out3["check"] is None and "skipped" in out3["error"] and out3["refresh_errors"]


def test_snapshot_survives_restart_and_no_false_alerts(offline, monkeypatch):
    """Production sequence on an ephemeral host: the cache is wiped and restored from the seed,
    the durable state keeps the snapshot, the check refuses until a refresh, then no false alerts."""
    from app.data import seed
    monkeypatch.setattr(seed, "SEED", offline["tmp"] / "seed.sqlite.gz")
    alerts.refresh_then_check()
    seed.build()                                              # the shipped seed = same public data
    at, snap = state.last_snapshot()
    assert at and len(snap) == 14 * 5

    # --- simulated restart: the cache file disappears, the state store does not ---
    (offline["tmp"] / "cache.sqlite").unlink()
    tools.invalidate_scores()
    with TestClient(api.app) as client:                       # startup restores the seed (window unchanged)
        st = client.get("/alerts/status").json()
        assert st["last_snapshot_at"] == at and st["last_snapshot_rows"] == 70   # survived
        assert st["data_fresh"] is False
        assert client.post("/alerts/check", headers=CODE).status_code == 409    # no compare on seed data
        assert client.post("/refresh", headers=CODE).status_code == 202          # runs as a background task
        assert client.get("/refresh").json()["last"]["ok"] is True
        r = client.post("/alerts/check", headers=CODE)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["baseline"] is False and body["previous_snapshot_at"] == at
        assert body["score_changes"] == 0                       # same data -> no false alerts


def test_api_409_until_refreshed(offline, monkeypatch):
    with connect() as c:                                      # cache present, never refreshed in this "process"
        for h in load_hubs():
            w = analysis_window()
            c.execute("INSERT INTO weather_meta VALUES (?,?,?,?,?,?,?)", (h.id, 1, 1, 1, w.weather_start, w.weather_end, "x"))
    with TestClient(api.app) as client:
        r = client.post("/alerts/check", headers=CODE)
        assert r.status_code == 409 and "refresh" in r.json()["detail"].lower()
        assert client.get("/alerts/status").json()["data_fresh"] is False
        refresh_mod.refresh(pause_s=0)
        assert client.post("/alerts/check", headers=CODE).status_code == 200
        st = client.get("/alerts/status").json()
        assert st["data_fresh"] and st["state_backend"] == "sqlite" and st["window"] == "2021-2025"
