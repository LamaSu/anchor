"""Bench 6: ablation — with vs. without anchor.

Runs the SAME scripted 30-turn mission twice:
  (A) with anchor:    anchord daemon wired in
  (B) without anchor: dumb in-memory list + naive compaction ("keep last 500 chars")

Metrics:
  contradictions_caught / contradictions_injected
  drift_warnings_fired / off_mission_turns
  post_compact_fidelity — fraction of original mission constraints still
                          recoverable after compaction

The scripted mission is deterministic so the comparison is apples-to-apples.

Key design note: fidelity for (B) is measured by string-matching — count how
many of the original constraint tokens survive in the naive summary. This is
a generous lower bound (real chat-history compaction loses more).
"""
from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass, field
from typing import Any

from bench._lib import HTTP, anchord, banner, save_suite


# ---------------------------------------------------------------------------
# The scripted 30-turn mission
# ---------------------------------------------------------------------------

MISSION_PROMPT = (
    "Build a sensor API with tests. "
    "MUST use postgres. MUST NOT use sqlite. MUST tolerate offline mode. "
    "Ship by Friday. Tests must pass."
)

MISSION_CONSTRAINTS = [
    "MUST use postgres",
    "MUST NOT use sqlite",
    "MUST tolerate offline mode",
    "Ship by Friday",
    "Tests must pass",
]


@dataclass
class Turn:
    kind: str                      # "decision" | "tool" | "compact" | "off_mission" | "contradiction"
    text: str
    contradicts: str | None = None  # earlier turn index's text if this contradicts it
    is_off_mission: bool = False
    source: str = "agent"
    confidence: float = 0.8


# Scripted 30-turn session:
#   - 5 in-scope decisions
#   - 2 explicit contradictions (against prior decisions)
#   - 3 off-mission distractions (nothing to do with the mission)
#   - 20 tool calls sprinkled in
#   - A compact at turn 25, then 5 more turns
# Contradictions are phrased with explicit negation markers (don't / not / no)
# so the lexical memory_check can catch them without needing intention-engine.
SCRIPT: list[Turn] = [
    Turn("decision", "The api must use fastapi for the HTTP layer", source="user", confidence=1.0),
    Turn("tool",     "Read api/server.py"),
    Turn("decision", "The database must use postgres version 15", source="user", confidence=1.0),
    Turn("tool",     "Edit api/server.py"),
    Turn("decision", "Add retry-with-backoff for offline tolerance"),
    Turn("tool",     "Read api/db.py"),
    Turn("tool",     "Edit api/db.py"),
    Turn("off_mission", "Research whether to switch CI from github actions to gitlab ci", is_off_mission=True),
    Turn("tool",     "WebSearch gitlab ci features"),
    Turn("decision", "Write pytest integration tests for /sensors endpoint"),
    # Contradiction #1 -- explicit negation of the earlier postgres decision
    Turn("contradiction", "The database must not use postgres; switch to sqlite instead",
         contradicts="The database must use postgres version 15",
         source="agent", confidence=0.7),
    Turn("tool",     "Edit requirements.txt"),
    Turn("tool",     "Read tests/test_sensors.py"),
    Turn("decision", "Add Prometheus metrics on /sensors/health"),
    Turn("off_mission", "Investigate migrating to NATS for eventing", is_off_mission=True),
    Turn("tool",     "Read docker-compose.yml"),
    Turn("tool",     "Edit docker-compose.yml"),
    Turn("decision", "Pin pydantic version to 2.9.x"),
    Turn("tool",     "Read pyproject.toml"),
    # Contradiction #2 -- explicit negation of the fastapi decision
    Turn("contradiction", "The api must not use fastapi; rewrite the HTTP layer with flask",
         contradicts="The api must use fastapi for the HTTP layer",
         source="agent", confidence=0.6),
    Turn("tool",     "Read api/routes.py"),
    Turn("tool",     "Read api/db.py"),
    Turn("off_mission", "Rewrite README marketing section", is_off_mission=True),
    Turn("tool",     "Edit README.md"),
    Turn("compact",  "simulated compaction event"),
    Turn("tool",     "Read api/server.py"),
    Turn("decision", "Add rate-limit middleware"),
    Turn("tool",     "Edit api/middleware.py"),
    Turn("tool",     "Read tests/test_rate_limit.py"),
    Turn("decision", "Ship v0.1 with postgres + fastapi + tests passing", source="user", confidence=1.0),
]


# ---------------------------------------------------------------------------
# Scorecards
# ---------------------------------------------------------------------------

