"""Schemas shared by the LLM, the agent loop and the API.

Two layers on purpose:
- LLMAnswer: what the LLM must return (enforced by the API via output_config.format JSON schema,
  then re-validated here with Pydantic, including rules JSON schema can't express).
  It carries hub_ids only - the LLM never writes the scores in the table.
- AgentAnswer: what the API returns. Code fills hub scores/ranks from scoring.py.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

Hazard = Literal["composite", "winter", "wind", "flood", "heat"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # -> additionalProperties: false


class DataSource(_Strict):
    source: str
    period: str


class Uncertainty(_Strict):
    level: Literal["low", "medium", "high"]
    note: str


class LLMAnswer(_Strict):
    in_scope: bool = Field(description="false if the question is not about weather/hazard risk of the hub network")
    answer: str = Field(description="Direct answer for a logistics analyst, 2-6 sentences, numbers from tools only")
    hub_ids: list[str] = Field(description="Hub ids the answer is about, most exposed first (e.g. ['MSP','CHI']); [] if none")
    focus_hazard: Hazard = Field(description="Which score the hub table should show")
    reasoning_steps: list[str] = Field(description="Short steps: which tools/data were used and how the conclusion follows")
    data_used: list[DataSource]
    assumptions: list[str]
    uncertainty: Uncertainty
    follow_up_suggestions: list[str] = Field(description="2-3 natural next questions")

    @field_validator("answer")
    @classmethod
    def _answer_nonempty(cls, v: str) -> str:
        if len(v.strip()) < 10:
            raise ValueError("answer is empty or too short")
        return v

    @field_validator("hub_ids")
    @classmethod
    def _known_hubs(cls, v: list[str], info: ValidationInfo) -> list[str]:
        valid = (info.context or {}).get("valid_hub_ids")
        v = [h.strip().upper() for h in v]
        if valid is not None:
            bad = [h for h in v if h not in valid]
            if bad:
                raise ValueError(f"unknown hub ids {bad}; valid ids are {sorted(valid)}")
        return list(dict.fromkeys(v))  # dedupe, keep order

    @model_validator(mode="after")
    def _scoped_answers_state_assumptions(self):
        if self.in_scope and not self.assumptions:
            raise ValueError("in-scope answers must list at least one assumption")
        if self.in_scope and not self.data_used:
            raise ValueError("in-scope answers must list data_used")
        return self


def api_json_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema -> shape the structured-outputs API accepts: every property required,
    additionalProperties false, no numeric/string constraints (those are validated client-side)."""
    schema = model.model_json_schema()
    drop = {"minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
            "multipleOf", "minItems", "maxItems", "default", "title"}

    def fix(node):
        if isinstance(node, dict):
            for k in list(node):
                if k in drop and not isinstance(node.get(k), dict):
                    node.pop(k)
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for v in node.values():
                fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)
        return node

    return fix(schema)


class HubRow(BaseModel):
    hub_id: str
    city: str
    region: str
    hazard: Hazard
    score: float
    level: str
    rank: int
    of: int


class ToolCall(BaseModel):
    name: str
    args: dict
    ok: bool
    error: str | None = None


class ChatMeta(BaseModel):
    session_id: str
    model: str | None = None
    steps: int = 0
    tools_called: list[ToolCall] = []
    validation_retries: int = 0
    validation_errors: list[str] = []
    grounding_warnings: list[str] = []
    latency_ms: int = 0
    error: str | None = None


class AgentAnswer(BaseModel):
    in_scope: bool
    answer: str
    hubs: list[HubRow]
    focus_hazard: Hazard
    reasoning_steps: list[str]
    data_used: list[DataSource]
    assumptions: list[str]
    uncertainty: Uncertainty
    follow_up_suggestions: list[str]
    meta: ChatMeta


class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=100)
    message: str = Field(..., min_length=1, max_length=2000)
