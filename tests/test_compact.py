"""Compaction snapshot + resume."""
from __future__ import annotations

from anchord import compact, store
from anchord.lineage import build_mission_envelope, make_provenance
from anchord.schemas import MissionSpec, OpenQuestion, Provenanced, WorkingState


def _seed(agent_id: str):
    spec = MissionSpec(
        objective="ship feature",
        subgoals=["draft", "ship"],
        deliverables=[],
    )
    env = build_mission_envelope(spec=spec, created_by="user")
    store.save_mission_envelope(agent_id, env)

    prov = make_provenance(source="user", text="use postgres", confidence=1.0)
    oq = OpenQuestion(
        text="what region?",
        provenance=make_provenance(source="agent", text="what region?", confidence=0.5),
        raised_by="agent",
    )
    state = WorkingState(
        agent_id=agent_id, mission_hash=env.hash,
        decisions=[Provenanced(text="use postgres", provenance=prov)],
        open_questions=[oq],
    )
    store.save_state(state)
    return env, state


def test_snapshot_returns_envelope_with_diff(isolated_anchor_home):
    env, _ = _seed("agent-s1")
    snap = compact.snapshot("agent-s1")
    assert snap is not None
    assert snap.event_class == "compaction"
    assert snap.parent_hash == env.hash
    assert snap.diff is not None
    assert len(snap.diff.decisions_added) == 1
    assert len(snap.diff.open_questions) == 1


def test_snapshot_without_mission_returns_none(isolated_anchor_home):
    snap = compact.snapshot("no-such-agent")
    assert snap is None


def test_resume_payload_digest(isolated_anchor_home):
    _seed("agent-s2")
    snap = compact.snapshot("agent-s2")
    assert snap is not None
    payload = compact.resume("agent-s2")
    assert payload["mission"] is not None
    assert payload["latest_compaction"] is not None
    assert "digest" in payload
    assert "use postgres" in payload["digest"]


def test_snapshot_avoids_duplicate_decisions(isolated_anchor_home):
    _seed("agent-s3")
    snap1 = compact.snapshot("agent-s3")
    snap2 = compact.snapshot("agent-s3")
    assert snap1 is not None and snap2 is not None
    # Second snapshot should have 0 new decisions (already captured in first)
    assert len(snap2.diff.decisions_added) == 0
