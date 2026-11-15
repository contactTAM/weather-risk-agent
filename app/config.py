"""Central configuration: paths, env settings, hub list. No secrets or model names hardcoded."""
from __future__ import annotations

import os
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

BASELINE_LEN = 5   # weather baseline: last 5 full calendar years
FEMA_LEN = 20      # FEMA declarations: last 20 full calendar years


class Window(BaseModel):
    """Rolling analysis window (ASSUMPTION, documented): "last year" = last full calendar year."""
    last_full_year: int
    baseline_years: list[int]
    fema_start: int
    fema_end: int

    @property
    def weather_start(self) -> str:
        return f"{self.baseline_years[0]}-01-01"

    @property
    def weather_end(self) -> str:
        return f"{self.baseline_years[-1]}-12-31"

    @property
    def label(self) -> str:
        return f"{self.baseline_years[0]}-{self.baseline_years[-1]}"


def analysis_window(today: date | None = None) -> Window:
    """Last BASELINE_LEN full calendar years, rolling forward each January.

    The window rolls on WINDOW_ROLL_DAY (default Jan 15), not Jan 1: Open-Meteo's archive lags a few
    days, so on Jan 2 the previous year is not complete yet. ANALYSIS_END_YEAR pins the window
    (tests and reproducible runs).
    """
    pinned = os.getenv("ANALYSIS_END_YEAR", "").strip()
    if pinned:
        end = int(pinned)
    else:
        today = today or date.today()
        roll_day = int(os.getenv("WINDOW_ROLL_DAY", "15"))
        rolled = today.month > 1 or today.day >= roll_day
        end = today.year - 1 if rolled else today.year - 2
    return Window(last_full_year=end, baseline_years=list(range(end - BASELINE_LEN + 1, end + 1)),
                  fema_start=end - FEMA_LEN + 1, fema_end=end)


class Hub(BaseModel):
    id: str
    name: str
    city: str
    state: str
    region: str
    lat: float
    lon: float
    county_fips: str
    county: str
    timezone: str


def db_path() -> Path:
    p = Path(os.getenv("DB_PATH", "data/cache.sqlite"))
    return p if p.is_absolute() else ROOT / p


def nws_user_agent() -> str:
    return os.getenv("NWS_USER_AGENT") or "weather-risk-agent (demo)"


def llm_model() -> str:
    """Model id must come from the environment. No silent default."""
    model = os.getenv("LLM_MODEL", "").strip()
    if not model:
        raise RuntimeError("LLM_MODEL is not set. Add it to .env (see .env.example).")
    return model


@lru_cache
def load_hubs() -> list[Hub]:
    raw = yaml.safe_load((ROOT / "data" / "hubs.yaml").read_text())
    return [Hub(**h) for h in raw["hubs"]]


def get_hub(hub_id: str) -> Hub | None:
    key = hub_id.strip().upper()
    for h in load_hubs():
        if h.id == key or h.city.upper() == key:
            return h
    return None