@dataclass
class Scorecard:
    mode: str
    contradictions_injected: int = 0
    contradictions_caught: int = 0
    off_mission_turns: int = 0
    drift_warnings_fired: int = 0
    constraints_total: int = len(MISSION_CONSTRAINTS)
    constraints_recovered: int = 0
    post_compact_fidelity: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "contradictions_injected": self.contradictions_injected,
            "contradictions_caught": self.contradictions_caught,
            "contradiction_catch_rate": round(
                self.contradictions_caught / max(1, self.contradictions_injected), 3
            ),
            "off_mission_turns": self.off_mission_turns,
            "drift_warnings_fired": self.drift_warnings_fired,
            "drift_fire_rate": round(
                self.drift_warnings_fired / max(1, self.off_mission_turns), 3
            ),
            "constraints_total": self.constraints_total,
            "constraints_recovered": self.constraints_recovered,
            "post_compact_fidelity": round(self.post_compact_fidelity, 3),
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Run WITH anchor
# ---------------------------------------------------------------------------

def run_with_anchor(http: HTTP) -> Scorecard:
    agent = f"ablation-with-{int(time.time()*1000)}"
    sc = Scorecard(mode="with_anchor")

    http.post("/mission/new", {"agent_id": agent, "prompt": MISSION_PROMPT})

    prior_texts: list[dict[str, Any]] = []

    for idx, turn in enumerate(SCRIPT):
        if turn.kind == "decision":
            prior_texts.append({"text": turn.text, "source": turn.source, "confidence": turn.confidence})
            http.post("/state/append", {
                "agent_id": agent,
                "kind": "decision",
                "text": turn.text,
                "source": turn.source,
                "confidence": turn.confidence,
            })

        elif turn.kind == "contradiction":
            sc.contradictions_injected += 1
            # Call memory/check BEFORE storing — same pattern the agent should use
            _, payload, _ = http.post("/memory/check", {
                "agent_id": agent,
                "item": {
                    "text": turn.text,
                    "provenance": {
                        "source": turn.source,
                        "hash": f"sha256:abl-{idx:04x}",
                        "confidence": turn.confidence,
                    },
                },
            })
            warnings = payload.get("warnings", []) if isinstance(payload, dict) else []
            if any(w.get("type") == "contradiction" for w in warnings):
                sc.contradictions_caught += 1
                sc.notes.append(f"turn {idx}: contradiction caught → {turn.text[:60]}...")
            # Then append (what a "sloppy" agent would do anyway)
            http.post("/state/append", {
                "agent_id": agent, "kind": "decision",
                "text": turn.text, "source": turn.source, "confidence": turn.confidence,
            })

        elif turn.kind == "tool":
            http.post("/tool-call", {
                "agent_id": agent, "tool": turn.text.split()[0],
                "ok": True, "summary": turn.text,
            })

        elif turn.kind == "off_mission":
            sc.off_mission_turns += 1
            # Append it as a decision — pure off-mission noise
            http.post("/state/append", {
                "agent_id": agent, "kind": "decision",
                "text": turn.text, "source": turn.source, "confidence": 0.6,
            })
            # Also log a tool-call that smells off-mission
            http.post("/tool-call", {
                "agent_id": agent, "tool": "WebSearch",
                "ok": True, "summary": turn.text,
            })

        elif turn.kind == "compact":
            http.post("/compact/snapshot", {"agent_id": agent})
            sc.notes.append(f"turn {idx}: compaction fired")

    # Count drift findings fired during the session
    _, findings_payload, _ = http.get(f"/findings/{agent}")
    if isinstance(findings_payload, dict):
        findings = findings_payload.get("findings", [])
        sc.drift_warnings_fired = len(findings)
        branchless = [f for f in findings if f.get("check") == "branch_irrelevant"]
        if branchless:
            sc.notes.append(f"branch_irrelevant findings: {len(branchless)}")

    # Post-compaction fidelity: call /compact/resume and count recovered constraints
    _, resume_payload, _ = http.get(f"/compact/resume/{agent}")
    digest = ""
    if isinstance(resume_payload, dict):
        digest = resume_payload.get("digest", "")
        mission = resume_payload.get("mission") or {}
        spec = mission.get("spec", {}) if isinstance(mission, dict) else {}
        constraints = spec.get("constraints", [])
        # anchor stores constraints structured; fidelity = structured presence
        constraint_texts = [c.get("text", "") for c in constraints]
        # count how many of OUR constraint strings map to what's recovered
        for cons in MISSION_CONSTRAINTS:
            hay = " ".join(constraint_texts) + " " + digest
            if _fuzzy_contains(cons, hay):
                sc.constraints_recovered += 1

    sc.post_compact_fidelity = sc.constraints_recovered / max(1, sc.constraints_total)
    return sc


# ---------------------------------------------------------------------------
# Run WITHOUT anchor (naive baseline)
# ---------------------------------------------------------------------------

