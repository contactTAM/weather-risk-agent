"""Rolling analysis window: last 5 full calendar years, rolling on Jan 15 (archive lag)."""
from datetime import date

import pytest

from app.config import analysis_window


@pytest.fixture
def unpinned(monkeypatch):
    monkeypatch.delenv("ANALYSIS_END_YEAR", raising=False)
    monkeypatch.delenv("WINDOW_ROLL_DAY", raising=False)


def test_window_rolls_on_jan_15_not_jan_1(unpinned):
    assert analysis_window(date(2026, 11, 15)).baseline_years == [2021, 2022, 2023, 2024, 2025]
    assert analysis_window(date(2027, 1, 14)).last_full_year == 2025   # 2026 not complete in the archive yet
    w = analysis_window(date(2027, 1, 15))
    assert w.last_full_year == 2026 and w.baseline_years == [2022, 2023, 2024, 2025, 2026]
    assert (w.weather_start, w.weather_end, w.label) == ("2022-01-01", "2026-12-31", "2022-2026")


def test_fema_window_is_20_full_years(unpinned):
    w = analysis_window(date(2027, 6, 1))
    assert (w.fema_start, w.fema_end) == (2007, 2026)


def test_roll_day_configurable(unpinned, monkeypatch):
    monkeypatch.setenv("WINDOW_ROLL_DAY", "1")
    assert analysis_window(date(2027, 1, 1)).last_full_year == 2026


def test_pin_overrides_today(monkeypatch):
    monkeypatch.setenv("ANALYSIS_END_YEAR", "2030")
    assert analysis_window(date(2026, 11, 15)).baseline_years == [2026, 2027, 2028, 2029, 2030]


def test_fema_filter_and_refresh_range_follow_window(monkeypatch):
    import httpx
    import respx
    from app.config import get_hub
    from app.data import fema
    monkeypatch.setenv("ANALYSIS_END_YEAR", "2026")
    with respx.mock:
        route = respx.get(fema.URL).mock(return_value=httpx.Response(200, json={"DisasterDeclarationsSummaries": []}))
        fema.fetch(get_hub("MIA"))
    flt = route.calls[0].request.url.params["$filter"]
    assert "ge '2007-01-01" in flt and "le '2026-12-31" in flt


def test_system_prompt_and_stat_default_follow_window(monkeypatch):
    from app import agent, tools
    monkeypatch.setenv("ANALYSIS_END_YEAR", "2026")
    p = agent.system_prompt()
    assert 'calendar year 2026' in p and "2022-2026" in p and "2007-2026" in p
    assert tools.WeatherStatArgs(hub_id="DEN", metric="heat_days").year == 2026
    monkeypatch.setenv("ANALYSIS_END_YEAR", "2025")
    assert "calendar year 2025" in agent.system_prompt()
