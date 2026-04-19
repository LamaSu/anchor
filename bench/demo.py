"""Bitchin-sweet scripted demo.

Runs a 30-turn mission end-to-end in ~3 seconds while printing narration,
then opens bench/report.html in the browser. Designed to run in a terminal
next to the open dashboard.

Usage:
  python -m bench.demo                    # runs + opens dashboard
  python -m bench.demo --no-open          # runs, does not open browser
  python -m bench.demo --fast             # skips 250ms pauses between steps
"""
from __future__ import annotations

import argparse
import time
import webbrowser
from pathlib import Path

from bench._lib import HTTP, REPO_ROOT, anchord, banner
from bench.ablation import SCRIPT, MISSION_PROMPT


# Narration for chunks of the session
NARRATION = {
    0:  "user fires mission: 'Build sensor API, MUST use postgres, ship Friday'",
    2:  "agent logs first decision (user-stated, confidence=1.0)",
    10: "agent attempts contradiction: 'must not use postgres -> switch to sqlite'",
    14: "off-mission investigation starts -- anchor will notice via drift",
    19: "agent attempts contradiction: 'must not use fastapi -> rewrite with flask'",
    22: "another off-mission rabbit hole (README marketing)",
    24: "simulated compaction -- anchor snapshots the diff as an envelope",
    29: "final user-stated decision: ship v0.1",
}


def _sep() -> None:
    print("  " + "-" * 62)


def _print_narration(idx: int, turn_kind: str, text: str) -> None:
    if idx in NARRATION:
        _sep()
        print(f"  >> {NARRATION[idx]}")
    symbol = {
        "decision": ".",
        "contradiction": "!",
        "tool": " ",
        "off_mission": "?",
        "compact": "#",
    }.get(turn_kind, " ")
    preview = text if len(text) <= 60 else text[:57] + "..."
    print(f"  [{idx:02d}] {symbol} {turn_kind:<14s} {preview}")


def _print_mcheck_result(warnings: list[dict]) -> None:
    for w in warnings:
        t = w.get("type", "?")
        sev = w.get("severity", 0)
        sug = w.get("suggestion", "")
        mark = "[!]" if t == "contradiction" else "[i]"
        print(f"        {mark} memory-check {t} sev={sev:.2f}: {sug[:80]}")


def run_scripted(http: HTTP, agent: str, fast: bool) -> None:
    pause = 0.0 if fast else 0.08

    print(f"  agent_id = {agent}")
    print(f"  mission   = \"{MISSION_PROMPT}\"")
    print()

    _, payload, _ = http.post("/mission/new", {"agent_id": agent, "prompt": MISSION_PROMPT})
    env_hash = payload.get("envelope", {}).get("hash") if isinstance(payload, dict) else None
    print(f"  envelope.hash = {env_hash}")
    print(f"  trace_url     = {payload.get('trace_url') if isinstance(payload, dict) else '?'}")
    print()

    contradictions_caught = 0
    contradictions_injected = 0

    for idx, turn in enumerate(SCRIPT):
        _print_narration(idx, turn.kind, turn.text)

        if turn.kind == "decision":
            http.post("/state/append", {
                "agent_id": agent, "kind": "decision", "text": turn.text,
                "source": turn.source, "confidence": turn.confidence,
            })

        elif turn.kind == "contradiction":
            contradictions_injected += 1
            _, mc_payload, _ = http.post("/memory/check", {
                "agent_id": agent,
                "item": {
                    "text": turn.text,
                    "provenance": {"source": turn.source, "hash": f"sha256:demo-{idx:04x}",
                                   "confidence": turn.confidence},
                },
            })
            warnings = mc_payload.get("warnings", []) if isinstance(mc_payload, dict) else []
            if any(w.get("type") == "contradiction" for w in warnings):
                contradictions_caught += 1
            _print_mcheck_result(warnings)
            http.post("/state/append", {
                "agent_id": agent, "kind": "decision", "text": turn.text,
                "source": turn.source, "confidence": turn.confidence,
            })

        elif turn.kind == "tool":
            http.post("/tool-call", {
                "agent_id": agent, "tool": turn.text.split()[0],
                "ok": True, "summary": turn.text,
            })

        elif turn.kind == "off_mission":
            http.post("/state/append", {
                "agent_id": agent, "kind": "decision", "text": turn.text,
                "source": "agent", "confidence": 0.6,
            })
            http.post("/tool-call", {
                "agent_id": agent, "tool": "WebSearch", "ok": True, "summary": turn.text,
            })

        elif turn.kind == "compact":
            _, snap_payload, _ = http.post("/compact/snapshot", {"agent_id": agent})
            if isinstance(snap_payload, dict):
                diff = snap_payload.get("diff") or {}
                added = len(diff.get("decisions_added", []))
                open_q = len(diff.get("open_questions", []))
                print(f"        compaction envelope {snap_payload.get('hash','?')[:18]}... "
                      f"diff: +{added} decisions, {open_q} open questions")

        if pause:
            time.sleep(pause)

    _sep()
    print("  session complete. pulling final reports from anchord...")
    print()

    _, findings_payload, _ = http.get(f"/findings/{agent}")
    findings = findings_payload.get("findings", []) if isinstance(findings_payload, dict) else []
    _, resume_payload, _ = http.get(f"/compact/resume/{agent}")
    digest = resume_payload.get("digest", "") if isinstance(resume_payload, dict) else ""

    print(f"  contradictions caught:   {contradictions_caught}/{contradictions_injected}")
    print(f"  drift findings recorded: {len(findings)}")
    print()
    print("  -- resume digest --")
    for line in digest.splitlines():
        print(f"    {line}")
    print()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--no-open", action="store_true",
                   help="Do not auto-open bench/report.html")
    p.add_argument("--fast", action="store_true",
                   help="Skip inter-turn delays (for CI / replays)")
    args = p.parse_args()

    banner("anchor · scripted demo")

    with anchord(extra_env={"ANCHOR_TICK_EVERY": "5"}) as (http, _):
        agent = f"demo-{int(time.time())}"
        run_scripted(http, agent, args.fast)

    report = REPO_ROOT / "bench" / "report.html"
    if report.exists() and not args.no_open:
        print(f"  opening {report}")
        webbrowser.open(report.as_uri())
    elif not report.exists():
        print("  (bench/report.html not found — run `python -m bench.report` after `python -m bench.main`)")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
