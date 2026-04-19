"""Shared bench utilities: timing, percentiles, daemon lifecycle, HTTP helpers.

All benches use this so numbers are comparable and the daemon is managed
consistently. No external deps — stdlib only, same as the daemon itself.
"""
from __future__ import annotations

import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib import error as urlerr
from urllib import request as urlreq


# ---------------------------------------------------------------------------
# Paths + results
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "bench" / "results"


def results_path(name: str = "bench-results.json") -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return RESULTS_DIR / name


def load_results(path: Path | None = None) -> dict[str, Any]:
    p = path or results_path()
    if not p.exists():
        return {"metadata": {}, "suites": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"metadata": {}, "suites": {}}


def save_suite(suite_name: str, data: dict[str, Any], path: Path | None = None) -> None:
    """Merge a suite's results into bench-results.json atomically."""
    p = path or results_path()
    current = load_results(p)
    current.setdefault("suites", {})[suite_name] = data
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def update_metadata(updates: dict[str, Any], path: Path | None = None) -> None:
    p = path or results_path()
    current = load_results(p)
    current.setdefault("metadata", {}).update(updates)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Timing + percentiles
# ---------------------------------------------------------------------------

def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = int(round((p / 100.0) * (len(s) - 1)))
    k = max(0, min(k, len(s) - 1))
    return s[k]


def summarize(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {"n": 0}
    return {
        "n": len(samples),
        "p50_ms": round(percentile(samples, 50), 3),
        "p95_ms": round(percentile(samples, 95), 3),
        "p99_ms": round(percentile(samples, 99), 3),
        "mean_ms": round(statistics.mean(samples), 3),
        "stdev_ms": round(statistics.pstdev(samples), 3) if len(samples) > 1 else 0.0,
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
    }


def print_row(name: str, s: dict[str, float]) -> None:
    if not s or s.get("n", 0) == 0:
        print(f"  {name:<30s}  n=0  (no samples)")
        return
    print(
        f"  {name:<30s}  n={s['n']:<5d}  "
        f"p50={s['p50_ms']:6.2f}ms  p95={s['p95_ms']:6.2f}ms  "
        f"p99={s['p99_ms']:6.2f}ms  mean={s['mean_ms']:6.2f}ms"
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class HTTP:
    def __init__(self, base: str, timeout: float = 10.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def get(self, path: str) -> tuple[int, Any, float]:
        url = self.base + path
        t0 = time.perf_counter()
        try:
            with urlreq.urlopen(url, timeout=self.timeout) as r:
                body = r.read()
                code = r.getcode()
        except urlerr.HTTPError as e:
            body = e.read()
            code = e.code
        dt = (time.perf_counter() - t0) * 1000
        try:
            return code, json.loads(body.decode("utf-8")), dt
        except Exception:
            return code, body, dt

    def post(self, path: str, payload: dict[str, Any]) -> tuple[int, Any, float]:
        url = self.base + path
        req = urlreq.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.perf_counter()
        try:
            with urlreq.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
                code = r.getcode()
        except urlerr.HTTPError as e:
            body = e.read()
            code = e.code
        dt = (time.perf_counter() - t0) * 1000
        try:
            return code, json.loads(body.decode("utf-8")), dt
        except Exception:
            return code, body, dt


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(http: HTTP, tries: int = 50, delay_s: float = 0.1) -> bool:
    for _ in range(tries):
        try:
            code, _, _ = http.get("/healthz")
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(delay_s)
    return False


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------

@contextmanager
def anchord(
    port: int | None = None,
    anchor_home: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> Iterator[tuple[HTTP, subprocess.Popen[bytes]]]:
    """Spawn anchord on a free port with isolated ANCHOR_HOME. Tears down on exit."""
    if port is None:
        port = free_port()
    if anchor_home is None:
        anchor_home = Path(tempfile.mkdtemp(prefix="anchor-bench-"))
    env = os.environ.copy()
    env["ANCHORD_PORT"] = str(port)
    env["ANCHORD_HOST"] = "127.0.0.1"
    env["ANCHOR_HOME"] = str(anchor_home)
    if extra_env:
        env.update(extra_env)

    proc = subprocess.Popen(
        [sys.executable, "-m", "anchord"],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    http = HTTP(f"http://127.0.0.1:{port}")
    try:
        if not wait_ready(http):
            raise RuntimeError(f"anchord did not start on port {port}")
        yield http, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def banner(title: str) -> None:
    print()
    print("=" * 66)
    print(f"  {title}")
    print("=" * 66)
