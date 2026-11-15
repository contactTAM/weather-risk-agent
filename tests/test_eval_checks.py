"""The eval checker itself must be right: test it on hand-made answers."""
from evals.run import run_checks


def ans(**kw):
    base = {"in_scope": True, "answer": "Dallas scores 36.5, moderate.", "hubs": [{"hub_id": "DFW"}],
            "assumptions": ["a"], "meta": {"tools_called": [{"name": "get_hub_risk_breakdown"}], "grounding_warnings": []}}
    base.update(kw)
    return base


def test_passes_and_fails_as_expected():
    assert run_checks(ans(), {"in_scope": True, "hubs_include": ["DFW"], "mentions_all": ["moderate"]}) == []
    assert run_checks(ans(), {"hubs_include": ["MIA"]})
    assert run_checks(ans(), {"tools_none": True})
    assert run_checks(ans(meta={"error": "boom"}), {})[0].startswith("agent error")
    assert run_checks(ans(assumptions=[]), {}) == ["no assumptions stated"]
    assert run_checks(ans(meta={"tools_called": [], "grounding_warnings": ["x"]}), {"no_grounding_warnings": True})
