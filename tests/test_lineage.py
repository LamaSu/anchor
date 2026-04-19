"""Hashing + envelope builder determinism."""
from __future__ import annotations

from anchord.lineage import (
    build_mission_envelope,
    canonical_json,
    envelope_hash,
    make_provenance,
    mission_hash,
    provenance_hash,
    walk_chain,
)
from anchord.schemas import CompactionDiff, MissionSpec


def test_canonical_json_is_stable():
    a = {"b": 2, "a": 1, "c": [3, 1, 2]}
    b = {"a": 1, "c": [3, 1, 2], "b": 2}
    assert canonical_json(a) == canonical_json(b)


def test_mission_hash_determinism():
    spec = MissionSpec(objective="x", subgoals=["1", "2"])
    assert mission_hash(spec) == mission_hash(spec)
    assert mission_hash(spec).startswith("sha256:")


def test_mission_hash_varies_with_content():
    a = MissionSpec(objective="A")
    b = MissionSpec(objective="B")
    assert mission_hash(a) != mission_hash(b)


def test_provenance_hash_stable():
    assert provenance_hash("hello") == provenance_hash("hello")
    assert provenance_hash("hello") != provenance_hash("world")


def test_make_provenance_fields():
    p = make_provenance(source="user", text="t", confidence=0.9)
    assert p.source == "user"
    assert p.confidence == 0.9
    assert p.hash.startswith("sha256:")


def test_build_envelope_new_vs_compaction_hashes():
    spec = MissionSpec(objective="x")
    new_env = build_mission_envelope(spec=spec, created_by="user")
    comp_diff = CompactionDiff(tool_call_count=5)
    comp_env = build_mission_envelope(
        spec=spec, created_by="hook:pre-compact",
        parent_hash=new_env.hash, event_class="compaction", diff=comp_diff,
    )
    # Compaction envelopes use envelope_hash (depends on timestamp+diff) → different from plain mission_hash
    assert new_env.hash == mission_hash(spec)
    assert comp_env.hash != new_env.hash
    assert comp_env.parent_hash == new_env.hash
    assert comp_env.event_class == "compaction"


def test_walk_chain():
    spec = MissionSpec(objective="x")
    root = build_mission_envelope(spec=spec, created_by="user")
    amend = build_mission_envelope(
        spec=MissionSpec(objective="x", subgoals=["a"]),
        created_by="user", parent_hash=root.hash, event_class="amend",
    )
    envelopes = {root.hash: root, amend.hash: amend}
    chain = walk_chain(envelopes, leaf_hash=amend.hash)
    assert len(chain) == 2
    assert chain[0].hash == amend.hash
    assert chain[1].hash == root.hash


def test_envelope_hash_strips_hash_field():
    body = {"hash": "should-be-stripped", "x": 1, "y": 2}
    h1 = envelope_hash(body)
    h2 = envelope_hash({"x": 1, "y": 2})
    assert h1 == h2
