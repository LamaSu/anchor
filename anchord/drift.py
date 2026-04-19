"""Drift engine — the six checks from anchor-brief.md §3.

Each check is a pure function that takes (mission, state, tool_calls) and
returns zero or more DriftFindings. `run_all(agent_id)` loads everything from
store, runs every check, appends findings, and returns them.

Fails open: every check is wrapped in try/except. A broken check must never
prevent the others from running, and the daemon must never raise.

Rules of thumb:
  - Checks 1-3 are pure lexical — jaccard overlap, keyword matching, deliverable
    name presence. Cheap, deterministic, no external deps.
  - Check 4 delegates to memory_check.check() for contradiction detection.
  - Check 5 is lexical overlap between the objective and recent tool calls
    (the §5.5 intention-engine graph-walk is the production version; this is
    the offline-friendly approximation).
  - Check 6 is optional: if `intention_engine` is importable AND exposes an
    embed() API, run a cosine comparison; otherwise skip silently.

Severity levels match the spec:
  subgoal_detached    0.70  nudge
  deliverable_loss    0.80  rehydrate
  constraint_drop     0.95  halt
  assumption_conflict (from memory warning)  nudge
  branch_irrelevant   0.60  nudge
  semantic_drift      0.65  nudge
"""
from __future__ import annotations

import re
import uuid
from typing import Iterable

from . import memory_check, store
from .schemas import (
    DriftFinding,
    MissionEnvelope,
    Provenanced,
    ProvTriple,
    ToolCallRef,
    WorkingState,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")

# Mirrors scheduler.py — keep these in sync intentionally; drift.py must match
# what the scheduler classifies as risk so constraint_drop fires on the same
# surface.
_HIGH_RISK_KEYWORDS = (
    "rm -rf",
    "git push --force",
    "drop table",
    "delete from",
    "kill -9",
    "chmod 777",
    "sudo rm",
    "rm ",  # also catch bare rm
)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text) if len(t) >= 3}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _build_provenance(
    check: str,
    mission: MissionEnvelope | None,
    state: WorkingState | None,
    tool_calls: list[ToolCallRef] | None,
    langfuse_span_id: str | None = None,
) -> ProvTriple:
    used: list[str] = []
    if mission is not None:
        used.append(f"mission:{mission.hash}")
    if state is not None:
        used.append(f"state:{state.agent_id}")
    if tool_calls:
        used.append(f"tool-call:{tool_calls[-1].seq}")
    return ProvTriple(
        agent="anchord:drift-check",
        activity=f"drift-check:{check}",
        used=used,
        generated=f"finding:{_short_id()}",
        langfuse_span_id=langfuse_span_id,
    )


def _finding(
    check: str,
    severity: float,
    message: str,
    evidence: dict,
    suggested_action: str,
    mission: MissionEnvelope | None,
    state: WorkingState | None,
    tool_calls: list[ToolCallRef] | None,
) -> DriftFinding:
    return DriftFinding(
        id=f"F{_short_id()}",
        check=check,  # type: ignore[arg-type]
        severity=severity,
        message=message,
        evidence=evidence,
        suggested_action=suggested_action,  # type: ignore[arg-type]
        provenance=_build_provenance(check, mission, state, tool_calls),
    )


# ---------------------------------------------------------------------------
# Check 1: subgoal_detached
# ---------------------------------------------------------------------------

def check_subgoal_detached(
    mission: MissionEnvelope,
    state: WorkingState,
    tool_calls: list[ToolCallRef],
) -> list[DriftFinding]:
    """Recent N>=5 consecutive tool calls have low lexical overlap with the current subgoal.

    Severity 0.7, suggested_action="nudge".
    """
    subgoals = mission.spec.subgoals
    if not subgoals:
        return []
    idx = max(0, min(state.current_subgoal_idx, len(subgoals) - 1))
    subgoal_text = subgoals[idx]
    subgoal_tokens = _tokens(subgoal_text)
    if not subgoal_tokens:
        return []

    recent = [c for c in tool_calls if (c.summary or c.tool)][-5:]
    if len(recent) < 5:
        return []

    scores: list[float] = []
    for call in recent:
        corpus = f"{call.tool} {call.summary}"
        scores.append(_jaccard(_tokens(corpus), subgoal_tokens))

    # All 5 must be below threshold
    if all(s < 0.15 for s in scores):
        avg = sum(scores) / len(scores)
        return [_finding(
            "subgoal_detached",
            severity=0.7,
            message=(
                f"Last {len(recent)} tool calls have jaccard overlap {avg:.3f} "
                f"with current subgoal ({idx}): '{subgoal_text[:80]}'"
            ),
            evidence={
                "subgoal_idx": idx,
                "subgoal_text": subgoal_text,
                "recent_calls": [{"seq": c.seq, "tool": c.tool, "summary": c.summary[:120]}
                                 for c in recent],
                "jaccard_scores": [round(s, 3) for s in scores],
                "threshold": 0.15,
            },
            suggested_action="nudge",
            mission=mission,
            state=state,
            tool_calls=tool_calls,
        )]

    return []


