"""anchor benchmark harness — skeleton.

Measures: p50/p95/p99 latency of each endpoint + end-to-end tool-call throughput.

Run: `python bench/bench.py [--host 127.0.0.1] [--port 3458] [--iters 500]`
Daemon must be running separately.

This is a SKELETON — wires up the measurement loop but does not yet enforce
SLO thresholds. Extend for regression gates.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from urllib import request as urlreq


def _percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    data = sorted(data)
    k = int(round((p / 100.0) * (len(data) - 1)))
    return data[k]


def _time_get(url: str) -> float:
    t0 = time.perf_counter()
    with urlreq.urlopen(url, timeout=5) as r:
        r.read()
    return (time.perf_counter() - t0) * 1000


def _time_post(url: str, payload: dict) -> float:
    req = urlreq.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    t0 = time.perf_counter()
    with urlreq.urlopen(req, timeout=5) as r:
        r.read()
    return (time.perf_counter() - t0) * 1000


def _run_case(name: str, iters: int, fn):
    samples: list[float] = []
    for _ in range(iters):
        try:
            samples.append(fn())
        except Exception as e:
            print(f"  {name}: error — {e}")
            break
    if not samples:
        return
    print(f"{name:<24s}  n={len(samples):<5d}  "
          f"p50={_percentile(samples, 50):6.2f}ms  "
          f"p95={_percentile(samples, 95):6.2f}ms  "
          f"p99={_percentile(samples, 99):6.2f}ms  "
          f"mean={statistics.mean(samples):6.2f}ms")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=3458)
    p.add_argument("--iters", type=int, default=100)
    args = p.parse_args()
    base = f"http://{args.host}:{args.port}"
    agent = f"bench-{int(time.time())}"

    print(f"anchor bench — base={base} iters={args.iters} agent={agent}")

    _run_case("GET  /healthz", args.iters, lambda: _time_get(f"{base}/healthz"))

    _time_post(f"{base}/mission/new", {"agent_id": agent, "prompt": "bench mission"})
    _run_case(
        "GET  /mission/<id>", args.iters,
        lambda: _time_get(f"{base}/mission/{agent}"),
    )

    _run_case(
        "POST /state/append", args.iters,
        lambda: _time_post(f"{base}/state/append", {
            "agent_id": agent, "kind": "decision", "text": f"decision {time.time()}",
        }),
    )

    _run_case(
        "POST /tool-call", args.iters,
        lambda: _time_post(f"{base}/tool-call", {
            "agent_id": agent, "tool": "Read", "ok": True, "summary": "bench.py",
        }),
    )

    _run_case(
        "POST /memory/check", args.iters,
        lambda: _time_post(f"{base}/memory/check", {
            "agent_id": agent,
            "item": {
                "text": f"bench item {time.time()}",
                "provenance": {"source": "bench", "hash": f"sha256:{time.time()}", "confidence": 0.8},
            },
        }),
    )

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
