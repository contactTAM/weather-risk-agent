"""Shared HTTP helper: one GET with timeout and small retry. Real field names verified 2026-11-15."""
from __future__ import annotations

import time

import httpx


def get_json(url: str, params: dict | None = None, headers: dict | None = None,
             retries: int = 2, timeout: float = 30.0) -> dict:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 429 and attempt < retries:
                # Rate limited (Open-Meteo free tier is per-minute weighted): wait out the window.
                time.sleep(float(r.headers.get("Retry-After", 65)))
                continue
            if r.status_code in (500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")
