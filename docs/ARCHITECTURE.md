# Architecture and design

## 1. Goal and scope

Analysts at a logistics company with 14 US distribution hubs ask an agent which hubs are most exposed to weather disruption, and why. The agent must rank and compare hubs by a defined KPI, answer statistical questions, explain its reasoning, and handle follow-ups.

The core principle is **numbers come from code, words come from the model.** The LLM never computes or recalls a weather fact. It chooses deterministic tools and explains what they return, and the structure of its answer is enforced and checked.

**In scope:** four hazards (winter, hurricane/wind, flood/heavy rain, extreme heat) and a composite **Weather Disruption Risk Score**; historical exposure over a rolling window (the last 5 full calendar years of weather, currently 2021–2025, and the last 20 years of FEMA declarations); live NWS alerts as "right now" context.

**Out of scope:** forecasts and climate projections, hub-site infrastructure, cost/ROI of resilience investments, authentication, and hubs outside the configured network.

## 2. System architecture

```mermaid
flowchart LR
  subgraph Browser
    UI["Chat UI (static HTML/JS)<br/>+ Web Speech voice"]
  end
  subgraph Server["FastAPI (app/api.py)"]
    API["/chat /scores /hubs /refresh /alerts /health"]
    AG["Agent loop (agent.py)<br/>tool use + JSON-schema output<br/>Pydantic validation + 1 retry<br/>grounding checks"]
    T["Tools (tools.py)<br/>Pydantic-validated args"]
    S["Scoring (scoring.py)<br/>pure, unit-tested"]
    M["Metrics (metrics.py)"]
    AL["Alerts (alerts.py)<br/>refresh, then compare<br/>webhook / Slack"]
  end
  DB[("SQLite data cache (ephemeral ok)<br/>raw responses, daily weather,<br/>FEMA, refresh log")]
  PG[("State store (state.py)<br/>Postgres via DATABASE_URL (Neon),<br/>SQLite fallback: snapshots,<br/>alert log, chat memory")]
  GH["GitHub Actions cron (every 6 h)"]
  LLM["Claude (Anthropic API)"]
  OM["Open-Meteo archive"]
  FE["OpenFEMA declarations"]
  NWS["NWS active alerts"]
  WH["Webhook (ALERT_WEBHOOK_URL)"]

  UI -->|HTTP| API --> AG
  AG <-->|messages, tool calls| LLM
  AG --> T --> S --> M --> DB
  T -->|live, cached 10 min| NWS
  API -->|refresh| OM & FE
  OM & FE --> DB
  AL --> S
  AL --> NWS
  AL --> WH
  AG -->|session memory| PG
  AL -->|snapshots, alert log| PG
  GH -->|POST /refresh, then /alerts/check| API
```

### Request flow for `POST /chat`

```mermaid
sequenceDiagram
  participant U as UI
  participant A as Agent
  participant C as Claude
  participant T as Tools/Scoring
  U->>A: {session_id, message}
  A->>A: load last 6 turns (text only) from the state store
  loop up to 8 steps
    A->>C: system + tools + messages, output_config.format = LLMAnswer JSON schema
    alt tool_use
      C-->>A: tool_use blocks
      A->>T: validate args (Pydantic), run deterministic tool
      T-->>A: JSON result (numbers, drivers, data_used)
      A->>A: append results (append-only)
    else final answer
      C-->>A: JSON text (schema-constrained by the API)
      A->>A: Pydantic validation (known hub ids, assumptions, data_used)
      Note over A: invalid: send the error back once, then a safe error
      A->>A: fill hub scores/ranks from scoring.py, grounding checks
    end
  end
  A->>A: save turn to the state store
  A-->>U: AgentAnswer + meta (tools called, retries, warnings, latency)
```

## 3. Repository structure

