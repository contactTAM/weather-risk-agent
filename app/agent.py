"""Agent loop: Claude chooses tools and writes the explanation; code does all the numbers.

Flow per user message:
  1. replay prior turns of this session (text only) + new message
  2. loop: model -> tool_use? run validated tools, append results (append-only) -> repeat
  3. final turn is JSON constrained by output_config.format (LLMAnswer schema)
  4. Pydantic validation (+ rules JSON schema can't express); on failure, one retry with the error
  5. code fills hub scores from scoring.py, runs grounding checks, stores the turn
"""
from __future__ import annotations

import json
import os
import re
import time

import anthropic
from pydantic import ValidationError

from functools import lru_cache

from app.config import analysis_window, llm_model, load_hubs
from app import state
from app.db import now_iso
from app.schemas import AgentAnswer, ChatMeta, HubRow, LLMAnswer, ToolCall, Uncertainty, api_json_schema
from app.scoring import COMPOSITE, rank
from app.tools import TOOLS, all_scores, run_tool

MAX_STEPS = 8
HISTORY_TURNS = 6
VALID_IDS = {h.id for h in load_hubs()}
ANSWER_SCHEMA = api_json_schema(LLMAnswer)
TOOL_SCHEMAS = [t.schema() for t in TOOLS.values()]

def system_prompt() -> str:
    """Built from the rolling window; identical for every request within a window (prompt cache)."""
    return _system_prompt(analysis_window().last_full_year)


