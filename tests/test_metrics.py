from app.metrics import baseline_indicators, weather_stat, yearly_counts


def day(date, snow=0.0, rain=0.0, tmin=5.0, tmax=20.0, gust=10.0):
    return {"date": date, "snowfall_cm": snow, "precip_mm": rain, "tmin_c": tmin, "tmax_c": tmax, "gust_kmh": gust}


DAILY = [
    day("2024-01-01", snow=0.05),            # trace: below 0.1 cm, not a snowfall day
    day("2024-01-02", snow=0.5, tmin=-2),    # measurable snow, not >= 1 cm; freezing
    day("2024-01-03", snow=12, tmin=-8),     # heavy snow
    day("2024-07-01", tmax=36, rain=30),     # heat + heavy rain
    day("2025-01-01", snow=2, gust=95),      # snow >= 1cm, severe gust
    day("2025-07-01", rain=55, gust=61, tmax=None),  # extreme rain, strong gust, missing tmax
]


def test_yearly_counts_apply_thresholds():
    y = yearly_counts(DAILY)
    assert y[2024]["snowfall_any_days"] == 2      # 0.5 and 12; trace 0.05 excluded
    assert y[2024]["snow_days_1cm"] == 1 and y[2024]["heavy_snow_days"] == 1
    assert y[2024]["freeze_days"] == 2 and y[2024]["heat_days"] == 1 and y[2024]["rain25_days"] == 1
    assert y[2025]["gust90_days"] == 1 and y[2025]["gust60_days"] == 2 and y[2025]["rain50_days"] == 1
    assert y[2025]["missing_days"] == 1


def test_baseline_averages_years_and_counts_fema():
    fema = [{"incident_type": "Hurricane"}, {"incident_type": "Flood"},
            {"incident_type": "Severe Storm"}, {"incident_type": "Biological"}]
    ind = baseline_indicators(DAILY, fema, [2024, 2025])
    assert ind["snow_days_1cm"] == 1.0          # (1 + 1) / 2
    assert ind["rain25_days"] == 1.0            # (1 + 1) / 2
    assert ind["fema_wind"] == 1 and ind["fema_flood"] == 2 and ind["fema_winter"] == 0


def test_weather_stat_percentage_and_missing_year():
    s = weather_stat(DAILY, "snowfall_any_days", 2024)
    assert (s["count"], s["days"], s["pct"]) == (2, 4, 50.0)
    assert weather_stat(DAILY, "snowfall_any_days", 2019)["available"] is False
