"""Bench 2: end-to-end mission lifecycle.

A single "mission flow" = new → 25 state-appends + 25 tool-calls → snapshot → resume.
This is the real-world shape of an agent session, so we time each step and the
total wallclock per flow. Runs N flows.
"""
from __future__ import annotations

import argparse
import time

from bench._lib import HTTP, anchord, banner, print_row, save_suite, summarize


def one_flow(http: HTTP, agent: str) -> dict[str, float]:
    """Run one full mission lifecycle, return per-step ms + total."""
    step: dict[str, float] = {}

    # new mission
    _, _, dt = http.post(
        "/mission/new",
        {
            "agent_id": agent,
            "prompt": (
                "Build a sensor API with tests. MUST use postgres. "
                "MUST NOT ship before tests pass. Must tolerate offline mode. "
                "Ship by Friday."
            ),
        },
    )
    step["new"] = dt
    total = dt

    # 25 state-appends
    append_samples: list[float] = []
    for i in range(25):
        _, _, dt = http.post(
            "/state/append",
            {
                "agent_id": agent,
                "kind": "decision" if i % 3 else "assumption",
                "text": f"decision-{i}: choose {'postgres' if i % 2 else 'fastapi'} for component {i}",
                "source": "user" if i % 4 == 0 else "agent",
                "confidence": 1.0 if i % 4 == 0 else 0.8,
            },
        )
        append_samples.append(dt)
        total += dt
    step["append_p95"] = summarize(append_samples)["p95_ms"]
    step["append_mean"] = summarize(append_samples)["mean_ms"]

    # 25 tool-calls (triggers drift tick at #25)
    tool_samples: list[float] = []
    for i in range(25):
        _, _, dt = http.post(
            "/tool-call",
            {
                "agent_id": agent,
                "tool": "Read" if i % 2 else "Edit",
                "ok": True,
                "summary": f"file{i}.py",
            },
        )
        tool_samples.append(dt)
        total += dt
    step["tool_p95"] = summarize(tool_samples)["p95_ms"]
    step["tool_mean"] = summarize(tool_samples)["mean_ms"]
    step["tool_at_25"] = tool_samples[-1]  # the tick-triggering one

    # snapshot
    _, _, dt = http.post("/compact/snapshot", {"agent_id": agent})
    step["snapshot"] = dt
    total += dt

    # resume
    _, _, dt = http.get(f"/compact/resume/{agent}")
    step["resume"] = dt
    total += dt

    step["total"] = total
    return step


def run(http: HTTP, flows: int) -> dict:
    # Warmup
    http.get("/healthz")

    per_step: dict[str, list[float]] = {}
    totals: list[float] = []

    for i in range(flows):
        agent = f"lifecycle-{int(time.time()*1000)}-{i}"
        s = one_flow(http, agent)
        totals.append(s["total"])
        for k, v in s.items():
            per_step.setdefault(k, []).append(v)
        print(f"  flow {i+1}/{flows}: total={s['total']:7.2f}ms  "
              f"new={s['new']:5.2f}  snap={s['snapshot']:5.2f}  resume={s['resume']:5.2f}")

    summary = {
        "flows": flows,
        "total_wallclock": summarize(totals),
        "per_step": {k: summarize(v) for k, v in per_step.items()},
    }

    print()
    print("  -- per-step summary --")
    for k, s in summary["per_step"].items():
        if k == "total":
            continue
        print_row(k, s)

    return {
        "description": f"End-to-end mission flow (new + 25 appends + 25 tool-calls + snapshot + resume), {flows} runs",
        **summary,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--flows", type=int, default=10)
    args = p.parse_args()

    banner(f"bench_lifecycle — {args.flows} end-to-end mission flows")
    with anchord() as (http, _):
        data = run(http, args.flows)
        save_suite("lifecycle", data)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