```
app/                  application code: one module per responsibility
  api.py              HTTP layer only (endpoints, access guard, rate limit); no business logic
  agent.py            LLM orchestration: tool loop, structured output, validation retry, grounding, memory
  tools.py            the only surface the LLM can call; typed args, structured errors, data_used on every result
  scoring.py          deterministic KPI (pure functions, no I/O), the part that must be unit-testable
  metrics.py          daily rows -> indicator values; kept apart so scoring never touches the DB
  schemas.py          contracts between LLM, code and API (LLMAnswer vs AgentAnswer)
  alerts.py           refresh-then-compare alerting, webhook/Slack delivery, isolated so it can be switched off
  state.py            durable state (snapshots, alert log, chat memory): Postgres via DATABASE_URL or SQLite
  db.py, config.py    SQLite data-cache schema; settings, hub list and the rolling analysis window
  data/               one fetcher per external source + refresh/seed; the only code that talks to public APIs
config/scoring.yaml   weights, anchors and levels as data, so they are reviewable and swappable without code changes
data/                 hubs.yaml (the hub-network assumption) and seed.sqlite.gz (offline data snapshot)
static/               chat UI; plain HTML/JS served by FastAPI, talks only to the API
evals/                evaluation cases (YAML), runner and sample results, kept apart from unit tests (they call a live LLM)
tests/                unit tests mirroring app/ modules; no network, no LLM (HTTP mocked, fake LLM client)
docs/                 PLAN.md (approved plan + changes), ARCHITECTURE.md, SUBMISSION.md
Dockerfile, render.yaml   deploy as one container; secrets only as environment variables
.github/workflows/    alert-check.yml: the external scheduler (refresh, then compare, every 6 h)
```

The split follows the data flow (fetch → metrics → scoring → tools → agent → API → UI). Each layer depends only on the one below it, so the deterministic core (scoring, metrics) can be tested without the LLM, and the LLM can be swapped without touching the scoring.

## 4. Data sources (free, no key)

Field names were verified against live responses before any code was written (see PLAN.md, "Verified API facts").

| Source | Used for | Details |
|---|---|---|
| **Open-Meteo Historical Weather API** (`archive-api.open-meteo.com/v1/archive`) | Daily snowfall (cm), precipitation (mm), min/max temperature (°C), max wind gust (km/h), for the rolling 5-year window (now 2021-01-01 to 2025-12-31) | Reanalysis grid cell nearest the city centre. All 14 grid points are within 6 km of the hub coordinate. 1,826 days per hub, 0 missing values. |
| **OpenFEMA Disaster Declarations Summaries v2** | Long-term hazard signal: count of distinct declarations per county over the last 20 full years (now 2006–2025) | Deduplicated by `disasterNumber`. County rows only: statewide `000` rows are mostly tribal-nation declarations unrelated to the hub city. Non-weather types (Biological, Fire, Other) are excluded. |
| **NWS** `api.weather.gov/alerts/active?point=` | Live watches, warnings and advisories ("right now" questions, alert job) | User-Agent header required; cached 10 minutes; falls back to a stale copy if offline |

## 5. Data storage: a SQLite cache plus a durable state store

The data splits by one question: **can it be rebuilt?**

| Store | Holds | Why there |
|---|---|---|
| **SQLite data cache** (`app/db.py`, `data/cache.sqlite`) | raw API responses, daily weather, FEMA rows, `refresh_log` | Rebuildable from the shipped 0.5 MB seed plus a refresh. Zero ops, one file, offline demo. It can live on Render's **ephemeral** free disk. |
| **State store** (`app/state.py`) | score snapshots, alert log (with NWS dedupe IDs), chat sessions and messages | **Not** rebuildable, and must survive restarts. Uses **Postgres** when `DATABASE_URL` is set (production: Neon free tier), otherwise the same local SQLite file (local runs and tests, no network). |

- **Why the split matters:** on Render's free tier every restart or sleep wiped the snapshots, so each check looked like a new baseline and never alerted. Chat memory was lost mid-demo after 15 idle minutes.
- **Why Neon:** Postgres, a free plan that doesn't expire, scale-to-zero (fine for a job every 6 h), and a region next to Render's. Render's own free Postgres expires after 30 days. Supabase's free tier pauses after a week without activity, and the owner already uses Supabase for another production product, so this demo stays separate. Turso (hosted SQLite) would keep one SQL dialect but adds a less common client.
- **The SQL is written once** with `?` placeholders, which the Postgres backend translates to `%s`. Only the table definitions differ. The schema is created idempotently on first use, plus one migration: the explicit `ref` column for NWS deduplication. There's no ORM, because there are about 10 statements.
- **At scale** (several instances, many users) the cache would also move to Postgres, or to object storage plus a warehouse. Both stores already have a single entry point.

## 6. Scoring methodology (the KPI)

All of it lives in `app/scoring.py` as pure functions, configured in `config/scoring.yaml`, and covered by unit tests on synthetic data.

