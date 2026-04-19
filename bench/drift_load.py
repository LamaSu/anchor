"""Bench 3: drift-tick latency vs. state size.

run_all() walks the entire WorkingState — how does it scale?

Strategy:
  For each load (N decisions):
    1. Seed the agent with a mission + N decisions
    2. Fire 25 tool-calls; the 25th triggers a drift tick server-side
    3. Time the tick-triggering tool-call — that's the drift-tick wallclock
    4. Also time run_all() in-process for a cleaner number without network
"""
from __future__ import annotations

import argparse
import importlib
import os
import tempfile
import time
from pathlib import Path

from bench._lib import (
    HTTP,
    anchord,
    banner,
    print_row,
    save_suite,
    summarize,
)


def _seed(http: HTTP, agent: str, n_decisions: int) -> None:
    http.post(
        "/mission/new",
        {
            "agent_id": agent,
            "prompt": (
                "Build sensor API. Must use postgres. Must tolerate offline. "
                "Ship by Friday. Tests must pass."
            ),
        },
    )
    for i in range(n_decisions):
        http.post(
            "/state/append",
            {
                "agent_id": agent,
                "kind": "decision",
                "text": f"decision {i}: implement component {i} using method {i % 7}",
                "source": "agent",
                "confidence": 0.8,
            },
        )


def _time_tick_via_http(http: HTTP, agent: str, runs: int) -> list[float]:
    """Fire 25 tool-calls and time the 25th (which triggers drift)."""
    samples: list[float] = []
    for _ in range(runs):
        # 24 fast tool-calls
        for i in range(24):
            http.post(
                "/tool-call",
                {"agent_id": agent, "tool": "Read", "ok": True, "summary": f"t{i}"},
            )
        # the 25th — this one triggers the tick
        _, _, dt = http.post(
            "/tool-call",
            {"agent_id": agent, "tool": "Read", "ok": True, "summary": "t24"},
        )
        samples.append(dt)
    return samples


def _time_runall_inproc(agent: str, runs: int) -> list[float]:
    """Call anchord.drift.run_all directly in-process (no HTTP overhead)."""
    # reload drift in case ANCHOR_HOME changed
    from anchord import drift  # noqa: WPS433
    importlib.reload(drift)
    samples: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        drift.run_all(agent)
        samples.append((time.perf_counter() - t0) * 1000)
    return samples


def run(loads: list[int], runs_per_load: int) -> dict:
    runs_summary = []
    # Use one isolated home for the whole bench
    home = Path(tempfile.mkdtemp(prefix="anchor-bench-drift-"))
    os.environ["ANCHOR_HOME"] = str(home)

    with anchord(anchor_home=home) as (http, _):
        for n in loads:
            agent = f"drift-load-{n}"
            print(f"\n  seeding agent {agent} with {n} decisions...")
            t0 = time.perf_counter()
            _seed(http, agent, n)
            seed_s = time.perf_counter() - t0
            print(f"  seeded in {seed_s:.1f}s")

            http_samples = _time_tick_via_http(http, agent, runs_per_load)
            inproc_samples = _time_runall_inproc(agent, runs_per_load)

            s_http = summarize(http_samples)
            s_inproc = summarize(inproc_samples)
            runs_summary.append({
                "decisions": n,
                "http_tick_ms": s_http,
                "inproc_run_all_ms": s_inproc,
            })
            print_row(f"  tick via HTTP  (N={n})", s_http)
            print_row(f"  run_all inproc (N={n})", s_inproc)

    return {
        "description": "Drift-tick latency as a function of WorkingState decision count",
        "loads": loads,
        "runs_per_load": runs_per_load,
        "runs": runs_summary,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--loads", type=int, nargs="+", default=[10, 50, 200, 500])
    p.add_argument("--runs", type=int, default=5)
    args = p.parse_args()

    banner(f"bench_drift_load — loads={args.loads} runs={args.runs}")
    data = run(args.loads, args.runs)
    save_suite("drift_load", data)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
