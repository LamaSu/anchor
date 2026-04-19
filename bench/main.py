"""Full bench suite runner.

Runs every sub-bench in sequence, writes bench-results.json, renders
bench/report.html, exits non-zero if the SLO gate fails.

Usage:
  python -m bench.main                  # all benches, default params
  python -m bench.main --quick          # faster: fewer iters, smaller loads
  python -m bench.main --skip slo       # skip named sub-benches
  python -m bench.main --no-exit-slo    # never fail on SLO (local tuning)
"""
from __future__ import annotations

import argparse
import os
import platform
import sys
import time
from datetime import datetime, timezone

import anchord

from bench import ablation as ablation_bench
from bench import bench_http as http_bench
from bench import concurrent as conc_bench
from bench import drift_load as drift_bench
from bench import lifecycle as lifecycle_bench
from bench import report as report_mod
from bench import slo as slo_bench
from bench._lib import anchord as daemon_ctx
from bench._lib import banner, results_path, save_suite, update_metadata


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--quick", action="store_true")
    p.add_argument("--skip", nargs="+", default=[],
                   help="Sub-bench names to skip: http lifecycle drift concurrent ablation slo")
    p.add_argument("--no-exit-slo", action="store_true")
    args = p.parse_args()

    skip = set(args.skip)

    # Reset results file so stale suites from prior runs don't leak in
    rp = results_path()
    rp.write_text('{"metadata": {}, "suites": {}}', encoding="utf-8")

    update_metadata({
        "started_at": datetime.now(timezone.utc).isoformat(),
        "host": os.environ.get("ANCHORD_HOST", "127.0.0.1") + ":" + os.environ.get("ANCHORD_PORT", "ephemeral"),
        "python_version": platform.python_version(),
        "anchor_version": getattr(anchord, "__version__", "0.1.0"),
        "platform": platform.platform(),
        "quick": args.quick,
    })

    banner("anchor · benchmark suite")
    print(f"  results file: {rp}")
    print(f"  quick mode:   {args.quick}")

    t_start = time.perf_counter()

    # ---- HTTP ----
    if "http" not in skip:
        banner("[1/5] HTTP endpoint latency")
        iters = 50 if args.quick else 200
        with daemon_ctx() as (http, _):
            data = http_bench.run(http, iters)
        save_suite("http", data)

    # ---- Lifecycle ----
    if "lifecycle" not in skip:
        banner("[2/5] End-to-end mission lifecycle")
        flows = 5 if args.quick else 15
        with daemon_ctx() as (http, _):
            data = lifecycle_bench.run(http, flows)
        save_suite("lifecycle", data)

    # ---- Drift load ----
    if "drift" not in skip:
        banner("[3/5] Drift-tick under load")
        loads = [10, 50, 200] if args.quick else [10, 50, 200, 500]
        runs_ = 3 if args.quick else 5
        data = drift_bench.run(loads, runs_)
        save_suite("drift_load", data)

    # ---- Concurrency ----
    if "concurrent" not in skip:
        banner("[4/5] Concurrency")
        agent_counts = [1, 4, 8] if args.quick else [1, 4, 16, 32]
        ops = 10 if args.quick else 20
        data = conc_bench.run(agent_counts, ops)
        save_suite("concurrency", data)

    # ---- Ablation ----
    if "ablation" not in skip:
        banner("[5/5] Ablation — with vs without anchor")
        data = ablation_bench.run()
        save_suite("ablation", data)

    # ---- SLO gate (consumes everything above) ----
    slo_verdict = "SKIP"
    if "slo" not in skip:
        banner("[gate] SLO regression check")
        data = slo_bench.run(slo_bench.DEFAULT_THRESHOLDS_MS)
        save_suite("slo", data)
        slo_verdict = data["verdict"]

    dur = time.perf_counter() - t_start
    update_metadata({
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": round(dur, 1),
    })

    html = report_mod.render()
    print()
    print(f"  wrote dashboard: {html}")
    print(f"  wrote results:   {results_path()}")
    print(f"  total wall:      {dur:.1f}s")
    print(f"  SLO verdict:     {slo_verdict}")

    if slo_verdict == "FAIL" and not args.no_exit_slo:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