# ---------------------------------------------------------------------------
# Check 2: deliverable_loss
# ---------------------------------------------------------------------------

def check_deliverable_loss(
    mission: MissionEnvelope,
    state: WorkingState,
) -> list[DriftFinding]:
    """After 20+ tool calls, state has zero references to deliverable names.

    Severity 0.8, suggested_action="rehydrate". Fires once per missing deliverable,
    but coalesced into a single finding so the agent gets one reminder.
    """
    deliverables = mission.spec.deliverables
    if not deliverables:
        return []
    if len(state.tool_calls) < 20:
        return []

    # Build a corpus from everything we're tracking in state
    corpus_parts: list[str] = []
    corpus_parts.extend(d.text for d in state.decisions)
    corpus_parts.extend(a.text for a in state.running_assumptions)
    corpus_parts.extend(q.text for q in state.open_questions)
    corpus_parts.extend(f"{c.tool} {c.summary}" for c in state.tool_calls)
    corpus = " ".join(corpus_parts).lower()
    corpus_tokens = _tokens(corpus)

    missing: list[str] = []
    for d in deliverables:
        name = d.name.strip()
        if not name:
            continue
        name_lc = name.lower()
        name_tokens = _tokens(name)

        # Hit if: literal substring in the joined corpus OR
        # at least one non-trivial token of the name appears in the corpus tokens.
        literal_hit = name_lc in corpus
        token_hit = any(len(t) >= 4 and t in corpus_tokens for t in name_tokens)
        if not literal_hit and not token_hit:
            missing.append(name)

    if not missing:
        return []

    return [_finding(
        "deliverable_loss",
        severity=0.8,
        message=(
            f"{len(missing)}/{len(deliverables)} deliverable(s) have no references "
            f"after {len(state.tool_calls)} tool calls: {missing}"
        ),
        evidence={
            "missing_deliverables": missing,
            "total_deliverables": len(deliverables),
            "tool_call_count": len(state.tool_calls),
        },
        suggested_action="rehydrate",
        mission=mission,
        state=state,
        tool_calls=state.tool_calls,
    )]


# ---------------------------------------------------------------------------
# Check 3: constraint_drop
# ---------------------------------------------------------------------------

def check_constraint_drop(
    mission: MissionEnvelope,
    state: WorkingState,
    tool_calls: list[ToolCallRef],
) -> list[DriftFinding]:
    """A tool call's summary matches a high-risk keyword AND a constraint
    contradicts that action.

    Severity 0.95, suggested_action="halt". One finding per (tool-call, constraint)
    match so the agent sees the specific rule in play.
    """
    constraints = mission.spec.constraints
    if not constraints:
        return []

    # Only look at recent calls — the scheduler fires on ticks so "recent" is
    # the right window; cap to last 25 so this stays O(constraints * 25).
    recent = tool_calls[-25:] if tool_calls else []
    if not recent:
        return []

    findings: list[DriftFinding] = []

    for call in recent:
        summary = (call.summary or "").lower()
        if not summary:
            continue
        matched_kw = next((kw for kw in _HIGH_RISK_KEYWORDS if kw in summary), None)
        if matched_kw is None:
            continue

        # Find constraints that mention the same keyword family
        matching_constraints = _find_matching_constraints(constraints, matched_kw, summary)
        if not matching_constraints:
            continue

        for constraint in matching_constraints:
            findings.append(_finding(
                "constraint_drop",
                severity=0.95,
                message=(
                    f"Tool call seq {call.seq} ({call.tool}) contains '{matched_kw}' "
                    f"which contradicts constraint: '{constraint.text[:120]}'"
                ),
                evidence={
                    "tool_call_seq": call.seq,
                    "tool": call.tool,
                    "summary": call.summary[:200],
                    "matched_keyword": matched_kw,
                    "constraint_text": constraint.text,
                    "constraint_source": constraint.provenance.source,
                },
                suggested_action="halt",
                mission=mission,
                state=state,
                tool_calls=tool_calls,
            ))

    return findings


