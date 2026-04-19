"""Typed schemas for mission state, working state, drift findings, and provenance.

Mirrors §4 + §4.5 of anchor-brief.md, plus the ProvenanceEnvelope and compaction
additions from anchor-memory-integration-plan.md.

All models are pydantic for validation; every state-bearing item (decisions,
assumptions, open_questions, findings) carries a ProvenanceEnvelope so
contradictions can surface at retrieval time.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

class ProvenanceEnvelope(BaseModel):
    """Receipts for every memory item. Enables retrieval-time contradiction detection."""
    source: str = Field(
        description='Origin: "user", "tool:Read", "agent:impl-bravo", "memory:<file>.md", '
                    '"extract:claude-haiku", "legacy", etc.'
    )
    recorded_at: datetime = Field(default_factory=now_utc)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0,
                              description="User-stated = 1.0; LLM-inferred typical 0.5-0.8")
    hash: str = Field(description="sha256 prefix of the item's canonical JSON")
    parent_hash: str | None = Field(default=None,
                                    description="If derived from another provenanced item")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "source": "user",
            "recorded_at": "2026-04-18T17:02:11Z",
            "confidence": 1.0,
            "hash": "sha256:c3a1d9e2",
            "parent_hash": None,
        }
    })


class Provenanced(BaseModel):
    """Mixin: any typed item that carries provenance."""
    text: str
    provenance: ProvenanceEnvelope


# ---------------------------------------------------------------------------
# Mission
# ---------------------------------------------------------------------------

class Deliverable(BaseModel):
    name: str
    acceptance: str = Field(description="Observable condition that proves it's done")


class MissionSpec(BaseModel):
    """Stable mission state — written once per /mission/new, amended by creating a child envelope."""
    objective: str = Field(description="One sentence: what the agent is trying to accomplish")
    subgoals: list[str] = Field(default_factory=list,
                                description="Ordered breakdown; first element is current focus")
    deliverables: list[Deliverable] = Field(default_factory=list)
    constraints: list[Provenanced] = Field(default_factory=list,
                                           description="Must-not-violate rules (budget, forbidden actions)")
    assumptions: list[Provenanced] = Field(default_factory=list,
                                           description="Pre-conditions believed true at mission start")
    success_signals: list[str] = Field(default_factory=list,
                                       description="Externally checkable indicators of success")


class MissionEnvelope(BaseModel):
    """Merkle-chained wrapper around MissionSpec. One file per envelope.

    event_class values:
      - "new":        initial mission from /mission/new
      - "amend":      PATCH /mission/:agent_id adding/revising a field
      - "compaction": PreCompact hook snapshot (carries .diff)
      - "fork":       child mission spawned from a parent agent
    """
    hash: str
    parent_hash: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    created_by: str = Field(description='Actor: "user", "agent:<name>", "hook:pre-compact"')
    event_class: Literal["new", "amend", "compaction", "fork"] = "new"
    spec: MissionSpec
    diff: "CompactionDiff | None" = Field(
        default=None,
        description="Populated only when event_class == 'compaction'",
    )


# ---------------------------------------------------------------------------
# Working State
# ---------------------------------------------------------------------------

class ToolCallRef(BaseModel):
    seq: int = Field(description="Monotonic counter within the agent session")
    tool: str
    ts: datetime = Field(default_factory=now_utc)
    ok: bool = True
    summary: str = Field(default="", max_length=280)
    args_hash: str = Field(default="", description="sha256 prefix of tool args (no payload stored)")


class OpenQuestion(Provenanced):
    raised_by: str | None = None
    status: Literal["open", "resolved", "deferred"] = "open"


class WorkingState(BaseModel):
    """Episodic state — updated continuously as the agent works."""
    agent_id: str
    mission_hash: str
    current_subgoal_idx: int = 0
    decisions: list[Provenanced] = Field(default_factory=list)
    running_assumptions: list[Provenanced] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    tool_calls: list[ToolCallRef] = Field(default_factory=list)
    mode: Literal["plan", "build", "debug", "verify"] = "plan"
    last_updated: datetime = Field(default_factory=now_utc)

    def decision_hashes(self) -> set[str]:
        return {d.provenance.hash for d in self.decisions}


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------

DriftCheck = Literal[
    "subgoal_detached",
    "deliverable_loss",
    "constraint_drop",
    "assumption_conflict",
    "branch_irrelevant",
    "semantic_drift",
]


class ProvTriple(BaseModel):
    """PROV-O-shaped provenance for a single DriftFinding."""
    agent: str = Field(description='e.g., "agent:impl-bravo"')
    activity: str = Field(description='e.g., "drift-check:branch_irrelevant"')
    used: list[str] = Field(default_factory=list,
                            description='Input refs: mission:<hash>, state:<hash>, tool-call:<seq>')
    generated: str = Field(description='e.g., "finding:F42"')
    wasDerivedFrom: list[str] = Field(default_factory=list)
    langfuse_span_id: str | None = None


class DriftFinding(BaseModel):
    id: str
    check: DriftCheck
    severity: float = Field(ge=0.0, le=1.0)
    message: str
    detected_at: datetime = Field(default_factory=now_utc)
    evidence: dict[str, Any] = Field(default_factory=dict)
    suggested_action: Literal["ignore", "nudge", "rehydrate", "halt"] = "nudge"
    recovery_payload_ref: str | None = None
    provenance: ProvTriple


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------

class CompactionDiff(BaseModel):
    """Structured delta between the pre-compact state and the mission baseline."""
    decisions_added: list[Provenanced] = Field(default_factory=list)
    decisions_revised: list[Provenanced] = Field(default_factory=list)
    files_touched: list[str] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    mode_transitions: list[str] = Field(default_factory=list,
                                        description='e.g., ["plan->build", "build->debug"]')
    tool_call_count: int = 0


# ---------------------------------------------------------------------------
# Memory check (retrieval-time contradictions)
# ---------------------------------------------------------------------------

class MemoryWarning(BaseModel):
    type: Literal["contradiction", "stale", "low_confidence"]
    conflicting_item: Provenanced | None = None
    severity: float = Field(ge=0.0, le=1.0)
    suggestion: str


class MemoryCheckResult(BaseModel):
    warnings: list[MemoryWarning] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=now_utc)
    latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# Request/response envelopes (for the HTTP layer)
# ---------------------------------------------------------------------------

class NewMissionRequest(BaseModel):
    agent_id: str
    prompt: str = Field(description="Raw task prompt to extract MissionSpec from")
    parent_hash: str | None = None
    created_by: str = "user"


class AmendMissionRequest(BaseModel):
    field: Literal["assumption", "constraint", "deliverable", "subgoal", "objective"]
    op: Literal["add", "revise", "remove"]
    value: dict[str, Any] | str
    reason: str = ""
    created_by: str = "user"


class AppendStateRequest(BaseModel):
    agent_id: str
    kind: Literal["decision", "assumption", "open_question"]
    text: str
    source: str = "agent"
    confidence: float = 1.0


class ToolCallEvent(BaseModel):
    agent_id: str
    tool: str
    ok: bool = True
    summary: str = ""
    args_hash: str = ""


class CheckMemoryRequest(BaseModel):
    agent_id: str
    item: Provenanced | dict[str, Any]
    context: dict[str, Any] | None = None


class CompactSnapshotRequest(BaseModel):
    agent_id: str
    created_by: str = "hook:pre-compact"


# Forward-ref resolution for MissionEnvelope.diff
MissionEnvelope.model_rebuild()
