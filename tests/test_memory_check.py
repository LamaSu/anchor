"""Retrieval-time contradiction detection."""
from __future__ import annotations

from anchord import memory_check
from anchord.lineage import make_provenance
from anchord.schemas import Provenanced, WorkingState


def _item(text: str, source: str = "user", confidence: float = 1.0) -> Provenanced:
    return Provenanced(text=text, provenance=make_provenance(source=source, text=text, confidence=confidence))


def test_empty_state_no_warnings():
    item = _item("feature X should be shipped")
    result = memory_check.check(item=item, state=None)
    assert result.warnings == []
    assert result.latency_ms >= 0


def test_direct_contradiction_flagged():
    existing = _item("the api must use postgres")
    state = WorkingState(agent_id="a", mission_hash="sha256:m", decisions=[existing])
    new = _item("the api must not use postgres")
    result = memory_check.check(item=new, state=state)
    assert any(w.type == "contradiction" for w in result.warnings)


def test_duplicate_hash_skipped():
    item = _item("x")
    state = WorkingState(agent_id="a", mission_hash="sha256:m", decisions=[item])
    result = memory_check.check(item=item, state=state)
    # No contradiction with self
    assert all(w.type != "contradiction" for w in result.warnings)


def test_low_confidence_overwrite_flagged():
    user_item = _item(
        "the database should always use postgres version",
        source="user", confidence=1.0,
    )
    inferred = _item(
        "the database should always use postgres",
        source="extract:haiku", confidence=0.6,
    )
    state = WorkingState(agent_id="a", mission_hash="sha256:m", decisions=[user_item])
    result = memory_check.check(item=inferred, state=state)
    types = {w.type for w in result.warnings}
    assert "low_confidence" in types or "contradiction" in types


def test_latency_under_50ms():
    items = [_item(f"decision {i}") for i in range(20)]
    state = WorkingState(agent_id="a", mission_hash="sha256:m", decisions=items)
    new = _item("this is a new decision about something")
    result = memory_check.check(item=new, state=state)
    assert result.latency_ms < 50, f"latency too high: {result.latency_ms}ms"
