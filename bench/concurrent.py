"""Bench 5: concurrency — N parallel agents hammering the daemon.

For each (N agents): spawn N threads, each runs a miniature mission flow
(1 new + 10 appends + 10 tool-calls). Measure per-request latency and
overall throughput.

ThreadingHTTPServer handles requests on a thread pool, so this exercises:
  - per-agent file locks (store.py writes via tempfile+os.replace)
  - the _telemetry_cache dict (shared state across handler instances)
  - the scheduler's state-load/save round-trip under write contention
"""
from __future__ import annotations

import argparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from bench._lib import HTTP, anchord, banner, print_row, save_suite, summarize


def _mini_flow(http: HTTP, agent: str, ops_per_agent: int) -> list[float]:
    """1 new + ops/2 appends + ops/2 tool-calls. Returns per-request ms."""
    samples: list[float] = []

    _, _, dt = http.post(
        "/mission/new", {"agent_id": agent, "prompt": f"concurrent bench for {agent}"}
    )
    samples.append(dt)

    half = max(1, ops_per_agent // 2)
    for i in range(half):
        _, _, dt = http.post(
            "/state/append",
            {"agent_id": agent, "kind": "decision", "text": f"d{i}", "source": "agent"},
        )
        samples.append(dt)
    for i in range(half):
        _, _, dt = http.post(
            "/tool-call",
            {"agent_id": agent, "tool": "Read", "ok": True, "summary": f"f{i}"},
        )
        samples.append(dt)
    return samples


def run(agent_counts: list[int], ops_per_agent: int) -> dict:
    runs: list[dict] = []

    with anchord() as (http, _):
        # warmup
        http.get("/healthz")

        for n in agent_counts:
            print(f"\n  spinning up {n} agents × {ops_per_agent} ops each")
            all_samples: list[float] = []
            errs = 0
            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=n) as pool:
                futures = [
                    pool.submit(_mini_flow, http, f"concurrent-{n}-{i}-{int(time.time()*1000)}", ops_per_agent)
                    for i in range(n)
                ]
                for f in as_completed(futures):
                    try:
                        all_samples.extend(f.result())
                    except Exception as e:
                        errs += 1
                        print(f"    worker error: {e}")
            wall = time.perf_counter() - t0
            total_ops = len(all_samples)
            throughput = total_ops / wall if wall > 0 else 0
            s = summarize(all_samples)
            entry = {
                "agents": n,
                "ops_per_agent": ops_per_agent,
                "total_ops": total_ops,
                "wall_s": round(wall, 3),
                "throughput_ops_s": round(throughput, 1),
                "errors": errs,
                "latency": s,
            }
            runs.append(entry)
            print(f"    {total_ops} ops in {wall:.2f}s  "
                  f"throughput={throughput:.1f} ops/s  errors={errs}")
            print_row(f"    latency ({n} agents)", s)

    return {
        "description": "Concurrent agents → throughput + tail latency under contention",
        "runs": runs,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--agents", type=int, nargs="+", default=[1, 4, 16, 32])
    p.add_argument("--ops", type=int, default=20)
    args = p.parse_args()

    banner(f"bench_concurrent — agents={args.agents} ops={args.ops}")
    data = run(args.agents, args.ops)
    save_suite("concurrency", data)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
