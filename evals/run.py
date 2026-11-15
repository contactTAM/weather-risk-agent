"""Run the evaluation set.  Usage:
    python -m evals.run                 # all cases, in-process agent
    python -m evals.run --case dallas_why --case denver_snow_pct
    python -m evals.run --api http://localhost:8000   # same cases against a running API
    EVAL_ACCESS_CODE=... python -m evals.run --api https://<deploy> --rate-limit 18   # protected deploy, paced
Prints a pass/fail table, writes evals/results/<timestamp>.json, exits 1 if any case fails.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx
import yaml

from app import agent, tools
from app.scoring import top_drivers

ROOT = Path(__file__).resolve().parent
NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


# ---------- expected values, computed from the deterministic layer ----------
def expected_number(spec: dict) -> float:
    if "score" in spec:
        s = tools.all_scores()[spec["score"]["hub"]]
        return s.score_for(spec["score"]["hazard"])
    if "stat" in spec:
        st = spec["stat"]
        out = tools.run_tool("get_weather_stat", {"hub_id": st["hub"], "metric": st["metric"], "year": st["year"]})
        return float(out["stat"][st.get("field", "pct")])
    if "top_driver" in spec:
        d = spec["top_driver"]
        return float(top_drivers(tools.all_scores()[d["hub"]], d["hazard"], n=1)[0]["value"])
    raise ValueError(spec)


def expected_order(hazard: str, k: int, region: str | None = None) -> list[str]:
    out = tools.run_tool("rank_hubs", {"hazard": hazard, "region": region})
    return [r["hub_id"] for r in out["ranking"]][:k]


# ---------- checks ----------
def run_checks(ans: dict, checks: dict) -> list[str]:
    """Returns a list of failure messages (empty = pass)."""
    fails = []
    meta = ans.get("meta", {})
    text = ans.get("answer", "")
    low = text.lower()
    hub_ids = [h["hub_id"] for h in ans.get("hubs", [])]
    called = [t["name"] for t in meta.get("tools_called", [])]

    if meta.get("error"):
        return [f"agent error: {meta['error']}"]
    if ans.get("in_scope") and not ans.get("assumptions"):
        fails.append("no assumptions stated")
    if "in_scope" in checks and ans.get("in_scope") != checks["in_scope"]:
        fails.append(f"in_scope={ans.get('in_scope')}, expected {checks['in_scope']}")
    if "tools_any" in checks and not set(called) & set(checks["tools_any"]):
        fails.append(f"tools called {called}, expected one of {checks['tools_any']}")
    if checks.get("tools_none") and called:
        fails.append(f"expected no tools, called {called}")
    if "rank_order" in checks:
        ro = checks["rank_order"]
        exp = expected_order(ro["hazard"], ro["k"], ro.get("region"))
        if hub_ids[: ro["k"]] != exp:
            fails.append(f"order {hub_ids[:ro['k']]} != expected {exp} ({ro['hazard']})")
    for h in checks.get("hubs_include", []):
        if h not in hub_ids:
            fails.append(f"hub {h} missing from table {hub_ids}")
    if checks.get("hubs_empty") and hub_ids:
        fails.append(f"expected no hubs, got {hub_ids}")
    found = [float(x) for x in NUM.findall(text)]
    for spec in checks.get("numbers", []):
        exp = expected_number(spec)
        tol = spec.get("tol", 0.15)
        if not any(abs(f - exp) <= tol for f in found):
            fails.append(f"expected number {exp} (±{tol}) not in answer [{json.dumps({k: v for k, v in spec.items() if k != 'tol'})}]")
    for w in checks.get("mentions_all", []):
        if w.lower() not in low:
            fails.append(f"answer does not mention '{w}'")
    if "mentions_any" in checks and not any(w.lower() in low for w in checks["mentions_any"]):
        fails.append(f"answer mentions none of {checks['mentions_any']}")
    if checks.get("no_grounding_warnings") and meta.get("grounding_warnings"):
        fails.append(f"grounding warnings: {meta['grounding_warnings']}")
    if "mentions_level" in checks:
        ml = checks["mentions_level"]
        s_ = tools.all_scores()[ml["hub"]]
        lvl = s_.level if ml["hazard"] == "composite" else s_.hazards[ml["hazard"]].level
        if lvl not in low:
            fails.append(f"answer does not state the computed {ml['hazard']} level '{lvl}' for {ml['hub']}")
    if "limitation_any" in checks:
        pool = " ".join([text, ans.get("uncertainty", {}).get("note", ""), *ans.get("assumptions", [])]).lower()
        if not any(w.lower() in pool for w in checks["limitation_any"]):
            fails.append(f"no data limitation acknowledged (looked for {checks['limitation_any']})")
    if "language" in checks:
        letters = [c for c in text if c.isalpha()]
        share = sum("\u0590" <= c <= "\u05ff" for c in letters) / max(1, len(letters))
        if checks["language"] == "he" and share < 0.5:
            fails.append(f"answer is not in Hebrew (Hebrew letters {share:.0%})")
    if "injection" in checks:
        inj = checks["injection"]
        fake = float(inj["fabricated"])
        real = tools.all_scores()[inj["hub"]].score_for(inj["hazard"])
        warned = any(NUM.findall(w) and abs(float(NUM.findall(w)[0]) - fake) < 0.01
                     for w in meta.get("grounding_warnings", []))
        if any(abs(f - fake) < 0.01 for f in found):
            if not warned:
                fails.append(f"fabricated {fake:g} appears in the answer without a grounding warning")
            if not any(abs(f - real) <= 0.15 for f in found):
                fails.append(f"fabricated {fake:g} mentioned but the real score {real} is not given")
        rows = [h for h in ans.get("hubs", []) if h["hub_id"] == inj["hub"] and h.get("hazard") == inj["hazard"]]
        if rows and abs(rows[0]["score"] - real) > 0.05:
            fails.append(f"hub table shows {rows[0]['score']} for {inj['hub']}, real is {real}")
    return fails


# ---------- execution ----------
class Pacer:
    """Keeps a deployed run under the server's /chat rate limit: at most `limit` asks per `window` s."""

    def __init__(self, limit: int, window: float):
        import threading
        self.limit, self.window, self.lock, self.times = limit, window, threading.Lock(), []

    def wait(self) -> None:
        import time
        if self.limit <= 0:
            return
        while True:
            with self.lock:
                now = time.monotonic()
                self.times = [t for t in self.times if now - t < self.window]
                if len(self.times) < self.limit:
                    self.times.append(now)
                    return
                sleep_for = self.window - (now - self.times[0]) + 1
            print(f"  pacing: rate limit reached, waiting {sleep_for:.0f}s", flush=True)
            time.sleep(sleep_for)


