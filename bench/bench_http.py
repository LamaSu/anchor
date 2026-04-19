"""Bench 1: basic HTTP endpoint latency.

Hits each endpoint N times, records p50/p95/p99.
"""
from __future__ import annotations

import argparse
import time

from bench._lib import HTTP, anchord, banner, print_row, save_suite, summarize


def run(http: HTTP, iters: int) -> dict:
    agent = f"bench-http-{int(time.time()*1000)}"
    cases: list[dict] = []

    # Warmup
    http.get("/healthz")

    # --- GET /healthz ---
    samples: list[float] = []
    for _ in range(iters):
        _, _, dt = http.get("/healthz")
        samples.append(dt)
    s = summarize(samples) | {"name": "GET /healthz"}
    cases.append(s)
    print_row("GET /healthz", s)

    # Seed a mission
    http.post("/mission/new", {"agent_id": agent, "prompt": "bench-http mission"})

    # --- GET /mission/<id> ---
    samples = []
    for _ in range(iters):
        _, _, dt = http.get(f"/mission/{agent}")
        samples.append(dt)
    s = summarize(samples) | {"name": "GET /mission/<id>"}
    cases.append(s)
    print_row("GET /mission/<id>", s)

    # --- POST /state/append ---
    samples = []
    for i in range(iters):
        _, _, dt = http.post(
            "/state/append",
            {"agent_id": agent, "kind": "decision", "text": f"decision {i} {time.time()}"},
        )
        samples.append(dt)
    s = summarize(samples) | {"name": "POST /state/append"}
    cases.append(s)
    print_row("POST /state/append", s)

    # --- POST /tool-call ---
    samples = []
    for i in range(iters):
        _, _, dt = http.post(
            "/tool-call",
            {"agent_id": agent, "tool": "Read", "ok": True, "summary": f"bench {i}"},
        )
        samples.append(dt)
    s = summarize(samples) | {"name": "POST /tool-call"}
    cases.append(s)
    print_row("POST /tool-call", s)

    # --- POST /memory/check ---
    samples = []
    for i in range(iters):
        _, _, dt = http.post(
            "/memory/check",
            {
                "agent_id": agent,
                "item": {
                    "text": f"bench memcheck item {i}",
                    "provenance": {"source": "bench", "hash": f"sha256:{i:08x}", "confidence": 0.8},
                },
            },
        )
        samples.append(dt)
    s = summarize(samples) | {"name": "POST /memory/check"}
    cases.append(s)
    print_row("POST /memory/check", s)

    # --- POST /compact/snapshot ---
    samples = []
    for i in range(min(iters, 20)):  # heavier, cap lower
        _, _, dt = http.post("/compact/snapshot", {"agent_id": agent})
        samples.append(dt)
    s = summarize(samples) | {"name": "POST /compact/snapshot"}
    cases.append(s)
    print_row("POST /compact/snapshot", s)

    # --- GET /compact/resume/<id> ---
    samples = []
    for _ in range(iters):
        _, _, dt = http.get(f"/compact/resume/{agent}")
        samples.append(dt)
    s = summarize(samples) | {"name": "GET /compact/resume"}
    cases.append(s)
    print_row("GET /compact/resume", s)

    return {"description": "Per-endpoint latency, single client", "cases": cases, "iters": iters}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--iters", type=int, default=200)
    args = p.parse_args()

    banner(f"bench_http — {args.iters} iters per case")
    with anchord() as (http, _):
        data = run(http, args.iters)
        save_suite("http", data)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
