"""Tool tests on a synthetic database (no network, no dependence on the live cache)."""
import pytest

from app import tools
from app.config import BASELINE_YEARS, load_hubs
from app.db import connect
from app.scoring import rank


@pytest.fixture
def seeded(tmp_db, monkeypatch):
    """Each hub gets a different number of snowy/hot days so rankings are predictable."""
    with connect() as c:
        for i, h in enumerate(load_hubs()):
            rows = []
            for y in BASELINE_YEARS:
                for d in range(1, 31):
                    rows.append((h.id, f"{y}-01-{d:02d}", 2.0 if d <= i else 0.0, 0.0, -1.0, 36.0 if d <= 14 - i else 10.0, 20.0))
            c.executemany("INSERT INTO daily_weather VALUES (?,?,?,?,?,?,?)", rows)
            c.execute("INSERT INTO weather_meta VALUES (?,?,?,?,?,?,?)",
                      (h.id, h.lat, h.lon, 100, "2021-01-01", "2025-12-31", "x"))
    tools.invalidate_scores()
    monkeypatch.setattr(tools.nws, "active_alerts", lambda hub: {"alerts": [], "fetched_at": "now", "stale": False})
    yield
    tools.invalidate_scores()


def test_rank_matches_scoring_and_filters_region(seeded):
    out = tools.run_tool("rank_hubs", {"hazard": "winter", "region": "Midwest"})
    ids = [r["hub_id"] for r in out["ranking"]]
    assert set(ids) == {h.id for h in load_hubs() if h.region == "Midwest"}
    expected = [r.hub_id for r in rank([tools.all_scores()[i] for i in ids], "winter")]
    assert ids == expected


def test_hazard_and_region_aliases(seeded):
    assert tools.run_tool("rank_hubs", {"hazard": "hurricane"})["hazard"] == "wind"
    assert tools.run_tool("rank_hubs", {"hazard": "snow", "region": "midwestern", "top_n": 2})["region"] == "Midwest"


def test_invalid_args_are_reported_not_raised(seeded):
    assert tools.run_tool("rank_hubs", {"hazard": "volcano"})["error"] == "invalid_arguments"
    assert tools.run_tool("rank_hubs", {"region": "Europe"})["error"] == "invalid_arguments"
    assert tools.run_tool("compare_hubs", {"hub_ids": ["MIA"]})["error"] == "invalid_arguments"
    assert tools.run_tool("nope", {})["error"] == "unknown_tool"


def test_unknown_hub_lists_valid_hubs(seeded):
    out = tools.run_tool("get_hub_risk_breakdown", {"hub_id": "Seattle"})
    assert out["error"] == "hub_not_found" and len(out["valid_hubs"]) == 14
    cmp = tools.run_tool("compare_hubs", {"hub_ids": ["Miami", "Seattle"]})
    assert [r["hub_id"] for r in cmp["comparison"]] == ["MIA"] and cmp["not_found"]


def test_hub_resolution_by_city_and_id(seeded):
    assert tools.resolve_hub("dallas hub").id == "DFW"
    assert tools.resolve_hub("Kansas City").id == "MCI"
    assert tools.resolve_hub("ewr").id == "EWR"


def test_breakdown_has_numbers_and_quality(seeded):
    b = tools.run_tool("get_hub_risk_breakdown", {"hub_id": "DFW"})
    assert b["composite_rank"].endswith("of 14")
    assert b["hazards"]["winter"]["indicators"][0]["key"] == "snow_days_1cm"
    assert b["data_quality"]["weather_years"] == BASELINE_YEARS


def test_weather_stat_pct_and_year_guard(seeded):
    # MSP is index 1 -> 1 snowy day per year in the seed, 30 days per year in the seed
    out = tools.run_tool("get_weather_stat", {"hub_id": "Minneapolis", "metric": "snowfall_any_days", "year": 2025})
    assert out["stat"]["count"] == 1 and out["stat"]["pct"] == pytest.approx(3.3)
    assert "snow_days_1cm" in out["related"]
    assert tools.run_tool("get_weather_stat", {"hub_id": "DEN", "metric": "heat_days", "year": 1999})["error"] == "year_not_cached"


def test_schemas_are_valid_for_llm():
    for t in tools.TOOLS.values():
        s = t.schema()
        assert s["input_schema"]["type"] == "object" and s["description"]
