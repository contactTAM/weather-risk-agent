"""Ship a small data-only snapshot so a fresh clone / cloud deploy works without a slow first refresh.

    python -m app.data.seed build     # cache.sqlite -> data/seed.sqlite.gz (weather + FEMA rows only)
On startup, ensure_db() restores the seed if the local DB has no weather data.
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from app.config import ROOT, db_path
from app.db import connect

SEED = ROOT / "data" / "seed.sqlite.gz"
KEEP = ("daily_weather", "weather_meta", "fema_declarations")


def build() -> Path:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "seed.sqlite"
        shutil.copy(db_path(), tmp)
        c = sqlite3.connect(tmp)
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            if t not in KEEP:
                c.execute(f"DELETE FROM {t}")
        c.commit(); c.execute("VACUUM"); c.close()
        with open(tmp, "rb") as src, gzip.open(SEED, "wb") as dst:
            shutil.copyfileobj(src, dst)
    return SEED


def ensure_db() -> str:
    with connect() as c:
        if c.execute("SELECT count(*) FROM weather_meta").fetchone()[0] > 0:
            return "existing"
    if not SEED.exists():
        return "empty (no seed; run python -m app.data.refresh)"
    with gzip.open(SEED, "rb") as src, open(db_path(), "wb") as dst:
        shutil.copyfileobj(src, dst)
    return "restored from seed"


if __name__ == "__main__":
    if sys.argv[1:] == ["build"]:
        print("wrote", build())
    else:
        print(ensure_db())