@lru_cache
def _system_prompt(last_full_year: int) -> str:
    win = analysis_window()
    return f"""You are the Weather Risk Intelligence Agent for a logistics company's US distribution hubs.
Analysts use you to decide which hubs are most exposed to weather disruption and should get resilience investment.

Hub network (fictional company, real cities): {", ".join(f"{h.id}={h.city}, {h.state} ({h.region})" for h in load_hubs())}.

How you work:
- Every number you state must come from a tool result in this conversation. Never estimate or recall weather facts.
- Scores are deterministic, 0-100, from fixed anchors: low <33 <= moderate <66 <= high. Hazards: winter, wind
  (hurricane and high wind), flood (flood and heavy rain), heat, and composite (the Weather Disruption Risk Score).
- "Last year" means calendar year {win.last_full_year}. Scores use a {win.label} baseline of yearly averages
  (the last {len(win.baseline_years)} full calendar years, rolling each January) plus FEMA county declarations
  {win.fema_start}-{win.fema_end}. Weather data exists only for {win.label}. Say which period you used.
- Explain "why" with the top contributing indicators and their real values (e.g. "54.6 days/yr above 35 C").
- Distinguish relative from absolute: a hub can rank high in the network while its score is only "moderate".
  If the user's premise is off (e.g. calls a moderate score "high"), say so politely.
- If close_call_with_next is true, say the order between those hubs is not meaningful.
- For "right now"/current conditions use get_active_alerts; live alerts don't change historical scores.
- Hubs not in the network: say so and list nearby alternatives from the network; do not guess.
- Questions outside weather/hazard risk for these hubs: set in_scope=false, answer briefly what you can help with,
  call no tools, hub_ids=[].
- Follow-ups ("and Chicago?") refer to the previous question's hazard/metric unless stated otherwise.
- Final reply: JSON matching the schema. hub_ids in the order you discuss them (most exposed first).
  data_used: when in_scope, always cite the data_used entries from the tool results you relied on (never empty).
  assumptions: the ones that matter for this answer. uncertainty: say what limits confidence (reanalysis grid
  data, FEMA declarations reflect impact not only weather, close calls, small differences)."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _request_kwargs() -> dict:
    kw = dict(model=llm_model(), max_tokens=16000, system=system_prompt(), tools=TOOL_SCHEMAS,
              output_config={"format": {"type": "json_schema", "schema": ANSWER_SCHEMA},
                             "effort": os.getenv("LLM_EFFORT", "medium")})
    if os.getenv("LLM_FALLBACKS", "default") != "off":
        # Refusal fallback (recommended for claude-sonnet-5-5): server retries on a fallback model if
        # a safety classifier declines. We log which model actually answered (meta.model).
        kw |= {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    return kw


# ---------------- memory ----------------
def load_history(session_id: str) -> list[dict]:
    return state.load_history(session_id, HISTORY_TURNS * 2)


def save_turn(session_id: str, user_msg: str, answer_text: str) -> None:
    state.save_turn(session_id, user_msg, answer_text, now_iso())


# ---------------- grounding ----------------
_NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


def _numbers_in(obj, out: set[float]) -> set[float]:
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        out.update(float(x) for x in _NUM.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _numbers_in(v, out)
    return out


def ungrounded_numbers(text: str, tool_results: list[dict]) -> list[str]:
    """Numbers in the answer that don't appear in any tool result or the system prompt (rounding tolerance).
    Ignores years and small integers (ranks/counts like 'top 3', '14 hubs')."""
    known = _numbers_in(system_prompt(), set())
    for r in tool_results:
        _numbers_in(r, known)
    known_list = sorted(known)
    bad = []
    for s in _NUM.findall(text):
        v = float(s)
        if (2000 <= v <= 2030 and "." not in s) or (v.is_integer() and abs(v) <= 20):
            continue
        tol = 0.051 if "." in s else 0.51
        derived = {k * 100 for k in known} | {k / 100 for k in known}
        if any(abs(v - k) <= tol for k in known | derived):
            continue
        # A computed gap between two one-decimal tool values ("leads by 54.5 points" = 100.0 - 45.5) is
        # grounded. Only for decimal numbers and with a tight tolerance: with integers and +/-0.5 almost any
        # number is the difference of two known ones (an injected "99" passed as 100 - 1.0; eval caught it).
        if "." in s and any(abs(abs(a - b) - v) <= 0.051 for i, a in enumerate(known_list) for b in known_list[i + 1:]):
            continue
        bad.append(s)
    return bad


# ---------------- finalize ----------------
def build_hub_rows(hub_ids: list[str], hazard: str) -> list[HubRow]:
    scores = all_scores()
    ranked = {r.hub_id: r for r in rank(list(scores.values()), hazard)}
    hubs = {h.id: h for h in load_hubs()}
    return [HubRow(hub_id=i, city=hubs[i].city, region=hubs[i].region, hazard=hazard,
                   score=ranked[i].score, level=ranked[i].level, rank=ranked[i].rank, of=len(ranked))
            for i in hub_ids if i in ranked]


def _safe_error(meta: ChatMeta, msg: str) -> AgentAnswer:
    meta.error = msg
    return AgentAnswer(in_scope=True, answer="Sorry, I couldn't produce a reliable answer to that. " + msg,
                       hubs=[], focus_hazard=COMPOSITE, reasoning_steps=[], data_used=[], assumptions=[],
                       uncertainty=Uncertainty(level="high", note="No validated answer was produced."),
                       follow_up_suggestions=["Which hubs are most exposed overall?"], meta=meta)


def chat(session_id: str, message: str, client: anthropic.Anthropic | None = None) -> AgentAnswer:
    t0 = time.monotonic()
    client = client or _client()
    meta = ChatMeta(session_id=session_id)
    messages: list[dict] = load_history(session_id) + [{"role": "user", "content": message}]
    tool_results: list[dict] = []
    kwargs = _request_kwargs()
    retried = False

    for _ in range(MAX_STEPS):
        meta.steps += 1
        try:
            resp = client.beta.messages.create(messages=messages, **kwargs)
        except anthropic.APIConnectionError:
            return _finish(_safe_error(meta, "The language model could not be reached."), t0)
        except anthropic.RateLimitError:
            return _finish(_safe_error(meta, "The language model is rate-limited; try again in a minute."), t0)
        except anthropic.APIStatusError as e:
            return _finish(_safe_error(meta, f"Language model error ({e.status_code})."), t0)
        meta.model = resp.model

        if resp.stop_reason == "refusal":
            return _finish(_safe_error(meta, "The request was declined by the model's safety system."), t0)
        if resp.stop_reason == "max_tokens":
            return _finish(_safe_error(meta, "The answer was too long."), t0)

        messages.append({"role": "assistant", "content": resp.content})  # append-only, verbatim

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if tool_uses:
            results = []
            for tu in tool_uses:
                out = run_tool(tu.name, tu.input)
                err = out.get("error") if isinstance(out, dict) else None
                meta.tools_called.append(ToolCall(name=tu.name, args=tu.input or {}, ok=err is None, error=err))
                tool_results.append(out)
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": json.dumps(out, default=str), "is_error": err is not None})
            messages.append({"role": "user", "content": results})  # all results in one message
            continue

        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            parsed = LLMAnswer.model_validate_json(text, context={"valid_hub_ids": VALID_IDS})
        except ValidationError as e:
            meta.validation_errors.append("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())[:500])
            if retried:
                return _finish(_safe_error(meta, "The answer failed schema validation twice."), t0)
            retried = True
            meta.validation_retries += 1
            messages.append({"role": "user", "content":
                             f"Your JSON failed validation: {e.errors(include_url=False, include_context=False)}. "
                             "Return a corrected JSON answer only."})
            continue

        answer = AgentAnswer(**parsed.model_dump(exclude={"hub_ids"}),
                             hubs=build_hub_rows(parsed.hub_ids, parsed.focus_hazard), meta=meta)
        # (No "hub not returned by a tool" warning: unknown ids are rejected by the validator and table
        #  values are filled from scoring.py, so it only flagged correct tool-free answers.)
        meta.grounding_warnings += [f"number {n} not found in tool results"
                                    for n in ungrounded_numbers(parsed.answer, tool_results)]
        save_turn(session_id, message, text)
        return _finish(answer, t0)

    return _finish(_safe_error(meta, f"Stopped after {MAX_STEPS} steps without a final answer."), t0)


def _finish(ans: AgentAnswer, t0: float) -> AgentAnswer:
    ans.meta.latency_ms = int((time.monotonic() - t0) * 1000)
    return ans


if __name__ == "__main__":  # quick CLI: python -m app.agent "question" [session]
    import sys
    a = chat(sys.argv[2] if len(sys.argv) > 2 else "cli", sys.argv[1])
    print(a.model_dump_json(indent=2))
