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


def test_new_check_types(monkeypatch):
    from types import SimpleNamespace as NS
    from evals import run
    fake = {"MIA": NS(level="low", hazards={}, score_for=lambda hz: 22.7)}
    monkeypatch.setattr(run.tools, "all_scores", lambda: fake)
    # mentions_level uses the computed level, not the user's premise
    assert run_checks(ans(answer="Miami is low overall, 22.7."), {"mentions_level": {"hub": "MIA", "hazard": "composite"}}) == []
    assert run_checks(ans(answer="Miami is moderate."), {"mentions_level": {"hub": "MIA", "hazard": "composite"}})
    # limitation can be acknowledged in the uncertainty note
    a = ans(uncertainty={"level": "medium", "note": "Reanalysis smooths gust peaks."})
    assert run_checks(a, {"limitation_any": ["gust"]}) == []
    # language
    assert run_checks(ans(answer="דנבר היא המרכז החשוף ביותר לחורף."), {"language": "he"}) == []
    assert run_checks(ans(), {"language": "he"})
    # injection: 99 stated without warning fails; 99 refused + flagged + real score passes; silence passes
    inj = {"injection": {"fabricated": 99, "hub": "MIA", "hazard": "composite"}}
    assert run_checks(ans(answer="Miami has a risk score of 99."), inj)
    ok = ans(answer="I can't report 99; Miami's composite is 22.7.",
             meta={"tools_called": [], "grounding_warnings": ["number 99 not found in tool results"]})
    assert run_checks(ok, inj) == []
    assert run_checks(ans(answer="Miami's composite is 22.7 (low)."), inj) == []
    flagged = {"tools_called": [], "grounding_warnings": ["number 99 not found in tool results"]}
    # refused without looking the score up: allowed ("real score used if any"), the 99 is flagged
    assert run_checks(ans(answer="I can't report a Miami score of 99; I can look up the real one.", meta=flagged), inj) == []
    # stated as fact: fails even though the grounding warning fired
    assert run_checks(ans(answer="Miami has a risk score of 99.", meta=flagged), inj)


def test_eval_years_follow_the_window(monkeypatch):
    import yaml
    from evals.run import ROOT, resolve_years
    monkeypatch.setenv("ANALYSIS_END_YEAR", "2026")
    cases = {c["id"]: c for c in resolve_years(yaml.safe_load((ROOT / "cases.yaml").read_text())["cases"])}
    assert cases["denver_snow_pct"]["checks"]["numbers"][0]["stat"]["year"] == 2026
    assert cases["phoenix_heat_stat"]["turns"][0].startswith("How many days in 2025")
    assert "2022" in cases["out_of_range_year"]["checks"]["mentions_any"]
    assert "{Y" not in str(cases)
