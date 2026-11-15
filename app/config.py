"""Central configuration: paths, env settings, hub list. No secrets or model names hardcoded."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Analysis window (ASSUMPTION, documented): "last year" = last full calendar year.
LAST_FULL_YEAR = 2025
BASELINE_YEARS = list(range(2021, 2026))  # 5-year baseline used for scoring
FEMA_START_YEAR = 2006                     # 20-year FEMA window: 2006-2025
FEMA_END_YEAR = 2025


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
