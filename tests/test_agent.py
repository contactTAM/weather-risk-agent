"""Agent loop tests with a fake Anthropic client (no network, no API cost)."""
import json
from types import SimpleNamespace as NS

import pytest

from app import agent, tools
from app.schemas import LLMAnswer, api_json_schema


def text_block(obj):
    return NS(type="text", text=json.dumps(obj) if not isinstance(obj, str) else obj)


def tool_block(name, args, id_="tu_1"):
    return NS(type="tool_use", name=name, input=args, id=id_)


def resp(*blocks, stop="end_turn"):
    return NS(content=list(blocks), stop_reason=stop, model="fake-model")


GOOD = {"in_scope": True, "answer": "Minneapolis is the most exposed Midwest hub for winter.",
        "hub_ids": ["MSP"], "focus_hazard": "winter", "reasoning_steps": ["ranked"],
        "data_used": [{"source": "Open-Meteo", "period": "2021-2025"}],
        "assumptions": ["Fictional hub network"], "uncertainty": {"level": "medium", "note": "reanalysis"},
        "follow_up_suggestions": ["And Chicago?"]}


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=lambda o: o.__dict__)))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def env(tmp_db, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "fake-model")
    fake_scores = {}

    from app.scoring import DataQuality, score_indicators, load_config
    q = DataQuality(weather_years=[2025], weather_days=365, missing_days=0, grid_distance_km=1,
                    fema_window="2006-2025", notes=[])
    for i, h in enumerate(agent.load_hubs()):
        fake_scores[h.id] = score_indicators(h.id, {"snow_days_1cm": i * 3}, load_config(), q)
    monkeypatch.setattr(tools, "_SCORES", fake_scores)
    monkeypatch.setattr(agent, "all_scores", lambda: fake_scores)
    yield


def test_tool_loop_then_validated_answer_with_code_filled_scores():
    c = FakeClient([resp(tool_block("rank_hubs", {"hazard": "winter", "region": "Midwest"}), stop="tool_use"),
                    resp(text_block(GOOD))])
    a = agent.chat("s1", "Midwest winter?", client=c)
    assert a.meta.error is None and a.meta.steps == 2
    assert [t.name for t in a.meta.tools_called] == ["rank_hubs"] and a.meta.tools_called[0].ok
    assert a.hubs[0].hub_id == "MSP" and a.hubs[0].hazard == "winter"
    assert a.hubs[0].score == tools.all_scores()["MSP"].hazards["winter"].score   # from scoring, not the LLM
    # tool results go back in one user message, request carries the JSON-schema output format
    second = c.calls[1]
    assert second["messages"][-1]["content"][0]["type"] == "tool_result"
    assert second["output_config"]["format"]["type"] == "json_schema"


def test_invalid_answer_retried_once_then_ok():
    bad = dict(GOOD, hub_ids=["SEA"])
    c = FakeClient([resp(text_block(bad)), resp(text_block(GOOD))])
    a = agent.chat("s2", "q", client=c)
    assert a.meta.validation_retries == 1 and a.meta.error is None
    assert "unknown hub ids" in a.meta.validation_errors[0]
    assert "failed validation" in c.calls[1]["messages"][-1]["content"]


def test_invalid_twice_returns_safe_error():
    c = FakeClient([resp(text_block("not json")), resp(text_block({"answer": "x"}))])
    a = agent.chat("s3", "q", client=c)
    assert a.meta.error and "validation twice" in a.meta.error and a.hubs == []
    assert a.uncertainty.level == "high"


def test_refusal_is_safe_error():
    a = agent.chat("s4", "q", client=FakeClient([resp(stop="refusal")]))
    assert "declined" in a.meta.error


def test_in_scope_answer_requires_assumptions():
    with pytest.raises(Exception):
        LLMAnswer.model_validate(dict(GOOD, assumptions=[]))
    LLMAnswer.model_validate(dict(GOOD, in_scope=False, assumptions=[], data_used=[]))


def test_history_is_replayed_for_follow_ups():
    agent.chat("s5", "Rank Midwest by flood", client=FakeClient([resp(text_block(GOOD))]))
    c = FakeClient([resp(text_block(GOOD))])
    agent.chat("s5", "and Chicago?", client=c)
    msgs = c.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"] == "Rank Midwest by flood" and msgs[2]["content"] == "and Chicago?"


def test_grounding_flags_invented_numbers():
    c = FakeClient([resp(tool_block("rank_hubs", {"hazard": "winter"}), stop="tool_use"),
                    resp(text_block(dict(GOOD, answer="Minneapolis scores 77.7 on winter, very exposed.")))])
    a = agent.chat("s6", "q", client=c)
    assert any("77.7" in w for w in a.meta.grounding_warnings)


def test_api_schema_is_strict():
    s = api_json_schema(LLMAnswer)
    assert s["additionalProperties"] is False and set(s["required"]) == set(LLMAnswer.model_fields)
    for d in s.get("$defs", {}).values():
        assert d["additionalProperties"] is False


def test_grounding_accepts_differences_of_tool_numbers():
    assert agent.ungrounded_numbers("leads by 54.5 points", [{"a": 100.0, "b": 45.5}]) == []
    assert agent.ungrounded_numbers("leads by 61.3 points", [{"a": 100.0, "b": 45.5}]) == ["61.3"]