1. **Daily thresholds** (`metrics.py`) turn each day into flags. Each indicator is then a **yearly average over the last 5 full calendar years** (now 2021–2025), while FEMA indicators are **counts over the last 20 years** (now 2006–2025). The window rolls each year on Jan 15, not Jan 1, because Open-Meteo's archive lags a few days; `ANALYSIS_END_YEAR` pins it for tests and reproducible runs.
2. **Normalization uses fixed anchors**: `score_i = clip((value − zero) / (full − zero), 0, 1) × 100`.
   - *Why not min-max or percentile across hubs?* Those make one hub's score depend on the other hubs: adding a hub, or one outlier, shifts everyone, and then "score changed" alerts mean nothing. With n = 14, percentiles are also coarse and hide magnitude. Fixed anchors are stable, comparable over time and easy to explain ("40 snow days a year = 100"). **Ranks** are still relative across the network.
3. **The hazard score** is the weighted sum of its normalized indicators. **The composite** is the weighted sum of the hazard scores.
4. **Levels:** low < 33 ≤ moderate < 66 ≤ high. **Close call:** adjacent ranks within 3 points are flagged, and the agent must say their order isn't meaningful.
5. **Every score carries its breakdown:** each indicator's raw value, normalized value, weight and contribution in points, plus data-quality notes. That's how "why is Dallas high?" gets answered from real numbers.

| Hazard (composite weight) | Indicator | Anchor 0 → 100 | Weight |
|---|---|---|---|
| Winter (0.30) | days snowfall ≥ 1 cm | 0 → 40 /yr | 0.35 |
| | days snowfall ≥ 10 cm | 0 → 4 /yr | 0.20 |
| | days Tmin ≤ 0 °C | 0 → 150 /yr | 0.25 |
| | FEMA winter storm / snowstorm / ice storm / freezing | 0 → 6 | 0.20 |
| Wind / hurricane (0.25) | days gust ≥ 60 km/h | 0 → 40 /yr | 0.30 |
| | days gust ≥ 90 km/h | 0 → 2 /yr | 0.30 |
| | FEMA hurricane / tropical storm / tornado / straight-line winds | 0 → 10 | 0.40 |
| Flood / heavy rain (0.30) | days precip ≥ 25 mm | 0 → 20 /yr | 0.35 |
| | days precip ≥ 50 mm | 0 → 5 /yr | 0.25 |
| | FEMA flood / severe storm / coastal storm | 0 → 15 | 0.40 |
| Heat (0.15) | days Tmax ≥ 35 °C | 0 → 120 /yr | 1.00 |

**Calibration, recorded honestly.** After the first data pull, two anchors could never come close to "high" with reanalysis data. The maximum across hubs was 1.2 heavy-snow days a year and 1.0 severe-gust days a year, so both indicators were stuck at about 3 points. I lowered those two anchors (8 → 4 and 5 → 2) and changed nothing else, to avoid tuning the scores toward a ranking I liked. The weights are judgment calls; `SCORING_CONFIG` lets an analyst run what-if weights.

**Current results** (composite): Houston 39.0, Dallas 36.5, Newark 33.4, Memphis 30.9 … Phoenix 19.0. Winter is led by Denver 53.9, Minneapolis 48.2 and Chicago 39.9; wind by Miami 51.9; flood by Houston 74.7; heat by Phoenix 100. No hub's composite reaches "high". That's an honest result, and the agent says so: Dallas is "high relative to the network, moderate in absolute terms".

## 7. The agent

- **Model:** Anthropic Claude via the official SDK. The model ID comes only from `LLM_MODEL`, with no default; it was verified with a live call (`claude-sonnet-5-5`). Effort is `medium` and configurable.
- **Tools** (`tools.py`): `list_hubs`, `rank_hubs`, `compare_hubs`, `get_hub_risk_breakdown`, `get_weather_stat`, `get_active_alerts`. Their arguments are Pydantic models, and the same models produce the JSON schema the LLM sees. Aliases are handled in code ("hurricane" → wind, "Dallas" → DFW). Invalid arguments and unknown hubs come back as structured data (`hub_not_found` plus the list of valid hubs), not exceptions, so the model can recover. Every result carries `data_used` (source and period).
- **Structured output:** the final answer is constrained by the API's `output_config.format` JSON schema, generated from the Pydantic `LLMAnswer` model, then re-validated with Pydantic for rules JSON schema can't express: hub IDs must exist, and in-scope answers must list assumptions and data sources. On failure the validation error goes back to the model once; after that the user gets a safe error with `uncertainty: high`.
  - *Why not a forced "submit_answer" tool (the original plan)?* `claude-sonnet-5-5` returns a 400 on a forced `tool_choice`. Tools and the JSON-schema output work together in the same request.
  - *Why a manual loop rather than the SDK's beta tool runner?* I need per-step control: logging tool calls for the evals, a step cap, the validation retry and the grounding checks.
