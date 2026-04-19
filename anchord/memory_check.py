"""Retrieval-time contradiction detection.

Call `check(item)` before writing a memory/decision/assumption into WorkingState.
Surfaces:
  - contradiction: conflicts with an existing high-confidence item
  - stale: item references past state the session has already moved past
  - low_confidence: LLM-inferred (<0.7) competing with user-stated (==1.0)

Targets sub-50ms latency: cheap keyword/string similarity first, semantic
comparison only if intention-engine is importable.
"""
from __future__ import annotations

import re
import time
from typing import Iterable

from .schemas import (
    MemoryCheckResult,
    MemoryWarning,
    Provenanced,
    WorkingState,
)


# ---------------------------------------------------------------------------
# Lightweight similarity (no external deps)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text) if len(t) >= 3}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_NEGATION_MARKERS = {
    "not", "no", "never", "none", "n't", "without", "cannot", "can't",
    "don't", "doesn't", "won't", "shouldn't", "isn't", "aren't", "wasn't",
}


def _has_opposing_negation(text_a: str, text_b: str) -> bool:
    """Cheap heuristic: same content words, one side has a negation the other doesn't."""
    la = text_a.lower()
    lb = text_b.lower()
    neg_a = any(m in la for m in _NEGATION_MARKERS)
    neg_b = any(m in lb for m in _NEGATION_MARKERS)
    if neg_a == neg_b:
        return False
    # Do the non-negation content tokens overlap substantially?
    a_core = _tokens(la) - _NEGATION_MARKERS
    b_core = _tokens(lb) - _NEGATION_MARKERS
    return _jaccard(a_core, b_core) >= 0.55


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check(
    item: Provenanced,
    state: WorkingState | None = None,
    constraints: Iterable[Provenanced] = (),
    assumptions: Iterable[Provenanced] = (),
) -> MemoryCheckResult:
    t0 = time.perf_counter()
    warnings: list[MemoryWarning] = []

    candidates: list[Provenanced] = []
    if state is not None:
        candidates.extend(state.decisions)
        candidates.extend(state.running_assumptions)
    candidates.extend(constraints)
    candidates.extend(assumptions)

    item_tokens = _tokens(item.text)

    for existing in candidates:
        if existing.provenance.hash == item.provenance.hash:
            continue
        ex_tokens = _tokens(existing.text)
        sim = _jaccard(item_tokens, ex_tokens)

        # 1. Direct contradiction: high lexical overlap + opposing negation
        if _has_opposing_negation(item.text, existing.text):
            severity = min(1.0, 0.6 + 0.4 * sim)
            warnings.append(MemoryWarning(
                type="contradiction",
                conflicting_item=existing,
                severity=severity,
                suggestion=(
                    f"New item appears to contradict an earlier item recorded "
                    f"{_age_hint(existing)}. Confirm or revise before storing."
                ),
            ))
            continue

        # 2. Low-confidence overwrite of user-stated fact
        if (
            sim >= 0.7
            and existing.provenance.confidence >= 0.95
            and item.provenance.confidence < 0.7
        ):
            warnings.append(MemoryWarning(
                type="low_confidence",
                conflicting_item=existing,
                severity=0.5,
                suggestion=(
                    "Low-confidence inference overlaps with a user-stated item. "
                    "Prefer the user-stated version."
                ),
            ))

    # 3. Stale: state references a subgoal the session has already moved past
    if state is not None and state.current_subgoal_idx > 0:
        stale_markers = {
            f"subgoal 0", f"subgoal[0]", f"first subgoal",
        }
        lowered = item.text.lower()
        if any(m in lowered for m in stale_markers):
            warnings.append(MemoryWarning(
                type="stale",
                conflicting_item=None,
                severity=0.3,
                suggestion=(
                    f"Item references subgoal 0 but session is now on "
                    f"subgoal {state.current_subgoal_idx}."
                ),
            ))

    dt_ms = (time.perf_counter() - t0) * 1000
    return MemoryCheckResult(warnings=warnings, latency_ms=round(dt_ms, 2))


def _age_hint(item: Provenanced) -> str:
    try:
        from datetime import datetime, timezone
        age = datetime.now(timezone.utc) - item.provenance.recorded_at
        secs = int(age.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return "earlier"
