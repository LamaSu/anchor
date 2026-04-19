"""Tests for anchord.drift.

Each test seeds a mission + state + tool-calls through `store`, then invokes
either `run_all(agent_id)` or one check directly, and asserts the expected
DriftFinding(s) are produced.

conftest.py autouse-isolates ANCHOR_HOME to a tmp dir per test.
"""
from __future__ import annotations

import os
import sys

import pytest

# Ensure we can import anchord from the repo root when running pytest from cwd.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from anchord import drift, store  # noqa: E402
from anchord.lineage import build_mission_envelope, make_provenance  # noqa: E402
from anchord.schemas import (  # noqa: E402
    Deliverable,
    DriftFinding,
    MissionSpec,
    Provenanced,
    ToolCallRef,
    WorkingState,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

AGENT = "test-agent"


def _seed(
    *,
    objective: str = "Port intention-engine storage to TiDB",
    subgoals: list[str] | None = None,
    deliverables: list[Deliverable] | None = None,
    constraints: list[str] | None = None,
    assumptions: list[Provenanced] | None = None,
    decisions: list[Provenanced] | None = None,
    tool_calls: list[ToolCallRef] | None = None,
    current_subgoal_idx: int = 0,
) -> tuple[str, int]:
    """Seed the store with a mission + state + tool calls. Returns (agent_id, mission_hash_short)."""
    constraints = constraints or []
    constraint_items = [
        Provenanced(text=c, provenance=make_provenance("user", c, confidence=1.0))
        for c in constraints
    ]

    spec = MissionSpec(
        objective=objective,
        subgoals=subgoals or [],
        deliverables=deliverables or [],
        constraints=constraint_items,
        assumptions=[],
        success_signals=[],
    )
    env = build_mission_envelope(spec=spec, created_by="user", event_class="new")
    store.save_mission_envelope(AGENT, env)

    state = WorkingState(
        agent_id=AGENT,
        mission_hash=env.hash,
        current_subgoal_idx=current_subgoal_idx,
        decisions=decisions or [],
        running_assumptions=assumptions or [],
        tool_calls=tool_calls or [],
    )
    store.save_state(state)

    for tc in (tool_calls or []):
        store.append_tool_call(AGENT, tc)

    return AGENT, 0


def _make_tool_call(seq: int, tool: str = "Bash", summary: str = "") -> ToolCallRef:
    return ToolCallRef(seq=seq, tool=tool, summary=summary)


# ---------------------------------------------------------------------------
# run_all
# ---------------------------------------------------------------------------

def test_run_all_returns_list_when_empty():
    """run_all must always return a list (never raise) even when nothing is seeded."""
    findings = drift.run_all("no-such-agent-12345")
    assert isinstance(findings, list)
    assert findings == []


def test_run_all_returns_list_with_seeded_state():
    """With a seeded benign mission (no triggers), run_all returns an empty list cleanly."""
    tool_calls = [_make_tool_call(i, "Read", f"Read file {i}") for i in range(1, 4)]
    _seed(tool_calls=tool_calls)
    findings = drift.run_all(AGENT)
    assert isinstance(findings, list)
    # No triggers hit with only 3 calls + no constraints/deliverables/etc.
    for f in findings:
        assert isinstance(f, DriftFinding)


# ---------------------------------------------------------------------------
# Check 3: constraint_drop (rm -rf)
# ---------------------------------------------------------------------------

def test_constraint_drop_fires_on_rm_rf():
    """A Bash call containing 'rm -rf' should fire constraint_drop at severity 0.95 when
    a constraint mentions no-rm-rf / deletion.
    """
    calls = [
        _make_tool_call(1, "Read", "Read README.md"),
        _make_tool_call(
            2, "Bash",
            "run: rm -rf node_modules/ to clean up",
        ),
    ]
    _seed(
        constraints=["no rm -rf or destructive deletions allowed"],
        tool_calls=calls,
    )

    findings = drift.run_all(AGENT)
    cd = [f for f in findings if f.check == "constraint_drop"]
    assert cd, f"expected constraint_drop finding; got: {[f.check for f in findings]}"
    f = cd[0]
    assert f.severity == pytest.approx(0.95)
    assert f.suggested_action == "halt"
    assert "rm -rf" in f.evidence.get("matched_keyword", "")
    # Provenance is attached
    assert f.provenance.agent == "anchord:drift-check"
    assert f.provenance.activity.startswith("drift-check:constraint_drop")


def test_constraint_drop_does_not_fire_without_constraints():
    """If the mission has no constraints, rm -rf alone must not produce a constraint_drop."""
    calls = [_make_tool_call(1, "Bash", "rm -rf something")]
    _seed(constraints=[], tool_calls=calls)

    findings = drift.run_all(AGENT)
    cd = [f for f in findings if f.check == "constraint_drop"]
    assert cd == []


def test_constraint_drop_direct_call():
    """Calling check_constraint_drop directly should produce the same outcome."""
    calls = [_make_tool_call(1, "Bash", "sudo rm the_folder")]
    _seed(constraints=["do not use sudo rm"], tool_calls=calls)
    mission = store.load_current_mission(AGENT)
    state = store.load_state(AGENT)
    findings = drift.check_constraint_drop(mission, state, calls)
    assert findings
    assert findings[0].severity == pytest.approx(0.95)
    assert findings[0].suggested_action == "halt"


# ---------------------------------------------------------------------------
# Check 1: subgoal_detached
# ---------------------------------------------------------------------------

def test_subgoal_detached_does_not_fire_with_fewer_than_5_calls():
    """Fewer than 5 tool calls = not enough signal, check must stay silent."""
    calls = [
        _make_tool_call(1, "Read", "completely unrelated banana milkshake content"),
        _make_tool_call(2, "Read", "more unrelated zebra stripe analysis"),
        _make_tool_call(3, "Read", "cucumber salad recipe v2"),
    ]
    _seed(
        subgoals=["Write TiDB schema for EnrichedNode"],
        tool_calls=calls,
    )
    findings = drift.run_all(AGENT)
    sd = [f for f in findings if f.check == "subgoal_detached"]
    assert sd == [], f"should not fire with only 3 calls, got: {sd}"


def test_subgoal_detached_fires_on_5_unrelated_calls():
    """5+ consecutive tool calls with jaccard < 0.15 to current subgoal → fire."""
    subgoal = "Write TiDB schema for EnrichedNode with vector fields"
    calls = [
        _make_tool_call(i, "Read",
                        f"Reading {x} recipes and {y} trivia")
        for i, (x, y) in enumerate(
            [
                ("banana", "zebra"),
                ("cucumber", "giraffe"),
                ("tomato", "elephant"),
                ("carrot", "tiger"),
                ("potato", "panda"),
            ],
            start=1,
        )
    ]
    _seed(
        subgoals=[subgoal],
        tool_calls=calls,
    )
    findings = drift.run_all(AGENT)
    sd = [f for f in findings if f.check == "subgoal_detached"]
    assert sd, f"expected subgoal_detached, got: {[f.check for f in findings]}"
    assert sd[0].severity == pytest.approx(0.7)
    assert sd[0].suggested_action == "nudge"


# ---------------------------------------------------------------------------
# Check 2: deliverable_loss
# ---------------------------------------------------------------------------

def test_deliverable_loss_fires_at_20_calls_with_no_mention():
    """20+ tool calls AND zero references to any deliverable name → fire, severity 0.8."""
    calls = [
        _make_tool_call(i, "Read", f"Read file-{i}.md")
        for i in range(1, 22)
    ]
    _seed(
        deliverables=[
            Deliverable(
                name="TiDBStorageMigration",
                acceptance="pytest passes with TIDB_HOST set",
            ),
        ],
        tool_calls=calls,
    )
    findings = drift.run_all(AGENT)
    dl = [f for f in findings if f.check == "deliverable_loss"]
    assert dl, f"expected deliverable_loss, got: {[f.check for f in findings]}"
    assert dl[0].severity == pytest.approx(0.8)
    assert dl[0].suggested_action == "rehydrate"
    assert "TiDBStorageMigration" in dl[0].evidence.get("missing_deliverables", [])


def test_deliverable_loss_does_not_fire_under_20_calls():
    """Fewer than 20 tool calls = not enough evidence yet."""
    calls = [
        _make_tool_call(i, "Read", f"Read file-{i}.md")
        for i in range(1, 10)
    ]
    _seed(
        deliverables=[
            Deliverable(name="SomeFarOutDeliverable", acceptance="does not match"),
        ],
        tool_calls=calls,
    )
    findings = drift.run_all(AGENT)
    dl = [f for f in findings if f.check == "deliverable_loss"]
    assert dl == []


def test_deliverable_loss_satisfied_by_any_token_mention():
    """If any tool call mentions a token of the deliverable name, the check is satisfied."""
    calls = [
        _make_tool_call(i, "Read", f"Read something about TiDBStorageMigration pattern")
        for i in range(1, 22)
    ]
    _seed(
        deliverables=[
            Deliverable(
                name="TiDBStorageMigration",
                acceptance="pytest passes with TIDB_HOST set",
            ),
        ],
        tool_calls=calls,
    )
    findings = drift.run_all(AGENT)
    dl = [f for f in findings if f.check == "deliverable_loss"]
    assert dl == [], f"should not fire — deliverable is mentioned, got: {dl}"


# ---------------------------------------------------------------------------
# Check 4: assumption_conflict
# ---------------------------------------------------------------------------

def test_assumption_conflict_fires_on_contradiction():
    """A running assumption that contradicts a mission constraint (via opposing negation)
    should fire assumption_conflict."""
    # Mission says "don't use mock databases"; state has an assumption
    # "use mock databases freely" (opposing negation + high overlap).
    running = Provenanced(
        text="use mock databases freely in tests",
        provenance=make_provenance("agent:impl-bravo", "use mock databases freely in tests",
                                   confidence=0.7),
    )
    _seed(
        constraints=["do not use mock databases in tests"],
        assumptions=[running],
    )

    findings = drift.run_all(AGENT)
    ac = [f for f in findings if f.check == "assumption_conflict"]
    assert ac, f"expected assumption_conflict, got: {[f.check for f in findings]}"
    assert ac[0].suggested_action == "nudge"
    assert 0.0 < ac[0].severity <= 1.0


# ---------------------------------------------------------------------------
# Check 5: branch_irrelevant
# ---------------------------------------------------------------------------

def test_branch_irrelevant_fires_on_off_topic_calls():
    """Objective talks about TiDB; tool calls are all about cooking — avg overlap < 0.2."""
    calls = [
        _make_tool_call(i, "Read", f"Read {topic} cookbook recipe {i}")
        for i, topic in enumerate(
            ["banana", "zebra", "cucumber", "giraffe", "tomato",
             "elephant", "carrot", "tiger", "potato", "panda"],
            start=1,
        )
    ]
    _seed(
        objective="Port intention-engine storage to TiDB and demo recall",
        tool_calls=calls,
    )
    findings = drift.run_all(AGENT)
    br = [f for f in findings if f.check == "branch_irrelevant"]
    assert br, f"expected branch_irrelevant, got: {[f.check for f in findings]}"
    assert br[0].severity == pytest.approx(0.6)
    assert br[0].suggested_action == "nudge"


# ---------------------------------------------------------------------------
# Check 6: semantic_drift (should silently skip if intention-engine missing)
# ---------------------------------------------------------------------------

def test_semantic_drift_silently_skips_without_intention_engine(monkeypatch):
    """If intention-engine is not importable, semantic_drift check returns []."""
    # Make sure we can't import intention_engine even if it's installed
    monkeypatch.setitem(sys.modules, "intention_engine", None)

    _seed(
        decisions=[
            Provenanced(text="write the schema",
                        provenance=make_provenance("agent", "write the schema")),
        ],
    )
    mission = store.load_current_mission(AGENT)
    state = store.load_state(AGENT)
    findings = drift.check_semantic_drift(mission, state)
    assert findings == []


# ---------------------------------------------------------------------------
# Failure isolation — run_all must not raise even if an internal check errors
# ---------------------------------------------------------------------------

def test_run_all_fails_open_when_check_raises(monkeypatch):
    """If a check raises, run_all should swallow and continue."""
    calls = [_make_tool_call(1, "Bash", "rm -rf /tmp/x")]
    _seed(constraints=["no destructive rm -rf"], tool_calls=calls)

    def _broken(*_a, **_k):
        raise RuntimeError("intentionally broken check")

    # Replace branch_irrelevant with a broken version; constraint_drop should still fire.
    monkeypatch.setattr(drift, "check_branch_irrelevant", _broken)

    # Monkeypatch _CHECKS so our broken function gets picked up
    monkeypatch.setattr(drift, "_CHECKS", (
        ("subgoal_detached", lambda m, s, tc: drift.check_subgoal_detached(m, s, tc)),
        ("constraint_drop", lambda m, s, tc: drift.check_constraint_drop(m, s, tc)),
        ("branch_irrelevant", lambda m, s, tc: _broken(m, s, tc)),
    ))

    findings = drift.run_all(AGENT)
    assert isinstance(findings, list)
    # constraint_drop should still have fired despite branch_irrelevant being broken
    assert any(f.check == "constraint_drop" for f in findings)


# ---------------------------------------------------------------------------
# Findings are persisted to findings.jsonl
# ---------------------------------------------------------------------------

def test_findings_are_persisted_to_jsonl():
    """run_all should append every finding to findings.jsonl."""
    calls = [_make_tool_call(1, "Bash", "rm -rf /tmp/x")]
    _seed(constraints=["no rm -rf allowed"], tool_calls=calls)

    findings = drift.run_all(AGENT)
    assert findings, "expected at least one finding"

    persisted = store.load_findings(AGENT)
    assert len(persisted) >= len(findings)
    persisted_checks = [p.get("check") for p in persisted]
    assert "constraint_drop" in persisted_checks