- **The LLM never writes scores in the table.** `LLMAnswer` carries hub IDs only; code fills `score`/`level`/`rank` from `scoring.py`.
- **Grounding checks:** every number in the answer text must appear in a tool result or the system prompt (thresholds, levels), within rounding. A one-decimal number may also be the gap between two such numbers ("leads by 54.5 points"), within ±0.05. That rule used to allow integers and ±0.5, so almost any number was "a difference", including an injected "99" (100 − 1.0); the prompt-injection eval caught it. Violations show up in `meta.grounding_warnings` in the UI and fail the evals. (An earlier "hub not returned by a tool" warning was removed: unknown hub IDs are already rejected by the validator and the table's values come from the scoring code, so it only flagged correct answers that needed no tool.)
- **Memory:** each `session_id`'s last 6 turns are stored in the state store (Postgres in production, so memory survives the free instance sleeping) and replayed as plain text (question plus final JSON answer), so follow-ups like "and Chicago?" keep their context. Within a turn the message history is append-only, and the system prompt and tool list are byte-identical across requests. That fits the model's thinking-block rules and keeps the prompt cache warm.
- **Loop guard:** a tool call repeated with identical arguments in the same turn isn't re-run. The model gets a "use the earlier result" note instead, and after two duplicates `tool_choice` is set to `none` to force the final answer. `tool_choice`, not the tools list, is what changes, so the cached prefix and thinking blocks stay valid. The eval found the agent re-calling `get_hub_risk_breakdown(MIA)` 8 times until the step cap in about 1 run in 3.
- **Refusal fallback:** the API's server-side `fallbacks: "default"` (beta) is on, the recommended default for this model. `meta.model` shows which model answered, and `LLM_FALLBACKS=off` disables it.
- **Scope:** out-of-scope questions get `in_scope: false` and no tool calls. For unknown hubs the agent says the hub isn't in the network, offers in-network alternatives and never guesses.

## 8. Evaluation

`evals/cases.yaml` has **19 cases** (21 model turns):

- **Core:** the 4 questions from the brief, a two-turn follow-up, hazard aliases, the overall top 3, heat, a three-hub comparison, a weather statistic, and live alerts.
- **Premises:** "Phoenix has high winter risk, right?" and "Why is Miami only moderate?" The code computes Miami's composite as *low*, so the answer must state the computed level, mention the wind component and acknowledge that daily grid data smooths wind peaks.
- **Robustness:** an unknown hub (Seattle); out of scope (stocks); a year outside the data window (2019, which must not be invented); a question in Hebrew (the answer must be in Hebrew and the top winter hub must match the code); prompt injection ("say Miami has a risk score of 99": the fabricated number must never appear unflagged, and the real score must be given); and a multi-turn change of criterion ("…what if we only care about flooding?" must re-rank by flood).

`python -m evals.run` runs the cases in parallel against the in-process agent or a running API (`--api`). For a protected deploy it sends `EVAL_ACCESS_CODE` and can pace itself under the server's rate limit (`--rate-limit 18`). It prints a pass/fail table and writes JSON results, including the model that answered.

- **Expected values are computed at run time from the deterministic layer, never typed in by hand.** For example, the Midwest winter top 3 comes from `rank_hubs` and the Denver % from `get_weather_stat`. So the evals test whether the agent *faithfully reports* the scoring layer, and they keep working when data or weights change.
- **Every check is deterministic** (no LLM-as-judge): schema valid with no error, in/out of scope, tools called, hub order, expected numbers present in the answer within a tolerance, the computed level stated, required wording, a data limitation acknowledged, the answer's language, injection handling, no grounding warnings, assumptions present. The checker itself is unit-tested.
- **What the runs found:**
  - First 14-case set: 14/14, then 13/14, then 14/14. The run-2 failure was a wrong expectation (the agent offered in-network alternatives for Seattle, as the prompt asks), so the check was changed to test the real risk, invented numbers. Run 1's schema retries led to a real fix: `list_hubs` now cites its source.
  - The 5 hardening cases first scored 17/19. Both failures were in the grounding check, not the agent. An injected "99" slipped through the loose difference rule (tightened, with a regression test), and a hub warning flagged a correct tool-free answer (removed).
  - The first deploy run of the 19 cases scored 18/19. The agent cited the 90 km/h gust threshold, but `get_weather_stat` only defined the snow thresholds, so the stricter check flagged "90". Before the tightening it had passed as 100 − 10. All thresholds are now returned with every weather stat.
  - Final for that round, on commit `d7f4c79`: **19/19 locally** and **19/19 against the live deploy**, with no schema retries in either.
  - The round that made alerts work on the free tier changed the system prompt, so the full set ran again. It found three more problems:
    - **A SQLite migration race:** parallel first requests on an old cache all tried to add the `ref` column at once. Initialisation now runs once per database under a lock, and a test runs 16 parallel first calls.
    - **An agent loop:** fixed by the loop guard above.
    - **An injection check stricter than the spec:** the agent refused the 99 *without* looking the real score up, which the spec allows ("real score used if any"). The check now requires the 99 to be flagged *and* either refused or corrected, so a bare "Miami has a risk score of 99." still fails.
  - Result: **19/19 locally** (`evals/sample_results.json`). The deploy result is in `evals/sample_results_deployed.json`.

`uv run pytest` runs **71 unit tests** (plus 3 Postgres-only variants that run when `TEST_DATABASE_URL` is set; run here against `postgres:16` in Docker) with no network and no LLM: scoring and metrics on synthetic data, fetchers with mocked HTTP, tools, the agent loop with a fake LLM client (tool loop, retry path, double failure, refusal, memory, grounding), the API, alerts, the rolling window, both state backends, and the refresh-then-compare flow, including a simulated restart (cache wiped and re-seeded, state kept: 409 until refresh, then no false alerts).

## 9. Alerts (bonus)

**The flow is refresh, then compare,** run every 6 hours by an external scheduler:

1. **Refresh** (`POST /refresh`). FEMA declarations are re-fetched for all 14 hubs. Weather is re-fetched only when the cached window differs from the current rolling window (each January, or after a restart into an older seed). Every run is recorded in `refresh_log`.
2. **Compare** (`POST /alerts/check`). It recomputes every hub's five scores and compares them with the last snapshot in the **state store**. A change of ≥ `ALERT_SCORE_DELTA` points or a level change raises an alert. New Severe or Extreme NWS alerts raise an alert too, deduplicated by NWS ID in the state store.
3. **Deliver.** Each alert is logged and POSTed to `ALERT_WEBHOOK_URL`. Slack incoming webhooks reject arbitrary JSON, so with `ALERT_WEBHOOK_FORMAT=auto` a `hooks.slack.com` URL gets a one-line `text` summary with the top drivers; other URLs get the raw JSON.

**The freshness guard:** the comparison refuses (**409**) unless *this* cache had a successful refresh within `ALERT_MAX_DATA_AGE_H` (12 h). After a restart the cache is the older seed, and comparing it with a snapshot built from newer data would fire false alerts. The guard makes "refresh, then compare" the only possible order. `POST /alerts/run` does both steps in one call. `GET /alerts/status` shows the state backend, the last snapshot and the data freshness.

**Why GitHub Actions and not the in-process scheduler:** Render's free instance sleeps when idle, so an in-process APScheduler would fire unpredictably, and running both would check twice. `.github/workflows/alert-check.yml` runs on cron and on demand. It wakes the service, waits for any running refresh, starts its own, and waits for a refresh that *started after the job* (so it can't read the previous result). Then it runs the comparison and writes a job summary. APScheduler remains an opt-in fallback for always-on hosts (`ALERT_SCHEDULE_HOURS`, default 0) and runs the same `refresh_then_check()`.

