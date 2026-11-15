"""API tests: FastAPI TestClient, agent stubbed (LLM covered in test_agent.py)."""
import pytest
from fastapi.testclient import TestClient

from app import agent, api, tools
from app.schemas import AgentAnswer, ChatMeta, Uncertainty
from tests.test_tools import seeded  # noqa: F401  (synthetic DB fixture)


@pytest.fixture
def client(seeded, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_MODEL", "fake-model")
    with TestClient(api.app) as c:
        yield c


def test_health(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["hubs_with_weather"] == 14 and h["model"] == "fake-model"


def test_scores_and_validation(client):
    r = client.get("/scores", params={"hazard": "winter", "region": "Midwest", "top_n": 3})
    assert r.status_code == 200 and len(r.json()["ranking"]) == 3
    assert client.get("/scores", params={"hazard": "volcano"}).status_code == 422
    assert client.get("/scores", params={"region": "Mars"}).status_code == 422


def test_hubs_and_breakdown(client):
    assert client.get("/hubs", params={"region": "South"}).json()["count"] == 5
    assert client.get("/hubs/DFW/breakdown").json()["hub"]["city"] == "Dallas"
    assert client.get("/hubs/Seattle/breakdown").status_code == 404


def test_chat_returns_structured_answer(client, monkeypatch):
    def fake_chat(session_id, message):
        return AgentAnswer(in_scope=True, answer="ok answer here", hubs=[], focus_hazard="composite",
                           reasoning_steps=[], data_used=[], assumptions=["a"],
                           uncertainty=Uncertainty(level="low", note="n"), follow_up_suggestions=[],
                           meta=ChatMeta(session_id=session_id))
    monkeypatch.setattr(agent, "chat", fake_chat)
    r = client.post("/chat", json={"session_id": "t", "message": "hi"})
    assert r.status_code == 200 and r.json()["meta"]["session_id"] == "t"
    assert client.post("/chat", json={"session_id": "t", "message": ""}).status_code == 422


def test_startup_fails_without_model(seeded, monkeypatch):  # noqa: F811
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="LLM_MODEL"):
        with TestClient(api.app):
            pass
