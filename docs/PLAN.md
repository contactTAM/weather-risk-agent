# PLAN: Weather Risk Intelligence Agent (Moveo M.ai home assignment)

> Written in plan mode and approved by Guy on 2026-11-15, then committed as the first file in the repo. Changes made after approval are marked inline.

## Context
A 24-hour home assignment for a Junior Full-Stack AI Engineer role. A logistics company with US distribution hubs wants an agent that ranks and compares hubs by weather-disruption exposure, explains why, and handles follow-ups. The graders look at reasoning and design more than polish, and this session is part of the submission. So the plan is narrow: deterministic scoring at the core, the LLM only routes and explains, and every number traces back to public data.

**Repo:** `~/code/weather-risk-agent` (already exists: empty git repo, branch `master`, rename it to `main`). Pushed to a new GitHub repo only when Guy says so.

## Scope
**In:** 14 hubs; 4 hazards (winter, wind/hurricane, flood/heavy rain, heat) plus a composite score; 3 public data sources cached in SQLite; tool-calling agent with schema-enforced output; FastAPI; chat UI; evals; tests; docs; alert job (bonus); browser voice (bonus).
**Out:** forecasts/climate projections, hub-level infrastructure data, cost/ROI modelling, auth, multi-user scale, any hub not in the config.

## Assumptions (shown in the docs and returned by the agent when relevant)
- The hub list is fictional: real US cities, with the city-centre lat/lon standing in for the hub site.
- "Last year" = the last full calendar year, **2025** (today is 2026-11-15). Scores use a **5-year baseline, 2021–2025** (yearly averages), so one odd year doesn't dominate.
- Open-Meteo values are reanalysis grid cells (about 9–25 km), not station readings. Peak gusts in particular are smoothed and under-reported, so the absolute counts are treated as indicative.
- FEMA declarations are county-level and reflect **impact and political process**, not just weather. They're used as a long-term (2006–2025, 20 years) hazard signal. Each disaster number is counted once per county.
- Biological, fire and other non-weather FEMA incident types are excluded.

## Verified API facts (probed 2026-11-15, before planning)
- **Open-Meteo archive** `archive-api.open-meteo.com/v1/archive?latitude&longitude&start_date&end_date&daily=snowfall_sum,precipitation_sum,temperature_2m_min,temperature_2m_max,wind_gusts_10m_max&timezone=` returns `daily.{time,...}` arrays. Units: snowfall cm, precipitation mm, temperature °C, gusts km/h.
- **OpenFEMA v2** `DisasterDeclarationsSummaries?$filter=fipsStateCode eq '12' and fipsCountyCode eq '086' and declarationDate ge '...'&$select=disasterNumber,incidentType,declarationType,declarationDate&$top=1000` works. Miami-Dade 2006+ gives Hurricane 10, Tropical Storm 1, Severe Storm 2, Biological 2, Other 1. That confirms the plan to dedupe by `disasterNumber` and to filter incident types.
- **NWS** `api.weather.gov/alerts/active?point=lat,lon` with a `User-Agent` header returns a GeoJSON FeatureCollection (`features[].properties.{event,severity,headline,...}`).

## Architecture
```
Browser chat UI (static HTML/JS, Web Speech API)
        │ POST /chat, GET /hubs, /scores ...
FastAPI (app/api.py) ── APScheduler alert job → webhook + alerts table
        │
Agent (app/agent.py): LLM tool loop → submit_answer (JSON schema) → Pydantic validate → grounding check
        │ tools (app/tools.py, Pydantic-validated args)
Scoring (app/scoring.py, pure functions)  ← metrics (app/metrics.py)
        │
Data layer (app/data/*.py): fetchers → SQLite cache (raw_responses, daily_weather, fema_declarations, scores_snapshot, sessions, messages, alerts)
```
**Why SQLite:** one file, no server, ships with Python, and it lets the live demo run offline and fast. Raw responses are kept for reproducibility. A one-function swap would move it to Postgres at scale.

### Key design decisions (changes from the proposed design, with reasons)
1. **Normalization: fixed anchors, not min-max or percentile.** Each indicator maps linearly from a documented "no exposure" value to a "high exposure" value and is clipped to 0–100 (for example, snow days ≥1 cm per year: 0 → 0 and 40 → 100).
   *Why:* min-max across 14 hubs makes a hub's score depend on the other hubs. Adding or removing a hub, or one outlier, would shift everyone, and then the alert "score changed" means nothing. Percentile with n=14 is coarse and hides magnitude. Fixed anchors keep scores stable, explainable ("40 snow days/yr = max") and comparable over time. **Ranks** are still relative, computed across hubs. Anchors are set from physical reasoning before looking at the results. I check the distribution once after the first fetch and record any change in the docs.