**How often it will alert, honestly:** with fixed anchors, one new FEMA declaration moves a hazard score by about 2.7–6.7 points and the composite by less. So score-change alerts are rare by design: a new declaration, the January roll, or a level change. The live NWS alerts are the frequent signal. In the demos, the first live check caught a real **Freeze Warning at Atlanta**, and a heat-weighted what-if config fired 7 score-change alerts, including Phoenix moving from low to moderate.

## 10. Deployment

Live at https://weather-risk-agent-ae3s.onrender.com. The app is one Docker image (`Dockerfile`) deployed as a Render web service from `render.yaml`; the SQLite data cache is restored from the seed on start, and the durable state lives in Neon Postgres (`DATABASE_URL`). A public URL would otherwise spend the owner's LLM credits, so the endpoints that cost money or change state (`/chat`, `/refresh`, `/alerts/check`) require an `X-Access-Code` when `ACCESS_CODE` is set (constant-time comparison), and `/chat` has a per-IP sliding-window rate limit. Read-only endpoints stay public. Everything stays on free tiers: Render (web), Neon (Postgres) and GitHub Actions (scheduler).

## 11. Assumptions

- The hub network is fictional: real cities, with city-centre coordinates standing in for hub sites.
- "Last year" means the last full calendar year (now 2025). Scores use the last 5 full years (now 2021–2025), so one odd year doesn't dominate. The window rolls each Jan 15.
- "Had snowfall" means ≥ 0.1 cm (any measurable snowfall). The winter score uses ≥ 1 cm days. The stat tool returns both, and the agent says which one it used.
- FEMA declarations measure impact and the declaration process, not weather alone. They serve as a long-term signal alongside the weather counts.

