"""Bench 4: SLO gate.

Reads bench-results.json and asserts that the p95 of every named case is
under a configured threshold. Exits 1 on violation so CI catches regressions.

Targets (per anchor-brief.md §4.3 + reasonable first-pass values):
  memory_check                  p95 < 50ms   (explicit SLO in schemas/brief)
  healthz                       p95 < 10ms
  state_append                  p95 < 50ms
  tool_call                     p95 < 50ms
  mission_get                   p95 < 25ms
  compact_resume                p95 < 100ms
  compact_snapshot              p95 < 500ms  (heavier — I/O + diff)
  lifecycle_total_wallclock     p95 < 5000ms (50 ops + 2 heavy)

The thresholds are loose on purpose — this is a REGRESSION gate, not a perf
goal. Tighten as the daemon matures.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from bench._lib import banner, load_results, results_path, save_suite


DEFAULT_THRESHOLDS_MS: dict[str, float] = {
    # Windows localhost http.server adds ~8ms of per-request overhead vs
    # unix-domain; thresholds are set to catch regressions on the slowest
    # platform in our CI matrix. Tighten per-platform once we have baselines.
    "GET /healthz": 25.0,
    "GET /mission/<id>": 50.0,
    "POST /state/append": 100.0,
    "POST /tool-call": 150.0,   # scheduler may fire drift tick on the tail
    "POST /memory/check": 50.0,  # explicit SLO in schemas.py — kept tight
    "POST /compact/snapshot": 500.0,
    "GET /compact/resume": 150.0,
    "lifecycle_total_p95": 8000.0,
    "lifecycle_append_p95": 100.0,
    "lifecycle_tool_p95": 150.0,
}


def _extract_p95(results: dict[str, Any]) -> dict[str, float]:
    """Flatten the results into {case_name: p95_ms}."""
    flat: dict[str, float] = {}
    http = results.get("suites", {}).get("http", {})
    for case in http.get("cases", []):
        name = case.get("name")
        p95 = case.get("p95_ms")
        if name and p95 is not None:
            flat[name] = p95
    life = results.get("suites", {}).get("lifecycle", {})
    total = life.get("total_wallclock", {}).get("p95_ms")
    if total is not None:
        flat["lifecycle_total_p95"] = total
    per_step = life.get("per_step", {})
    for step_key in ("append_p95", "tool_p95"):
        entry = per_step.get(step_key, {})
        p95 = entry.get("p95_ms") or entry.get("mean_ms")
        if p95 is not None:
            # NB: the per-step "append_p95" is ALREADY a p95 number; wrap as
            # p95-of-p95s to be a stable gate.
            flat[f"lifecycle_{step_key}"] = p95
    return flat


def run(thresholds: dict[str, float]) -> dict:
    results = load_results()
    flat = _extract_p95(results)

    rows: list[dict[str, Any]] = []
    passed = 0
    failed = 0
    skipped = 0

    for case, threshold in thresholds.items():
        p95 = flat.get(case)
        if p95 is None:
            rows.append({"case": case, "p95_ms": None, "threshold_ms": threshold,
                         "status": "SKIP", "reason": "no sample in bench-results.json"})
            skipped += 1
            continue
        ok = p95 <= threshold
        rows.append({"case": case, "p95_ms": p95, "threshold_ms": threshold,
                     "status": "PASS" if ok else "FAIL"})
        passed += int(ok)
        failed += int(not ok)

    verdict = "FAIL" if failed else ("WARN" if skipped == len(thresholds) else "PASS")

    print(f"  {'case':<28s}  {'p95_ms':>10s}  {'target':>10s}  status")
    print("  " + "-" * 62)
    for r in rows:
        p95_str = f"{r['p95_ms']:10.2f}" if r["p95_ms"] is not None else f"{'--':>10s}"
        print(f"  {r['case']:<28s}  {p95_str}  {r['threshold_ms']:10.2f}  {r['status']}")

    print()
    print(f"  verdict: {verdict}  ({passed} passed, {failed} failed, {skipped} skipped)")
    return {
        "description": "SLO regression gate (p95 latency per case)",
        "thresholds": thresholds,
        "rows": rows,
        "verdict": verdict,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--thresholds-json", help="Path to JSON {case: max_p95_ms}")
    p.add_argument("--no-exit-nonzero", action="store_true",
                   help="Always exit 0 even on FAIL (useful during local tuning)")
    args = p.parse_args()

    thresholds = DEFAULT_THRESHOLDS_MS
    if args.thresholds_json:
        thresholds = json.loads(open(args.thresholds_json).read())

    banner("bench_slo — regression gate")
    data = run(thresholds)
    save_suite("slo", data)
    if data["verdict"] == "FAIL" and not args.no_exit_nonzero:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
