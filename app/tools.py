"""Deterministic tools the agent can call. The LLM picks tools; every number comes from here.

Each tool = (Pydantic args model, function, description). Args are validated before execution;
the JSON schema sent to the LLM is generated from the same models.
"""
from __future__ import annotations

from typing import Callable, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config import Hub, analysis_window, load_hubs
from app.data import nws
from app.db import connect
from app.metrics import DAY_FLAGS, load_inputs, weather_stat
from app.scoring import COMPOSITE, HAZARDS, HubScore, load_config, rank, score_hub, top_drivers

HazardName = Literal["composite", "winter", "wind", "flood", "heat"]
REGIONS = ("Midwest", "South", "West", "Northeast")
HAZARD_ALIASES = {
    "overall": "composite", "total": "composite", "risk": "composite", "weather": "composite",
    "hurricane": "wind", "hurricanes": "wind", "tornado": "wind", "storm": "wind",
    "snow": "winter", "ice": "winter", "cold": "winter", "freeze": "winter",
    "rain": "flood", "flooding": "flood", "precipitation": "flood",
    "hot": "heat", "extreme heat": "heat",
}
SCALE = "0-100 (fixed anchors; low <33 <= moderate <66 <= high)"
def sources() -> dict:
    """Data sources with periods from the current rolling window."""
    win = analysis_window()
    return {
        "open_meteo": {"source": "Open-Meteo Historical Weather API (reanalysis)",
                       "period": f"{win.label} daily, yearly averages"},
        "fema": {"source": "OpenFEMA Disaster Declarations Summaries v2 (county)",
                 "period": f"{win.fema_start}-{win.fema_end}"},
        "nws": {"source": "NWS api.weather.gov active alerts", "period": "live"},
        "hubs": {"source": "Hub network configuration (data/hubs.yaml)", "period": "current"},
    }


def _norm_hazard(v):
    if isinstance(v, str):
        v = v.strip().lower()
        return HAZARD_ALIASES.get(v, v)
    return v


def _norm_region(v):
    if v is None:
        return v
    for r in REGIONS:
        if v.strip().lower() in (r.lower(), r.lower() + "ern"):
            return r
    raise ValueError(f"unknown region '{v}'; valid: {list(REGIONS)}")


def resolve_hub(ref: str) -> Hub | None:
    key = ref.strip().lower().removesuffix(" hub").strip()
    for h in load_hubs():
        if key in (h.id.lower(), h.city.lower(), h.name.lower()):
            return h
    return None


def _not_found(ref: str) -> dict:
    return {"error": "hub_not_found", "requested": ref,
            "message": f"No hub matching '{ref}' in the network.",
            "valid_hubs": [f"{h.id} ({h.city}, {h.state})" for h in load_hubs()]}


# ---------- score cache (recomputed after refresh) ----------
_SCORES: dict[str, HubScore] | None = None


def all_scores(force: bool = False) -> dict[str, HubScore]:
    global _SCORES
    if _SCORES is None or force:
        with connect() as conn:
            _SCORES = {h.id: score_hub(load_inputs(conn, h)) for h in load_hubs()}
    return _SCORES


def invalidate_scores() -> None:
    global _SCORES
    _SCORES = None


def _hub_brief(h: Hub) -> dict:
    return {"hub_id": h.id, "name": h.name, "city": h.city, "state": h.state, "region": h.region}


# Every daily threshold, returned with weather stats so the agent can cite thresholds from tool data
# (the stricter grounding check flagged "90" km/h when only snow definitions were returned).
METRIC_DEFINITIONS = {
    "snowfall_any_days": "snowfall >= 0.1 cm (any measurable snowfall)",
    "snow_days_1cm": "snowfall >= 1 cm",
    "heavy_snow_days": "snowfall >= 10 cm",
    "freeze_days": "minimum temperature <= 0 C",
    "gust60_days": "max wind gust >= 60 km/h",
    "gust90_days": "max wind gust >= 90 km/h",
    "rain25_days": "precipitation >= 25 mm",
    "rain50_days": "precipitation >= 50 mm",
    "heat_days": "maximum temperature >= 35 C",
}


# ---------- argument models ----------
class ListHubsArgs(BaseModel):
    region: str | None = Field(None, description="Census region: Midwest, South, West, Northeast")
    _r = field_validator("region", mode="before")(classmethod(lambda cls, v: _norm_region(v)))