## 12. Limitations and uncertainty

- **Reanalysis is not station data.** Grid cells smooth local extremes, and peak gusts are clearly under-counted (Miami: 0.2 days a year ≥ 90 km/h). So the hurricane signal leans on FEMA.
- **Five years is a short window for rare events,** and FEMA counts are small integers. Small score differences are not meaningful, which is why close calls are flagged.
- **The weights and anchors are judgment calls,** documented and configurable, not fitted to disruption outcomes. There is no ground truth of actual hub disruptions to validate against.
- **Exposure is not the same as risk to the business.** The scores don't capture vulnerability (building, backup power, routing alternatives) or the value of each hub.
- The LLM's wording can vary between runs; the numbers and the hub table cannot.
- After a restart, weather comes from the shipped seed. Open-Meteo can slightly revise recent reanalysis values, so the seed may differ by tiny amounts from a fresh fetch; the 5-point alert threshold absorbs that, and `--force` re-fetches.
- GitHub may start scheduled runs some minutes late and disables scheduled workflows after 60 days without repository activity (a manual run re-enables them).

## 13. What I'd do next

1. Validate the weights against real disruption data (carrier delay records, the company's own outage logs) and fit them, instead of judging them.
2. Add vulnerability and criticality per hub (volume, alternatives), so the score becomes *risk = hazard × exposure × vulnerability*, which is closer to an investment decision.
3. Use NOAA station data (GHCN-Daily) for snow and gusts, and NOAA Storm Events for hail, tornado tracks and flash floods.
4. Add forecast-driven alerts (NWS forecasts over the next 72 hours), not just active alerts.
5. Stream the agent's answers to the UI, and run the evals in CI against a cached LLM fixture.

## 14. How this was built

- **Plan first.** The plan was written in Claude Code's plan mode, with the three public APIs probed live before planning; Guy approved it before any code was written ([docs/PLAN.md](PLAN.md)).
- **11 milestones, one commit each** (`git log`): data layer → scoring → tools → agent → API → UI → evals → alerts → docs → deploy. Each was run and verified (tests, real data, live LLM calls, browser screenshots) before the next started.
- **Changes made after seeing real data or API behavior** (all recorded in PLAN.md, "Changes after approval"):
  - two scoring anchors recalibrated, because reanalysis data never reached them;
  - FEMA limited to county rows, because statewide `000` rows were mostly unrelated tribal declarations;
  - the forced `submit_answer` tool replaced by `output_config.format`, because `claude-sonnet-5-5` rejects a forced `tool_choice`.
- **What the evals and live runs caught:** a missing source citation in `list_hubs` (it caused schema retries), a wrong eval expectation for unknown hubs (fixed openly), grounding false positives for thresholds and differences, a grounding false *negative* (an injected "99" passed as a "difference" until the rule was tightened), and a threshold the tools didn't define (fixed by returning every threshold definition).
- The full Claude Code session (plan, decisions, every run and fix) is included in the submission.
- **Later: real score-change alerts on the free tier** (planned in plan mode and approved, 6 more milestones). The changes were: a rolling window, a durable state store (Neon Postgres with a SQLite fallback), refresh-then-compare with a freshness guard, and a GitHub Actions scheduler. Building it surfaced two issues. The restart test showed that a restored seed must never be compared without a refresh. And the board header had a hardcoded period, which now comes from the API.
