"""Compaction-survivable memory diff log.

Pre-compact hook calls `snapshot(agent_id)` which:
  1. Diffs WorkingState against the latest mission baseline (or prior compaction)
  2. Packages the diff into a CompactionDiff
  3. Creates a new MissionEnvelope with event_class="compaction"
  4. Returns the envelope hash (doubles as the compaction pointer)

Post-compact, `resume(agent_id)` emits a rehydration digest that the agent can
re-read to recover decisions, mode transitions, and open questions.
"""
from __future__ import annotations

from typing import Any

from . import store
from .lineage import build_mission_envelope
from .schemas import (
    CompactionDiff,
    MissionEnvelope,
    WorkingState,
)


def _baseline_decision_hashes(agent_id: str) -> set[str]:
    """Union of decision hashes in every prior compaction + mission envelope body."""
    seen: set[str] = set()
    envelopes = store.load_all_envelopes(agent_id)
    for env in envelopes.values():
        if env.event_class == "compaction" and env.diff is not None:
            for d in env.diff.decisions_added:
                seen.add(d.provenance.hash)
            for d in env.diff.decisions_revised:
                seen.add(d.provenance.hash)
    return seen


def _compute_diff(state: WorkingState, agent_id: str) -> CompactionDiff:
    already_captured = _baseline_decision_hashes(agent_id)

    added = [d for d in state.decisions if d.provenance.hash not in already_captured]
    open_q = [q for q in state.open_questions if q.status == "open"]

    return CompactionDiff(
        decisions_added=added,
        decisions_revised=[],
        files_touched=[],  # populated by hook caller if known
        open_questions=open_q,
        mode_transitions=[],  # not tracked in WorkingState yet
        tool_call_count=len(state.tool_calls),
    )


def snapshot(
    agent_id: str,
    created_by: str = "hook:pre-compact",
    files_touched: list[str] | None = None,
    mode_transitions: list[str] | None = None,
) -> MissionEnvelope | None:
    """Snapshot current state into a compaction envelope. Returns the envelope or None if no state."""
    state = store.load_state(agent_id)
    if state is None:
        return None
    current = store.load_current_mission(agent_id)
    if current is None:
        return None

    diff = _compute_diff(state, agent_id)
    if files_touched is not None:
        diff.files_touched = files_touched
    if mode_transitions is not None:
        diff.mode_transitions = mode_transitions

    parent_hash = current.hash
    env = build_mission_envelope(
        spec=current.spec,
        created_by=created_by,
        parent_hash=parent_hash,
        event_class="compaction",
        diff=diff,
    )
    store.save_mission_envelope(agent_id, env)
    return env


def resume(agent_id: str) -> dict[str, Any]:
    """Build a rehydration payload from the most recent compaction, or the current mission."""
    current = store.load_current_mission(agent_id)
    latest_comp = store.latest_compaction(agent_id)
    state = store.load_state(agent_id)

    payload: dict[str, Any] = {
        "agent_id": agent_id,
        "mission": current.model_dump(mode="json") if current is not None else None,
        "latest_compaction": latest_comp.model_dump(mode="json") if latest_comp is not None else None,
        "state": state.model_dump(mode="json") if state is not None else None,
    }

    # Digest: compact, human-readable restoration summary
    lines: list[str] = []
    if current is not None:
        lines.append(f"Mission: {current.spec.objective}")
        if current.spec.subgoals:
            idx = state.current_subgoal_idx if state else 0
            lines.append(f"Current subgoal ({idx}): {current.spec.subgoals[idx] if idx < len(current.spec.subgoals) else '(past last subgoal)'}")
        if current.spec.constraints:
            lines.append(f"Constraints ({len(current.spec.constraints)}):")
            for c in current.spec.constraints[:5]:
                lines.append(f"  - {c.text}")
    if latest_comp is not None and latest_comp.diff is not None:
        d = latest_comp.diff
        lines.append(f"Last compaction decisions: {len(d.decisions_added)} added, {len(d.decisions_revised)} revised")
        for dec in d.decisions_added[:3]:
            lines.append(f"  · {dec.text}")
        if d.open_questions:
            lines.append(f"Open questions ({len(d.open_questions)}):")
            for q in d.open_questions[:3]:
                lines.append(f"  ? {q.text}")
    if state is not None:
        lines.append(f"Mode: {state.mode} · tool calls: {len(state.tool_calls)}")

    payload["digest"] = "\n".join(lines)
    return payload
