"""Data layer tests with mocked HTTP (respx). Payload shapes copied from real responses (2026-11-15)."""
import httpx
import respx

from app.config import get_hub
from app.data import fema, http, nws, open_meteo
from app.db import connect

OM_BODY = {
    "latitude": 39.754, "longitude": -105.02, "elevation": 1597.0,
    "daily_units": {"snowfall_sum": "cm"},
    "daily": {"time": ["2025-01-01", "2025-01-02"], "snowfall_sum": [0.0, 3.5],
              "precipitation_sum": [0.0, 2.1], "temperature_2m_min": [-7.5, -3.5],
              "temperature_2m_max": [3.5, 7.9], "wind_gusts_10m_max": [16.2, 61.0]},
}
FEMA_BODY = {"DisasterDeclarationsSummaries": [
    {"disasterNumber": 4229, "incidentType": "Flood", "declarationType": "DR",
     "declarationDate": "2015-07-16T00:00:00.000Z", "designatedArea": "Denver (County)"},
    {"disasterNumber": 4229, "incidentType": "Flood", "declarationType": "DR",  # duplicate row
     "declarationDate": "2015-07-16T00:00:00.000Z", "designatedArea": "Denver (County)"},
    {"disasterNumber": 4498, "incidentType": "Biological", "declarationType": "DR",
     "declarationDate": "2020-03-28T00:00:00.000Z", "designatedArea": "Denver (County)"},
]}
NWS_BODY = {"type": "FeatureCollection", "features": [
    {"properties": {"event": "Winter Storm Warning", "severity": "Severe", "urgency": "Expected",
                    "certainty": "Likely", "headline": "Winter Storm Warning until 6 PM",
                    "onset": "2026-11-15T06:00:00-07:00", "ends": "2026-11-15T18:00:00-07:00",
                    "senderName": "NWS Boulder CO"}}]}


def test_open_meteo_parse_and_store(tmp_db):
    hub = get_hub("DEN")
    rows = open_meteo.parse_daily(OM_BODY)
    assert rows[1] == ("2025-01-02", 3.5, 2.1, -3.5, 7.9, 61.0)
    with connect() as c:
        assert open_meteo.store(c, hub, OM_BODY, "2025-01-01", "2025-01-02") == 2
        meta = c.execute("SELECT grid_lat, elevation_m FROM weather_meta WHERE hub_id='DEN'").fetchone()
        assert tuple(meta) == (39.754, 1597.0)


@respx.mock
def test_open_meteo_fetch_sends_verified_params():
    route = respx.get(open_meteo.URL).mock(return_value=httpx.Response(200, json=OM_BODY))
    open_meteo.fetch(get_hub("DEN"), "2025-01-01", "2025-01-02")
    q = route.calls[0].request.url.params
    assert q["daily"] == "snowfall_sum,precipitation_sum,temperature_2m_min,temperature_2m_max,wind_gusts_10m_max"
    assert q["timezone"] == "America/Denver"


def test_fema_dedupes_disasters_and_maps_hazards(tmp_db):
    rows = fema.parse(FEMA_BODY)
    assert sorted(r["disasterNumber"] for r in rows) == [4229, 4498]
    assert fema.hazard_of("Flood") == "flood"
    assert fema.hazard_of("Hurricane") == "wind"
    assert fema.hazard_of("Severe Ice Storm") == "winter"
    assert fema.hazard_of("Biological") is None
    with connect() as c:
        assert fema.store(c, get_hub("DEN"), FEMA_BODY) == 2


@respx.mock
def test_fema_filter_uses_county_fips():
    route = respx.get(fema.URL).mock(return_value=httpx.Response(200, json=FEMA_BODY))
    fema.fetch(get_hub("MIA"))
    flt = route.calls[0].request.url.params["$filter"]
    assert "fipsStateCode eq '12'" in flt and "fipsCountyCode eq '086'" in flt


@respx.mock
def test_nws_sends_user_agent_and_falls_back_to_cache(tmp_db):
    route = respx.get(nws.URL).mock(return_value=httpx.Response(200, json=NWS_BODY))
    out = nws.active_alerts(get_hub("DEN"), use_cache=False)
    assert route.calls[0].request.headers["User-Agent"]
    assert out["alerts"][0]["event"] == "Winter Storm Warning" and not out["stale"]
    # API down -> serve the cached copy, flagged stale
    respx.get(nws.URL).mock(side_effect=httpx.ConnectError("down"))
    out2 = nws.active_alerts(get_hub("DEN"), use_cache=False)
    assert out2["stale"] and out2["alerts"][0]["severity"] == "Severe"


@respx.mock
def test_http_retries_after_rate_limit(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    respx.get("https://x.test/a").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, json={"ok": 1})])
    assert http.get_json("https://x.test/a") == {"ok": 1}


def test_seed_restores_empty_db(tmp_db):
    from app.data.seed import SEED, ensure_db
    if not SEED.exists():
        import pytest; pytest.skip("no seed built")
    assert ensure_db() == "restored from seed"
    with connect() as c:
        assert c.execute("SELECT count(*) FROM weather_meta").fetchone()[0] == 14
    assert ensure_db() == "existing"
