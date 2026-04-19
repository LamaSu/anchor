"""Drift tick scheduling policy."""
from __future__ import annotations

from anchord import scheduler, store
from anchord.lineage import build_mission_envelope
from anchord.schemas import MissionSpec, ToolCallRef, WorkingState


def _seed(agent_id: str):
    spec = MissionSpec(objective="x")
    env = build_mission_envelope(spec=spec, created_by="user")
    store.save_mission_envelope(agent_id, env)
    state = WorkingState(agent_id=agent_id, mission_hash=env.hash)
    store.save_state(state)
    return env, state


def test_classify_flags_high_risk():
    call = ToolCallRef(seq=1, tool="Bash", summary="rm -rf /data")
    state = WorkingState(agent_id="x", mission_hash="sha256:y")
    classes = scheduler.classify_tool_call(call, state)
    assert "risk_action" in classes


def test_should_tick_fires_on_event_class():
    state = WorkingState(agent_id="x", mission_hash="sha256:y")
    d = scheduler.should_tick(state, event_classes=["risk_action"])
    assert d.should_tick is True


def test_should_tick_on_nth_call():
    state = WorkingState(
        agent_id="x", mission_hash="sha256:y",
        tool_calls=[ToolCallRef(seq=i+1, tool="Read") for i in range(25)],
    )
    d = scheduler.should_tick(state, tick_every=25)
    assert d.should_tick is True
    assert "tick #25" in d.reason


def test_should_not_tick_under_threshold():
    state = WorkingState(
        agent_id="x", mission_hash="sha256:y",
        tool_calls=[ToolCallRef(seq=i+1, tool="Read") for i in range(5)],
    )
    d = scheduler.should_tick(state, tick_every=25)
    assert d.should_tick is False


def test_run_if_triggered_records_call(isolated_anchor_home):
    _seed("agent-sc1")
    call = ToolCallRef(seq=0, tool="Read", summary="x.py")
    findings = scheduler.run_if_triggered("agent-sc1", call, drift_fn=None)
    assert findings == []
    state = store.load_state("agent-sc1")
    assert state is not None
    assert len(state.tool_calls) == 1
    assert state.tool_calls[0].seq == 1


def test_run_if_triggered_fires_drift_on_risk(isolated_anchor_home):
    _seed("agent-sc2")
    call = ToolCallRef(seq=0, tool="Bash", summary="rm -rf /important")
    sentinel = []

    def fake_drift(agent_id):
        sentinel.append(agent_id)
        return [{"id": "F1"}]

    findings = scheduler.run_if_triggered(
        "agent-sc2", call, drift_fn=fake_drift,
    )
    assert findings == [{"id": "F1"}]
    assert sentinel == ["agent-sc2"]