class RankHubsArgs(BaseModel):
    hazard: HazardName = Field("composite", description="composite | winter | wind (hurricane/wind) | flood (flood/heavy rain) | heat")
    region: str | None = Field(None, description="Optional census region filter")
    top_n: int | None = Field(None, ge=1, le=14)
    _h = field_validator("hazard", mode="before")(classmethod(lambda cls, v: _norm_hazard(v)))
    _r = field_validator("region", mode="before")(classmethod(lambda cls, v: _norm_region(v)))


class CompareHubsArgs(BaseModel):
    hub_ids: list[str] = Field(..., min_length=2, max_length=6, description="Hub ids or city names")
    hazards: list[HazardName] | None = Field(None, description="Default: all hazards + composite")

    @field_validator("hazards", mode="before")
    @classmethod
    def _hz(cls, v):
        return [_norm_hazard(x) for x in v] if v else v


class HubArgs(BaseModel):
    hub_id: str = Field(..., description="Hub id (e.g. DFW) or city name (e.g. Dallas)")


class WeatherStatArgs(BaseModel):
    hub_id: str
    metric: Literal[tuple(DAY_FLAGS)] = Field(  # type: ignore[valid-type]
        ..., description="snowfall_any_days (>=0.1 cm), snow_days_1cm, heavy_snow_days (>=10 cm), "
                         "freeze_days (min<=0C), gust60_days, gust90_days, rain25_days, rain50_days, heat_days (max>=35C)")
    # Static description keeps the tool schema stable (prompt cache); the window is in the system prompt.
    year: int = Field(default_factory=lambda: analysis_window().last_full_year,
                      description="Calendar year inside the analysis window. 'Last year' = the last full calendar year.")


# ---------- tool functions ----------
def list_hubs(args: ListHubsArgs) -> dict:
    hubs = [_hub_brief(h) for h in load_hubs() if not args.region or h.region == args.region]
    return {"hubs": hubs, "count": len(hubs), "assumption": "Fictional hub network; city-centre coordinates stand in for hub sites.",
            "data_used": [sources()["hubs"]]}


def rank_hubs(args: RankHubsArgs) -> dict:
    scores = all_scores()
    pool = [s for hid, s in scores.items()
            if not args.region or next(h for h in load_hubs() if h.id == hid).region == args.region]
    ranked = rank(pool, args.hazard)
    if args.top_n:
        ranked = ranked[: args.top_n]
    cfg = load_config()
    rows = []
    for r in ranked:
        s = scores[r.hub_id]
        rows.append(r.model_dump() | {"city": resolve_hub(r.hub_id).city,
                                      "top_drivers": top_drivers(s, args.hazard, n=2)})
    return {"hazard": args.hazard, "region": args.region or "all", "ranking": rows,
            "scale": SCALE,
            "weights": cfg["composite_weights"] if args.hazard == COMPOSITE else
                       {k: v["weight"] for k, v in cfg["hazards"][args.hazard]["indicators"].items()},
            "close_call_note": "close_call_with_next=true means the next hub is within "
                               f"{cfg['close_call_points']} points: treat their order as uncertain.",
            "data_used": [sources()["open_meteo"], sources()["fema"]]}


def compare_hubs(args: CompareHubsArgs) -> dict:
    hazards = args.hazards or [COMPOSITE, *HAZARDS]
    scores = all_scores()
    out, missing = [], []
    for ref in args.hub_ids:
        h = resolve_hub(ref)
        if not h:
            missing.append(_not_found(ref))
            continue
        s = scores[h.id]
        row = {"hub_id": h.id, "city": h.city}
        for hz in hazards:
            if hz == COMPOSITE:
                row[hz] = {"score": s.composite, "level": s.level}
            else:
                hs = s.hazards[hz]
                row[hz] = {"score": hs.score, "level": hs.level,
                           "indicators": {c.key: {"label": c.label, "value": c.value, "unit": c.unit, "points": c.contribution}
                                          for c in hs.indicators}}
        out.append(row)
    return {"comparison": out, "not_found": missing, "hazards": hazards, "scale": SCALE,
            "data_used": [sources()["open_meteo"], sources()["fema"]]}


