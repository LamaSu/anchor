"""On-disk persistence."""
from __future__ import annotations

import json

from anchord import store
from anchord.lineage import build_mission_envelope, make_provenance
from anchord.schemas import (
    DriftFinding,
    MissionSpec,
    ProvTriple,
    ToolCallRef,
    WorkingState,
)


def test_agent_dir_creates_layout(isolated_anchor_home):
    d = store.agent_dir("agent-1")
    assert d.exists()
    assert (d / "missions").exists()


def test_save_and_load_mission_envelope(isolated_anchor_home):
    spec = MissionSpec(objective="ship")
    env = build_mission_envelope(spec=spec, created_by="user")
    store.save_mission_envelope("agent-1", env)

    loaded = store.load_current_mission("agent-1")
    assert loaded is not None
    assert loaded.hash == env.hash

    by_hash = store.load_mission_envelope("agent-1", env.hash)
    assert by_hash is not None
    assert by_hash.spec.objective == "ship"


def test_lineage_index_tracks_head(isolated_anchor_home):
    spec1 = MissionSpec(objective="v1")
    env1 = build_mission_envelope(spec=spec1, created_by="user")
    store.save_mission_envelope("agent-2", env1)

    spec2 = MissionSpec(objective="v1", subgoals=["a"])
    env2 = build_mission_envelope(
        spec=spec2, created_by="user", parent_hash=env1.hash, event_class="amend",
    )
    store.save_mission_envelope("agent-2", env2)

    idx_path = store.agent_dir("agent-2") / "lineage.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    assert idx["head"] == env2.hash
    assert env1.hash in idx["envelopes"]
    assert env2.hash in idx["envelopes"]


def test_state_round_trip(isolated_anchor_home):
    state = WorkingState(agent_id="agent-3", mission_hash="sha256:x")
    store.save_state(state)
    loaded = store.load_state("agent-3")
    assert loaded is not None
    assert loaded.agent_id == "agent-3"


def test_ensure_state_creates_when_missing(isolated_anchor_home):
    assert store.load_state("agent-4") is None
    s = store.ensure_state("agent-4", "sha256:m")
    assert s.agent_id == "agent-4"
    again = store.load_state("agent-4")
    assert again is not None and again.agent_id == "agent-4"


def test_append_and_load_findings(isolated_anchor_home):
    pt = ProvTriple(agent="a", activity="drift-check:x", generated="finding:1")
    f = DriftFinding(id="F1", check="branch_irrelevant", severity=0.5, message="m", provenance=pt)
    store.append_finding("agent-5", f)
    out = store.load_findings("agent-5")
    assert len(out) == 1
    assert out[0]["id"] == "F1"


def test_append_tool_call(isolated_anchor_home):
    call = ToolCallRef(seq=1, tool="Read", summary="file.py")
    store.append_tool_call("agent-6", call)
    out = store.load_tool_calls("agent-6")
    assert len(out) == 1
    assert out[0]["tool"] == "Read"


def test_latest_compaction(isolated_anchor_home):
    spec = MissionSpec(objective="x")
    new_env = build_mission_envelope(spec=spec, created_by="user")
    store.save_mission_envelope("agent-7", new_env)
    assert store.latest_compaction("agent-7") is None

    from anchord.schemas import CompactionDiff
    diff = CompactionDiff(tool_call_count=3)
    comp = build_mission_envelope(
        spec=spec, created_by="hook:pre-compact",
        parent_hash=new_env.hash, event_class="compaction", diff=diff,
    )
    store.save_mission_envelope("agent-7", comp)
    latest = store.latest_compaction("agent-7")
    assert latest is not None
    assert latest.hash == comp.hash