def _find_matching_constraints(
    constraints: Iterable[Provenanced],
    keyword: str,
    summary: str,
) -> list[Provenanced]:
    """Return constraints whose text references this risky action.

    Matching is intentionally generous:
      - direct substring of the keyword in the constraint text
      - shared content-word overlap with the summary via jaccard >= 0.35
      - explicit negation markers ("no", "don't", "never", "must not") near
        a synonym of the keyword
    """
    kw_root = keyword.strip().split()[0]  # "rm" from "rm -rf"
    summary_tokens = _tokens(summary)

    matches: list[Provenanced] = []
    for c in constraints:
        text_lc = c.text.lower()

        # a. keyword (or its root) directly appears in constraint text
        if keyword in text_lc or kw_root in text_lc:
            matches.append(c)
            continue

        # b. constraint has a negation marker + meaningful overlap with summary
        has_negation = any(
            m in text_lc for m in (
                "no ", "not ", "never ", "don't", "do not", "mustn't", "must not",
                "shouldn't", "should not", "avoid", "without",
            )
        )
        overlap = _jaccard(_tokens(text_lc), summary_tokens)
        if has_negation and overlap >= 0.35:
            matches.append(c)

    return matches


# ---------------------------------------------------------------------------
# Check 4: assumption_conflict
# ---------------------------------------------------------------------------

def check_assumption_conflict(
    mission: MissionEnvelope,
    state: WorkingState,
) -> list[DriftFinding]:
    """For each running assumption, call memory_check.check() against the rest of
    state + mission. Any CONTRADICTION warning becomes a DriftFinding.

    Severity is taken from the memory warning; suggested_action="nudge".
    """
    findings: list[DriftFinding] = []

    for assumption in state.running_assumptions:
        try:
            result = memory_check.check(
                assumption,
                state=state,
                constraints=mission.spec.constraints,
                assumptions=mission.spec.assumptions,
            )
        except Exception:
            continue

        for w in result.warnings:
            if w.type != "contradiction":
                continue
            findings.append(_finding(
                "assumption_conflict",
                severity=float(w.severity),
                message=(
                    f"Assumption conflicts with existing memory: "
                    f"'{assumption.text[:80]}' vs "
                    f"'{(w.conflicting_item.text if w.conflicting_item else '?')[:80]}'"
                ),
                evidence={
                    "assumption_text": assumption.text,
                    "assumption_source": assumption.provenance.source,
                    "conflicting_item_text":
                        (w.conflicting_item.text if w.conflicting_item else None),
                    "memory_suggestion": w.suggestion,
                    "warning_type": w.type,
                },
                suggested_action="nudge",
                mission=mission,
                state=state,
                tool_calls=state.tool_calls,
            ))

    return findings


# ---------------------------------------------------------------------------
# Check 5: branch_irrelevant
# ---------------------------------------------------------------------------

def check_branch_irrelevant(
    mission: MissionEnvelope,
    state: WorkingState,
    tool_calls: list[ToolCallRef],
) -> list[DriftFinding]:
    """Lexical overlap between the objective and the last 10 tool calls.

    Below 0.2 average → severity 0.6, "nudge". The §5.5 production version uses
    intention-engine graph-walk relevance; this is the offline approximation
    that still fires on blatant off-topic branches.
    """
    objective = mission.spec.objective
    if not objective:
        return []
    recent = [c for c in tool_calls if (c.summary or c.tool)][-10:]
    if len(recent) < 5:
        # Not enough signal — avoid false positives early in a session.
        return []

    obj_tokens = _tokens(objective)
    if not obj_tokens:
        return []

    per_call_scores = [
        _jaccard(_tokens(f"{c.tool} {c.summary}"), obj_tokens) for c in recent
    ]
    avg = sum(per_call_scores) / len(per_call_scores)

    if avg < 0.2:
        return [_finding(
            "branch_irrelevant",
            severity=0.6,
            message=(
                f"Avg overlap between last {len(recent)} tool calls and objective "
                f"is {avg:.3f} (< 0.20)"
            ),
            evidence={
                "objective_excerpt": objective[:160],
                "per_call_scores": [round(s, 3) for s in per_call_scores],
                "threshold": 0.2,
                "recent_calls": [{"seq": c.seq, "tool": c.tool, "summary": c.summary[:120]}
                                 for c in recent],
            },
            suggested_action="nudge",
            mission=mission,
            state=state,
            tool_calls=tool_calls,
        )]

    return []


# ---------------------------------------------------------------------------
# Check 6: semantic_drift (optional — requires intention-engine)
# ---------------------------------------------------------------------------

