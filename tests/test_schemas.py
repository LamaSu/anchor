"""Schema validation round-trips."""
from __future__ import annotations

import json

import pytest

from anchord.schemas import (
    CompactionDiff,
    DriftFinding,
    MissionEnvelope,
    MissionSpec,
    OpenQuestion,
    ProvenanceEnvelope,
    Provenanced,
    ProvTriple,
    WorkingState,
    now_utc,
)


def test_provenance_envelope_defaults():
    p = ProvenanceEnvelope(source="user", hash="sha256:abc")
    assert p.confidence == 1.0
    assert p.parent_hash is None
    assert p.recorded_at is not None


def test_provenance_envelope_confidence_bounds():
    with pytest.raises(Exception):
        ProvenanceEnvelope(source="x", hash="sha256:a", confidence=1.5)


def test_mission_spec_round_trip():
    spec = MissionSpec(
        objective="ship feature",
        subgoals=["draft", "review", "ship"],
        deliverables=[],
        constraints=[],
        assumptions=[],
        success_signals=["tests green"],
    )
    j = json.dumps(spec.model_dump(mode="json"))
    spec2 = MissionSpec.model_validate(json.loads(j))
    assert spec2.objective == spec.objective
    assert spec2.subgoals == spec.subgoals


def test_mission_envelope_with_compaction_diff():
    spec = MissionSpec(objective="x")
    diff = CompactionDiff(
        decisions_added=[],
        decisions_revised=[],
        files_touched=["a.py"],
        open_questions=[],
        mode_transitions=["plan->build"],
        tool_call_count=42,
    )
    env = MissionEnvelope(
        hash="sha256:deadbeef",
        parent_hash=None,
        created_by="hook:pre-compact",
        event_class="compaction",
        spec=spec,
        diff=diff,
    )
    j = json.dumps(env.model_dump(mode="json"))
    env2 = MissionEnvelope.model_validate(json.loads(j))
    assert env2.event_class == "compaction"
    assert env2.diff is not None
    assert env2.diff.tool_call_count == 42


def test_working_state_decision_hashes():
    prov = ProvenanceEnvelope(source="user", hash="sha256:a", confidence=1.0)
    prov2 = ProvenanceEnvelope(source="user", hash="sha256:b", confidence=1.0)
    state = WorkingState(
        agent_id="agent-x",
        mission_hash="sha256:mission",
        decisions=[
            Provenanced(text="d1", provenance=prov),
            Provenanced(text="d2", provenance=prov2),
        ],
    )
    hashes = state.decision_hashes()
    assert "sha256:a" in hashes
    assert "sha256:b" in hashes


def test_drift_finding_requires_provenance():
    pt = ProvTriple(
        agent="anchord:drift-check",
        activity="drift-check:branch_irrelevant",
        generated="finding:x1",
    )
    f = DriftFinding(
        id="F1",
        check="branch_irrelevant",
        severity=0.6,
        message="drift",
        provenance=pt,
    )
    assert f.severity == 0.6
    assert f.suggested_action == "nudge"


def test_open_question_default_status():
    prov = ProvenanceEnvelope(source="agent", hash="sha256:q", confidence=0.6)
    q = OpenQuestion(text="what about X?", provenance=prov, raised_by="agent")
    assert q.status == "open"
