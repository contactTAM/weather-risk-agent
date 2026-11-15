import pytest

from app.metrics import HubInputs
from app.scoring import (DataQuality, level_for, load_config, normalize, rank,
                         score_hub, score_indicators, top_drivers)

CFG = load_config()
Q = DataQuality(weather_years=[2025], weather_days=365, missing_days=0, grid_distance_km=1.0,
                fema_window="2006-2025", notes=[])


def test_normalize_clips_to_anchor_range():
    assert normalize(20, 0, 40) == 50.0
    assert normalize(-5, 0, 40) == 0.0
    assert normalize(400, 0, 40) == 100.0
    with pytest.raises(ValueError):
        normalize(1, 5, 5)


def test_levels():
    assert (level_for(10, CFG), level_for(33, CFG), level_for(66, CFG)) == ("low", "moderate", "high")


def test_weights_sum_to_one():
    assert sum(CFG["composite_weights"].values()) == pytest.approx(1.0)
    for spec in CFG["hazards"].values():
        assert sum(i["weight"] for i in spec["indicators"].values()) == pytest.approx(1.0)


def test_hazard_score_is_weighted_sum_and_breakdown_adds_up():
    ind = {"snow_days_1cm": 20, "heavy_snow_days": 2, "freeze_days": 75, "fema_winter": 3}
    s = score_indicators("X", ind, CFG, Q)
    w = s.hazards["winter"]
    assert w.score == pytest.approx(50.0)               # every indicator at half its anchor
    assert sum(c.contribution for c in w.indicators) == pytest.approx(w.score, abs=0.2)
    assert s.composite == pytest.approx(sum(s.composite_contributions.values()), abs=0.2)


def test_zero_exposure_scores_zero_and_max_scores_hundred():
    zero = score_indicators("Z", {}, CFG, Q)
    assert zero.composite == 0 and zero.level == "low"
    big = {k: 1e6 for spec in CFG["hazards"].values() for k in spec["indicators"]}
    assert score_indicators("B", big, CFG, Q).composite == pytest.approx(100.0)


def test_score_independent_of_other_hubs():
    """Fixed anchors: adding a hub never changes another hub's score (why we avoided min-max)."""
    a = score_indicators("A", {"heat_days": 60}, CFG, Q)
    b = score_indicators("B", {"heat_days": 120}, CFG, Q)
    assert a.hazards["heat"].score == 50.0
    assert rank([a, b], "heat")[1].score == 50.0


def test_rank_order_ties_and_close_calls():
    a = score_indicators("AAA", {"heat_days": 60}, CFG, Q)
    b = score_indicators("BBB", {"heat_days": 60}, CFG, Q)
    c = score_indicators("CCC", {"heat_days": 120}, CFG, Q)
    r = rank([b, a, c], "heat")
    assert [x.hub_id for x in r] == ["CCC", "AAA", "BBB"]   # tie broken by id
    assert r[1].close_call_with_next and not r[0].close_call_with_next
    with pytest.raises(ValueError):
        rank([a], "volcano")


def test_top_drivers_explain_the_score():
    s = score_indicators("X", {"heat_days": 120, "rain25_days": 10}, CFG, Q)
    d = top_drivers(s, "composite", n=2)
    assert d[0]["indicator"] == "heat_days" and d[0]["points"] == pytest.approx(15.0)


def test_score_hub_end_to_end_with_quality_notes():
    daily = [{"date": f"2025-01-{i:02d}", "snowfall_cm": 2.0, "precip_mm": 0.0, "tmin_c": -3.0,
              "tmax_c": 1.0, "gust_kmh": 20.0} for i in range(1, 11)]
    s = score_hub(HubInputs("X", daily, [], grid_km=2.5), years=[2024, 2025])
    assert s.hazards["winter"].indicators[0].value == 10.0
    assert s.data_quality.weather_years == [2025]
    assert any("missing for years: [2024]" in n for n in s.data_quality.notes)
