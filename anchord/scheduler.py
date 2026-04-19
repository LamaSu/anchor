"""Drift-check scheduling.

Policy:
  - Every Nth tool call (default 25) → run all drift checks
  - Event-class triggers (mode transition, new subgoal, high-risk action keyword) → run immediately
  - Findings are appended to findings.jsonl; high-severity ones surface via /findings/stream (SSE)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from . import store
from .schemas import ToolCallRef, WorkingState


DEFAULT_TICK_EVERY = int(os.environ.get("ANCHOR_TICK_EVERY", "25"))

_HIGH_RISK_KEYWORDS = (
    "rm -rf", "git push --force", "drop table", "delete from",
    "kill -9", "chmod 777", "sudo rm",
)

_EVENT_CLASSES = {
    "mode_transition",
    "new_subgoal",
    "risk_action",
    "constraint_mention",
    "deliverable_mention",
}


@dataclass
class TickDecision:
    should_tick: bool
    reason: str
    event_classes: list[str]


def classify_tool_call(call: ToolCallRef, state: WorkingState) -> list[str]:
    """Classify an incoming tool call into event-classes."""
    classes: list[str] = []
    summary = (call.summary or "").lower()
    tool = (call.tool or "").lower()

    if any(kw in summary for kw in _HIGH_RISK_KEYWORDS):
        classes.append("risk_action")
    if tool in {"taskcreate", "taskupdate"} or "mode" in summary:
        # heuristic: explicit task/mode moves
        classes.append("mode_transition")
    if "subgoal" in summary or "next step" in summary:
        classes.append("new_subgoal")

    return classes


def should_tick(
    state: WorkingState,
    tick_every: int = DEFAULT_TICK_EVERY,
    event_classes: list[str] | None = None,
) -> TickDecision:
    ec = event_classes or []
    if any(c in _EVENT_CLASSES for c in ec):
        return TickDecision(True, f"event-class: {','.join(ec)}", ec)
    n = len(state.tool_calls)
    if n > 0 and n % tick_every == 0:
        return TickDecision(True, f"tool-call tick #{n}", ec)
    return TickDecision(False, "", ec)


def run_if_triggered(
    agent_id: str,
    call: ToolCallRef,
    drift_fn: Callable[[str], list] | None = None,
    tick_every: int = DEFAULT_TICK_EVERY,
) -> list:
    """Record tool call, classify, optionally fire drift checks. Returns findings."""
    state = store.load_state(agent_id)
    if state is None:
        return []

    call.seq = len(state.tool_calls) + 1
    state.tool_calls.append(call)
    store.append_tool_call(agent_id, call)
    store.save_state(state)

    classes = classify_tool_call(call, state)
    decision = should_tick(state, tick_every=tick_every, event_classes=classes)
    if not decision.should_tick or drift_fn is None:
        return []

    return drift_fn(agent_id)