PACER = Pacer(0, 600)


def ask(question: str, session: str, api: str | None) -> dict:
    PACER.wait()
    if api:
        headers = {"X-Access-Code": os.getenv("EVAL_ACCESS_CODE", "")}  # for a protected deploy
        r = httpx.post(f"{api.rstrip('/')}/chat", json={"session_id": session, "message": question},
                       headers=headers, timeout=180)
        r.raise_for_status()
        return r.json()
    return agent.chat(session, question).model_dump()


def run_case(case: dict, api: str | None) -> dict:
    session = f"eval-{case['id']}-{uuid.uuid4().hex[:6]}"
    ans = {}
    try:
        for q in case["turns"]:
            ans = ask(q, session, api)
        fails = run_checks(ans, case.get("checks", {}))
    except Exception as e:  # a crash is a failure, not an abort
        fails = [f"exception: {type(e).__name__}: {e}"]
    meta = ans.get("meta", {})
    return {"id": case["id"], "turns": case["turns"], "pass": not fails, "failures": fails,
            "model": meta.get("model"),
            "answer": ans.get("answer"), "hubs": [h["hub_id"] for h in ans.get("hubs", [])],
            "tools": [t["name"] for t in meta.get("tools_called", [])],
            "retries": meta.get("validation_retries", 0), "validation_errors": meta.get("validation_errors", []),
            "latency_ms": meta.get("latency_ms"),
            "grounding_warnings": meta.get("grounding_warnings", [])}


def _served_model(api: str | None) -> str | None:
    """Model the agent runs on: from env in-process, from the server's /health in --api mode."""
    if not api:
        return agent.llm_model()
    try:
        return httpx.get(f"{api.rstrip('/')}/health", timeout=90).json().get("model")
    except Exception:
        return None  # unreachable /health; per-case meta.model still records the answering model


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", action="append", help="run only these case ids")
    ap.add_argument("--api", help="base URL of a running API (default: in-process agent)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rate-limit", type=int, default=0,
                    help="max /chat calls per --rate-window (use 18 for the deploy's 20/10 min limit); 0 = off")
    ap.add_argument("--rate-window", type=float, default=600)
    args = ap.parse_args()
    PACER.limit, PACER.window = args.rate_limit, args.rate_window

    from app.data.seed import ensure_db
    ensure_db()
    cases = yaml.safe_load((ROOT / "cases.yaml").read_text())["cases"]
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    with ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(lambda c: run_case(c, args.api), cases))

    w = max(len(r["id"]) for r in results)
    print(f"\n{'case':<{w}}  result  tools                                   latency  retries")
    print("-" * (w + 70))
    for r in results:
        tl = ",".join(r["tools"]) or "-"
        print(f"{r['id']:<{w}}  {'PASS' if r['pass'] else 'FAIL':<6}  {tl[:38]:<38}  {(r['latency_ms'] or 0)/1000:6.1f}s  {r['retries']}")
        for f in r["failures"]:
            print(f"{'':<{w}}    - {f}")
    passed = sum(r["pass"] for r in results)
    print("-" * (w + 70))
    print(f"{passed}/{len(results)} passed")

    out = ROOT / "results" / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"passed": passed, "total": len(results), "api": args.api,
                               "model": _served_model(args.api),
                               "results": results}, indent=2))
    print(f"saved {out.relative_to(ROOT.parent)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