def run_without_anchor() -> Scorecard:
    """Dumb agent: flat list of decisions + naive 'last 500 chars' compaction.

    This models what an ephemeral-memory agent actually has after compaction —
    a truncated chat-history summary, NOT the raw mission prompt (which has
    been rotated out of context). We explicitly do NOT keep MISSION_PROMPT
    around; that would be unfair to anchor.
    """
    sc = Scorecard(mode="without_anchor")
    log: list[str] = []

    for idx, turn in enumerate(SCRIPT):
        if turn.kind == "decision":
            log.append(f"[decision] {turn.text}")

        elif turn.kind == "contradiction":
            sc.contradictions_injected += 1
            # No memory check — just append.
            log.append(f"[decision] {turn.text}")
            # Catch rate is 0 for this mode by construction.

        elif turn.kind == "tool":
            log.append(f"[tool] {turn.text}")

        elif turn.kind == "off_mission":
            sc.off_mission_turns += 1
            log.append(f"[decision] {turn.text}")
            # No drift check — fires = 0.

        elif turn.kind == "compact":
            # Naive compaction: "last 500 chars of the running log".
            # This is what agents WITHOUT structured memory end up with after
            # chat history is truncated; older turns are lost.
            joined = "\n".join(log)
            compact_summary = joined[-500:]
            log = [compact_summary]
            sc.notes.append(f"turn {idx}: naive compaction to last 500 chars "
                            f"(dropped {len(joined) - len(compact_summary)} chars)")

    # Post-compaction fidelity: fuzzy-match each original mission constraint
    # against whatever survives in the post-compact log. No cheating — we do
    # NOT include the raw mission prompt, because a real ephemeral-memory
    # agent doesn't have it anymore.
    full = " ".join(log)
    for cons in MISSION_CONSTRAINTS:
        if _fuzzy_contains(cons, full):
            sc.constraints_recovered += 1
    sc.post_compact_fidelity = sc.constraints_recovered / max(1, sc.constraints_total)
    return sc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-zA-Z0-9]+")


def _fuzzy_contains(needle: str, haystack: str, min_overlap: float = 0.6) -> bool:
    """True if ≥ min_overlap of the needle's content-word tokens appear in haystack."""
    needle_toks = {t.lower() for t in _WORD_RE.findall(needle) if len(t) > 2}
    hay_toks = {t.lower() for t in _WORD_RE.findall(haystack)}
    if not needle_toks:
        return False
    return len(needle_toks & hay_toks) / len(needle_toks) >= min_overlap


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _format_scorecard(sc: Scorecard) -> str:
    d = sc.to_dict()
    return (
        f"  contradictions: {d['contradictions_caught']}/{d['contradictions_injected']} "
        f"({d['contradiction_catch_rate']*100:.0f}%)\n"
        f"  drift firings:  {d['drift_warnings_fired']} across {d['off_mission_turns']} off-mission turns\n"
        f"  constraints recovered post-compact: "
        f"{d['constraints_recovered']}/{d['constraints_total']} "
        f"({d['post_compact_fidelity']*100:.0f}%)"
    )


def run() -> dict:
    print("  running WITHOUT anchor (naive baseline)...")
    without = run_without_anchor()
    print(_format_scorecard(without))

    print("\n  running WITH anchor...")
    # Lower the drift-tick threshold so off-mission tool calls trigger
    # branch_irrelevant findings in a realistic ~30-turn demo (default 25
    # would not fire; the "tick every 5 tool-calls" here is purely a demo
    # knob and doesn't affect the baseline).
    with anchord(extra_env={"ANCHOR_TICK_EVERY": "5"}) as (http, _):
        with_a = run_with_anchor(http)
    print(_format_scorecard(with_a))

    delta = {
        "contradiction_catch_rate_delta": round(
            with_a.to_dict()["contradiction_catch_rate"]
            - without.to_dict()["contradiction_catch_rate"], 3
        ),
        "drift_warning_delta": with_a.drift_warnings_fired - without.drift_warnings_fired,
        "fidelity_delta": round(
            with_a.post_compact_fidelity - without.post_compact_fidelity, 3
        ),
    }
    print(f"\n  deltas (with - without): {delta}")

    return {
        "description": "Mission-adherence comparison on an identical 30-turn scripted session",
        "script_length": len(SCRIPT),
        "mission_prompt": MISSION_PROMPT,
        "mission_constraints": MISSION_CONSTRAINTS,
        "without_anchor": without.to_dict(),
        "with_anchor": with_a.to_dict(),
        "deltas": delta,
    }


def main() -> int:
    _ = argparse.ArgumentParser().parse_args()  # no args yet; reserved
    banner("bench_ablation — with vs without anchor")
    data = run()
    save_suite("ablation", data)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