2. **Structured output through a forced final tool.** The final answer is a `submit_answer` tool whose `input_schema` is the Pydantic `AgentAnswer.model_json_schema()`. The LLM must call it, and the code validates it with Pydantic. If validation fails, the error goes back to the LLM once; after that, a safe error response is returned.
3. **The LLM never supplies scores in the table.** `AgentAnswer.hubs` holds only `hub_id`s. The code fills `score`/`rank`/`level` from `scoring.py`. A grounding check flags hub_ids that weren't returned by any tool. That keeps hallucinated numbers out of the structured part.
4. **Python 3.12 via `uv`.** System Python is 3.14, and some wheels lag behind it, so 3.12 is safer.
5. **Anthropic only (Guy's decision, 2026-11-15).** The model name comes from `LLM_MODEL` in `.env`, with no default in the code. If it isn't set, the app refuses to start with a clear error. In milestone 5, a one-line test call against the API confirms the model name. The OpenAI adapter is dropped: one provider done well beats two done thinly.

### Changes after approval
- **Anthropic only** (see decision 5).
- **FEMA county rows only.** Statewide rows (`fipsCountyCode '000'`) are excluded: many are tribal-nation declarations unrelated to the hub city (Arizona: 20 statewide "Severe Storm" rows). Added `Straight-Line Winds` → wind and `Freezing` → winter after seeing them in the data.
- **Anchor calibration** after the first pull: heavy-snow days 8 → 4 and severe-gust (≥ 90 km/h) days 5 → 2. Reanalysis smooths peaks: the maximum across hubs was 1.2 and 1.0 days a year, so both indicators were stuck at about 3 points. No other anchors were changed.
- **Structured output mechanism (milestone 5).** `claude-sonnet-5-5` rejects forced `tool_choice` with a 400, so the forced `submit_answer` tool was dropped. Instead the tool loop runs with `tool_choice: auto` and `output_config.format` = the `LLMAnswer` JSON schema in the same request. The API guarantees the final text is schema-valid JSON, and Pydantic re-validates it (known hub IDs, assumptions present). On failure there is one retry with the error, then a safe error. A manual loop is used, not the SDK's beta tool runner, for per-step logging, the step cap and the grounding checks. `parse()` was not used either: it raises inside response parsing, which would lose the raw reply needed for the retry. The schema comes from Pydantic through a small in-repo helper rather than private SDK internals.
- **Refusal fallback** (`fallbacks: "default"`, beta) is on, the recommended default for this model. `LLM_FALLBACKS=off` disables it; `meta.model` shows which model answered.
- **Grounding checks.** Every number in the answer text must appear in a tool result or the system prompt (within rounding), and every hub ID must have come back from a tool. Violations are reported in `meta.grounding_warnings` and used by the evals.
- **"Had snowfall"** = any measurable snowfall (≥ 0.1 cm), for the % question. The winter score uses ≥ 1 cm days. The stat tool returns both.

## Hubs (`data/hubs.yaml`)
Midwest: Chicago (Cook 17031), Minneapolis (Hennepin 27053), Detroit (Wayne 26163), Indianapolis (Marion 18097), Kansas City (Jackson MO 29095), Columbus (Franklin OH 39049).
Others: Miami (Miami-Dade 12086, South), Houston (Harris 48201, South), Dallas (Dallas 48113, South), Atlanta (Fulton 13121, South), Memphis (Shelby 47157, South), Denver (Denver 08031, West), Phoenix (Maricopa 04013, West), Newark (Essex 34013, Northeast). Regions follow the US Census regions.

## Scoring (`config/scoring.yaml`, `app/scoring.py`)
Yearly-average indicators over 2021–2025 (FEMA: count over 20 years). Each indicator is anchor-scaled to 0–100, the hazard score is the weighted sum, and the composite is the weighted sum of hazard scores.

| Hazard (composite weight) | Indicator | Anchor 0 → 100 | Weight |
|---|---|---|---|
| **Winter (0.30)** | days snowfall ≥ 1 cm | 0 → 40 | 0.35 |
| | days snowfall ≥ 10 cm (heavy) | 0 → 4 (was 8) | 0.20 |
| | days Tmin ≤ 0 °C | 0 → 150 | 0.25 |
| | FEMA Winter Storm/Snowstorm/Severe Ice Storm | 0 → 6 | 0.20 |
| **Wind/Hurricane (0.25)** | days gust ≥ 60 km/h | 0 → 40 | 0.30 |
| | days gust ≥ 90 km/h | 0 → 2 (was 5) | 0.30 |
| | FEMA Hurricane/Tropical Storm/Tornado | 0 → 10 | 0.40 |
| **Flood/Heavy rain (0.30)** | days precip ≥ 25 mm | 0 → 20 | 0.35 |
| | days precip ≥ 50 mm | 0 → 5 | 0.25 |
| | FEMA Flood/Severe Storm/Coastal Storm | 0 → 15 | 0.40 |
| **Heat (0.15)** | days Tmax ≥ 35 °C | 0 → 120 | 1.00 |

Levels: low < 33 ≤ moderate < 66 ≤ high. Every score returns a `ScoreBreakdown` with each indicator's raw value, normalized value, weight and contribution, plus `data_quality`: years covered, missing days, grid distance from the hub, and FEMA caveats. The weights are judgment calls and are documented as such. The composite weights can be overridden per query (for example, an analyst who only cares about winter uses `rank_hubs(hazard="winter")`).

## Agent tools (`app/tools.py`, Pydantic arg models)
`list_hubs(region?)`, `rank_hubs(hazard, region?, top_n?)`, `compare_hubs(hub_ids, hazards?)`, `get_hub_risk_breakdown(hub_id)`, `get_weather_stat(hub_id, metric, year)` (metric ∈ snow_day_pct, freeze_day_pct, heavy_rain_day_pct, heat_day_pct, plus count variants), `get_active_alerts(hub_id)`. Unknown hub → a structured `not_found` result listing the valid hubs.

**Output schema `AgentAnswer`:** `answer`, `hubs[{hub_id, score?, rank?, level?}]` (scores filled by code), `reasoning_steps[]`, `data_used[{source, period}]`, `assumptions[]`, `uncertainty{level: low|medium|high, note}`, `follow_up_suggestions[]`, `in_scope: bool`.

**Memory:** `sessions` and `messages` tables keyed by `session_id`. The last N turns, including tool results in compact form, are replayed so follow-ups like "and Chicago?" work.

## API (`app/api.py`)
`POST /chat {session_id, message}` → `AgentAnswer` + `tools_called`; `GET /hubs`; `GET /scores?hazard=&region=`; `GET /hubs/{id}/breakdown`; `POST /refresh`; `POST /alerts/check`; `GET /health`. FastAPI serves the UI at `/`.

## Repo structure
```
app/ api.py agent.py llm.py tools.py schemas.py scoring.py metrics.py db.py alerts.py config.py
app/data/ open_meteo.py fema.py nws.py refresh.py
config/scoring.yaml   data/hubs.yaml   static/index.html app.js style.css
evals/ cases.yaml run.py results/   tests/ test_scoring.py test_metrics.py test_fetchers.py test_tools.py test_agent_schema.py test_api.py
docs/ PLAN.md ARCHITECTURE.md SUBMISSION.md   README.md .env.example .gitignore pyproject.toml (Dockerfile optional)
```

## Evals (`evals/cases.yaml`, `python -m evals.run`)
About 12 cases: the 4 example questions; Midwest winter top-3; Miami vs Houston; Denver snow % (expected value **computed from metrics.py**, with a ±1 pt tolerance on the % in the answer text); why Dallas is high (the top contributors must be named); a follow-up pair ("rank Midwest by flood" → "and Chicago?"); an unknown hub (Seattle → honest not-found); out of scope (stock prices → `in_scope=false`); a hazard-only ranking; a live-alerts question. The deterministic checks: schema valid, expected tools called, expected hub order (derived from `scoring.py` at run time, never hand-coded), numeric tolerance, non-empty assumptions. The script prints a pass/fail table and writes `evals/results/<timestamp>.json`.

## Alerts (bonus)
APScheduler runs every 6 h, plus `POST /alerts/check`. Each run recomputes scores, pulls NWS active alerts, and diffs against the last `scores_snapshot`. When a score changes by ≥ 5 pts, a level changes, or a new Severe/Extreme NWS alert appears, it POSTs JSON to `ALERT_WEBHOOK_URL` (if set) and logs to the `alerts` table. Demo: point the webhook at a local `/alerts/echo` endpoint, or force a change with a config override.

## Milestones (a commit after each; each one is run and shown working)
1. **Scaffold:** `uv` project, `.gitignore`, `.env.example`, PLAN.md, hubs.yaml.
2. **Data layer and cache:** fetchers, SQLite schema, `python -m app.data.refresh`; shows real row counts per hub. Tests use mocked HTTP.
3. **Scoring and tests:** metrics + scoring, unit tests on synthetic data, then the actual ranking table printed.
4. **Tools:** typed tools plus tests.
5. **Agent and structured output:** needs the API key, which I'll ask for here. Demo of the 4 example questions from the CLI.
6. **API:** FastAPI endpoints plus `TestClient` tests.
7. **Chat UI:** bubbles, a hub/score table, and a collapsible "reasoning, data and assumptions" section; voice mic and TTS if quick.
8. **Evals.**
9. **Alerts (bonus).**
10. **Docs:** README, ARCHITECTURE (mermaid), SUBMISSION (1 page).
11. **Optional:** Dockerfile and a Render deploy. Push to GitHub on Guy's say-so.

**Cut order if time is tight:** voice → alerts → deploy. Evals and tests are never cut.

## Verification
- `uv run pytest`: scoring, metrics, fetchers (mocked), tools, schema validation (including the retry path), API.
- `uv run python -m app.data.refresh`, then sanity checks: Miami/Houston lead wind and flood, Minneapolis/Chicago/Denver lead winter, Phoenix leads heat.
- `uv run python -m evals.run`: all cases pass, with the table shown in the session.
- `uv run uvicorn app.api:app`: walk through the 4 example questions plus a follow-up in the browser.
- `git log` shows one commit per milestone; `git grep` finds no secrets.
