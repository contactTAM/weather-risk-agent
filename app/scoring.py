"""Deterministic Weather Disruption Risk scoring. Pure functions, no I/O, no LLM.

indicator value --(fixed anchors)--> 0-100 --(weights)--> hazard score --(weights)--> composite
Every result carries its full breakdown so explanations come from real numbers.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

from app.config import BASELINE_YEARS, FEMA_END_YEAR, FEMA_START_YEAR, ROOT
from app.metrics import HubInputs, baseline_indicators, yearly_counts

HAZARDS = ("winter", "wind", "flood", "heat")
COMPOSITE = "composite"


class IndicatorContribution(BaseModel):
    key: str
    label: str
    value: float
    unit: str
    anchor_zero: float
    anchor_full: float
    normalized: float      # 0-100
    weight: float          # within hazard
    contribution: float    # normalized * weight (points of the hazard score)


class HazardScore(BaseModel):
    hazard: str
    label: str
    score: float
    level: str
    indicators: list[IndicatorContribution]


class DataQuality(BaseModel):
    weather_years: list[int]
    weather_days: int
    missing_days: int
    grid_distance_km: float | None
    fema_window: str
    notes: list[str]


class HubScore(BaseModel):
    hub_id: str
    composite: float
    level: str
    hazards: dict[str, HazardScore]
    composite_contributions: dict[str, float]  # hazard -> weighted points of composite
    data_quality: DataQuality

    def score_for(self, hazard: str) -> float:
        return self.composite if hazard == COMPOSITE else self.hazards[hazard].score


class RankedHub(BaseModel):
    rank: int
    hub_id: str
    score: float
    level: str
    close_call_with_next: bool = False


@lru_cache
def load_config(path: str | None = None) -> dict:
    return yaml.safe_load(Path(path or ROOT / "config" / "scoring.yaml").read_text())


def normalize(value: float, zero: float, full: float) -> float:
    if full == zero:
        raise ValueError("anchor full must differ from zero")
    return round(max(0.0, min(1.0, (value - zero) / (full - zero))) * 100.0, 1)


def level_for(score: float, cfg: dict) -> str:
    lv = cfg["levels"]
    return "high" if score >= lv["high"] else "moderate" if score >= lv["moderate"] else "low"


def score_indicators(hub_id: str, ind: dict[str, float], cfg: dict, quality: DataQuality) -> HubScore:
    hazards: dict[str, HazardScore] = {}
    for hz, spec in cfg["hazards"].items():
        contribs = []
        for key, s in spec["indicators"].items():
            val = float(ind.get(key, 0.0))
            n = normalize(val, s["zero"], s["full"])
            contribs.append(IndicatorContribution(
                key=key, label=s["label"], value=round(val, 2), unit=s["unit"],
                anchor_zero=s["zero"], anchor_full=s["full"], normalized=n,
                weight=s["weight"], contribution=round(n * s["weight"], 1)))
        score = round(sum(c.normalized * c.weight for c in contribs), 1)
        hazards[hz] = HazardScore(hazard=hz, label=spec["label"], score=score,
                                  level=level_for(score, cfg), indicators=contribs)
    weights = cfg["composite_weights"]
    comp_parts = {hz: round(hazards[hz].score * w, 1) for hz, w in weights.items()}
    composite = round(sum(hazards[hz].score * w for hz, w in weights.items()), 1)
    return HubScore(hub_id=hub_id, composite=composite, level=level_for(composite, cfg),
                    hazards=hazards, composite_contributions=comp_parts, data_quality=quality)


def score_hub(inputs: HubInputs, cfg: dict | None = None, years: list[int] | None = None) -> HubScore:
    cfg = cfg or load_config()
    years = years or BASELINE_YEARS
    ind = baseline_indicators(inputs.daily, inputs.fema, years)
    yc = yearly_counts(inputs.daily)
    covered = [y for y in years if y in yc]
    notes = [
        "Weather is ERA5-family reanalysis via Open-Meteo for the grid cell nearest the city centre, not a station at the hub site; peak gusts are smoothed and likely under-counted.",
        "FEMA declarations are county-level and reflect damage and the declaration process, not only weather severity.",
    ]
    if len(covered) < len(years):
        notes.append(f"Weather data missing for years: {sorted(set(years) - set(covered))}.")
    quality = DataQuality(
        weather_years=covered, weather_days=sum(yc[y]["n_days"] for y in covered),
        missing_days=sum(yc[y]["missing_days"] for y in covered), grid_distance_km=inputs.grid_km,
        fema_window=f"{FEMA_START_YEAR}-{FEMA_END_YEAR}", notes=notes)
    return score_indicators(inputs.hub_id, ind, cfg, quality)


def rank(scores: list[HubScore], hazard: str = COMPOSITE, cfg: dict | None = None) -> list[RankedHub]:
    """Sort descending by the hazard score (ties broken by hub_id for determinism)."""
    if hazard != COMPOSITE and hazard not in HAZARDS:
        raise ValueError(f"unknown hazard {hazard}")
    cfg = cfg or load_config()
    ordered = sorted(scores, key=lambda s: (-s.score_for(hazard), s.hub_id))
    out = []
    for i, s in enumerate(ordered):
        sc = s.score_for(hazard)
        lvl = s.level if hazard == COMPOSITE else s.hazards[hazard].level
        close = i + 1 < len(ordered) and abs(sc - ordered[i + 1].score_for(hazard)) < cfg["close_call_points"]
        out.append(RankedHub(rank=i + 1, hub_id=s.hub_id, score=sc, level=lvl, close_call_with_next=close))
    return out


def top_drivers(score: HubScore, hazard: str = COMPOSITE, n: int = 3) -> list[dict]:
    """Largest contributors, in points of the requested score. Used to answer 'why is X high?'."""
    if hazard == COMPOSITE:
        w = load_config()["composite_weights"]
        items = [{"hazard": hz, "indicator": c.key, "label": c.label, "value": c.value, "unit": c.unit,
                  "points": round(c.contribution * w[hz], 1)}
                 for hz, h in score.hazards.items() for c in h.indicators]
    else:
        items = [{"hazard": hazard, "indicator": c.key, "label": c.label, "value": c.value, "unit": c.unit,
                  "points": c.contribution} for c in score.hazards[hazard].indicators]
    return sorted(items, key=lambda d: -d["points"])[:n]