def get_hub_risk_breakdown(args: HubArgs) -> dict:
    h = resolve_hub(args.hub_id)
    if not h:
        return _not_found(args.hub_id)
    scores = all_scores()
    s = scores[h.id]
    r = next(x for x in rank(list(scores.values())) if x.hub_id == h.id)
    return {"hub": _hub_brief(h), "composite": s.composite, "level": s.level, "scale": SCALE,
            "composite_rank": f"{r.rank} of {len(scores)}",
            "composite_contributions_points": s.composite_contributions,
            "composite_weights": load_config()["composite_weights"],
            "top_drivers": top_drivers(s, COMPOSITE, n=4),
            "hazards": {hz: hs.model_dump() for hz, hs in s.hazards.items()},
            "data_quality": s.data_quality.model_dump(),
            "data_used": [sources()["open_meteo"], sources()["fema"]]}


def get_weather_stat(args: WeatherStatArgs) -> dict:
    h = resolve_hub(args.hub_id)
    if not h:
        return _not_found(args.hub_id)
    win = analysis_window()
    if args.year not in win.baseline_years:
        return {"error": "year_not_cached", "message": f"Only {win.label} are cached (rolling {len(win.baseline_years)}-year window)."}
    with connect() as conn:
        daily = load_inputs(conn, h).daily
    stat = weather_stat(daily, args.metric, args.year)
    related = {}
    if args.metric in ("snowfall_any_days", "snow_days_1cm"):  # give both snow definitions
        other = "snow_days_1cm" if args.metric == "snowfall_any_days" else "snowfall_any_days"
        related[other] = weather_stat(daily, other, args.year)
    return {"hub": _hub_brief(h), "stat": stat, "related": related,
            "definitions": METRIC_DEFINITIONS,
            "data_used": [{"source": sources()["open_meteo"]["source"], "period": f"{args.year} (calendar year, local time)"}],
            "note": "Grid-cell reanalysis value near the city centre, not a station record."}


def get_active_alerts(args: HubArgs) -> dict:
    h = resolve_hub(args.hub_id)
    if not h:
        return _not_found(args.hub_id)
    res = nws.active_alerts(h)
    return {"hub": _hub_brief(h), **res, "data_used": [sources()["nws"]],
            "note": "Live alerts are current conditions; they do not change the historical risk score."}


class Tool(BaseModel):
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[..., dict]

    def schema(self) -> dict:
        s = self.args_model.model_json_schema()
        s.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": s}


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool(name="list_hubs", args_model=ListHubsArgs, fn=list_hubs,
         description="List hubs in the network, optionally filtered by census region."),
    Tool(name="rank_hubs", args_model=RankHubsArgs, fn=rank_hubs,
         description="Rank hubs by a hazard score (or the composite Weather Disruption Risk Score), optionally within a region. Returns scores, levels, top drivers and close-call flags."),
    Tool(name="compare_hubs", args_model=CompareHubsArgs, fn=compare_hubs,
         description="Side-by-side hazard scores and indicator values for 2-6 hubs."),
    Tool(name="get_hub_risk_breakdown", args_model=HubArgs, fn=get_hub_risk_breakdown,
         description="Full explanation of one hub's score: every indicator value, normalized value, weight, contribution, top drivers, rank, data quality."),
    Tool(name="get_weather_stat", args_model=WeatherStatArgs, fn=get_weather_stat,
         description="Count and percentage of days in a calendar year meeting a weather threshold at a hub (e.g. % of days with snowfall)."),
    Tool(name="get_active_alerts", args_model=HubArgs, fn=get_active_alerts,
         description="Current NWS watches/warnings/advisories at a hub (live)."),
]}


def run_tool(name: str, raw_args: dict) -> dict:
    """Validate args with Pydantic, then execute. Errors come back as data the LLM can react to."""
    tool = TOOLS.get(name)
    if not tool:
        return {"error": "unknown_tool", "valid_tools": list(TOOLS)}
    try:
        args = tool.args_model.model_validate(raw_args or {})
    except ValidationError as e:
        return {"error": "invalid_arguments", "details": e.errors(include_url=False, include_context=False)}
    except ValueError as e:
        return {"error": "invalid_arguments", "details": str(e)}
    return tool.fn(args)