def check_semantic_drift(
    mission: MissionEnvelope,
    state: WorkingState,
) -> list[DriftFinding]:
    """Optional semantic check: cosine(decision-centroid, objective) < 0.5.

    Requires `intention_engine` to be importable and expose one of:
      - Client(backend=...).embed(texts) -> list[vec]
      - embed(text) -> vec

    Silently skips if intention_engine isn't available. Returns empty list
    on any failure so the rest of run_all continues.
    """
    if not state.decisions:
        return []
    objective = mission.spec.objective
    if not objective:
        return []

    embed_fn = _resolve_intention_engine_embed()
    if embed_fn is None:
        return []  # silently skip

    try:
        vecs = embed_fn([objective] + [d.text for d in state.decisions])
    except Exception:
        return []

    if not vecs or len(vecs) < 2:
        return []

    obj_vec = vecs[0]
    decision_vecs = vecs[1:]
    centroid = _centroid(decision_vecs)
    if centroid is None:
        return []

    sim = _cosine(obj_vec, centroid)
    if sim is None:
        return []

    if sim < 0.5:
        return [_finding(
            "semantic_drift",
            severity=0.65,
            message=(
                f"Decision-centroid cosine similarity to objective is {sim:.3f} (< 0.50)"
            ),
            evidence={
                "cosine": round(sim, 4),
                "n_decisions": len(decision_vecs),
                "objective_excerpt": objective[:160],
            },
            suggested_action="nudge",
            mission=mission,
            state=state,
            tool_calls=state.tool_calls,
        )]

    return []


def _resolve_intention_engine_embed():
    """Return a callable texts->list[vec], or None if intention_engine not available."""
    try:
        import intention_engine  # type: ignore[import-not-found]
    except Exception:
        return None

    # Try Client-style API
    for backend in (None, "tidb", "memory"):
        try:
            Client = getattr(intention_engine, "Client", None)
            if Client is not None:
                client = Client() if backend is None else Client(backend=backend)
                if hasattr(client, "embed"):
                    def _fn(texts: list[str]):  # type: ignore[no-redef]
                        return client.embed(texts)
                    return _fn
        except Exception:
            continue

    # Try module-level embed(text) or embed_many(texts)
    embed_many = getattr(intention_engine, "embed_many", None)
    if callable(embed_many):
        return embed_many
    embed = getattr(intention_engine, "embed", None)
    if callable(embed):
        def _fn(texts: list[str]):  # type: ignore[no-redef]
            return [embed(t) for t in texts]
        return _fn

    return None


def _centroid(vectors: list) -> list[float] | None:
    try:
        if not vectors:
            return None
        dim = len(vectors[0])
        if dim == 0:
            return None
        acc = [0.0] * dim
        for v in vectors:
            if len(v) != dim:
                return None
            for i, x in enumerate(v):
                acc[i] += float(x)
        return [x / len(vectors) for x in acc]
    except Exception:
        return None


def _cosine(a, b) -> float | None:
    try:
        if not a or not b or len(a) != len(b):
            return None
        dot = sum(float(x) * float(y) for x, y in zip(a, b))
        na = sum(float(x) * float(x) for x in a) ** 0.5
        nb = sum(float(y) * float(y) for y in b) ** 0.5
        if na == 0.0 or nb == 0.0:
            return None
        return dot / (na * nb)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------

# Each check is wrapped to fail open; the tuple form keeps the checks
# enumerable for debug/metrics.
_CHECKS = (
    (
        "subgoal_detached",
        lambda m, s, tc: check_subgoal_detached(m, s, tc),
    ),
    (
        "deliverable_loss",
        lambda m, s, tc: check_deliverable_loss(m, s),
    ),
    (
        "constraint_drop",
        lambda m, s, tc: check_constraint_drop(m, s, tc),
    ),
    (
        "assumption_conflict",
        lambda m, s, tc: check_assumption_conflict(m, s),
    ),
    (
        "branch_irrelevant",
        lambda m, s, tc: check_branch_irrelevant(m, s, tc),
    ),
    (
        "semantic_drift",
        lambda m, s, tc: check_semantic_drift(m, s),
    ),
)


def run_all(agent_id: str) -> list[DriftFinding]:
    """Run every drift check for `agent_id`. Returns findings (may be empty).

    Fails open: exceptions from any check are swallowed (the check returns
    empty) so the rest of the engine keeps running. Findings are persisted
    via store.append_finding so anchord's SSE stream picks them up.
    """
    findings: list[DriftFinding] = []

    try:
        mission = store.load_current_mission(agent_id)
    except Exception:
        mission = None
    if mission is None:
        return findings

    try:
        state = store.load_state(agent_id)
    except Exception:
        state = None
    if state is None:
        return findings

    try:
        tool_call_dicts = store.load_tool_calls(agent_id)
        tool_calls = [ToolCallRef.model_validate(tc) for tc in tool_call_dicts]
    except Exception:
        tool_calls = list(state.tool_calls)

    for _name, fn in _CHECKS:
        try:
            result = fn(mission, state, tool_calls)
            if result:
                findings.extend(result)
        except Exception:
            # Swallow — a broken check must not break the engine.
            continue

    # Persist findings
    for f in findings:
        try:
            store.append_finding(agent_id, f)
        except Exception:
            continue

    return findings
