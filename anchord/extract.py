"""Prompt → MissionSpec extraction.

Two paths, prefer LLM if available:
  1. Anthropic SDK (`claude-haiku-4-5-20251001`) with a structured-output prompt
     requesting JSON that matches MissionSpec. One retry on malformed JSON.
  2. Template fallback — regex/heuristic extractor based on the spec in
     anchor-brief.md §4 (Wave 1C).

Fails open: never raises. Worst case returns MissionSpec(objective=first_line)
with empty lists. Every constraint/assumption surfaced gets a
ProvenanceEnvelope (extract:anthropic @ 0.8 OR extract:template @ 0.5).

The calling site (server.py::_extract_mission) also guards with try/except;
this module's job is to be best-effort and honest about confidence.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from .lineage import make_provenance
from .schemas import Deliverable, MissionSpec, Provenanced


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_mission(prompt: str) -> MissionSpec:
    """Extract a MissionSpec from a free-text prompt.

    Order of attempts:
      1. Anthropic SDK (if importable + ANTHROPIC_API_KEY set)
      2. Template-based heuristic extractor
      3. First-line-only fallback (never raises)
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return MissionSpec(objective="(empty prompt)")

    # Path 1: Anthropic
    spec = _try_anthropic(prompt)
    if spec is not None:
        return spec

    # Path 2: template
    try:
        return _template_extract(prompt)
    except Exception:
        pass

    # Path 3: absolute fallback
    first_line = _first_sentence(prompt)
    return MissionSpec(objective=first_line[:500])


# ---------------------------------------------------------------------------
# Anthropic path
# ---------------------------------------------------------------------------

_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

_EXTRACTION_PROMPT = """\
You are a mission extractor. The user will give you a task prompt. Your job is to return
ONLY a JSON object with this exact shape (no prose, no markdown fences):

{
  "objective": "one sentence describing the core goal",
  "subgoals": ["ordered step 1", "ordered step 2", ...],
  "deliverables": [{"name": "short name", "acceptance": "observable condition that proves it's done"}],
  "constraints": ["must-not-violate rule 1", ...],
  "assumptions": ["precondition believed true 1", ...],
  "success_signals": ["externally checkable indicator 1", ...]
}

Guidance:
- objective: a single sentence, present-tense verb
- subgoals: concrete ordered steps, if any are implied
- deliverables: artifacts/outputs with an acceptance criterion each
- constraints: anything the user said MUST / MUST NOT / SHOULD NOT / DON'T / NEVER
- assumptions: anything the user said "assume" / "given" / "if X then"
- success_signals: passing tests, deadlines, numeric thresholds, external checks

If the prompt is ambiguous, make reasonable guesses. Output valid JSON only.
"""


def _try_anthropic(prompt: str) -> MissionSpec | None:
    """Return a MissionSpec via the Anthropic SDK, or None if unavailable."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic  # type: ignore[import-not-found]
    except Exception:
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
    except Exception:
        return None

    # Attempt up to 2 times on malformed JSON
    last_raw: str = ""
    for attempt in range(2):
        try:
            msg = client.messages.create(
                model=_ANTHROPIC_MODEL,
                max_tokens=1024,
                system=_EXTRACTION_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:
            return None

        raw = _extract_text(msg)
        last_raw = raw
        parsed = _try_parse_json_obj(raw)
        if parsed is not None:
            try:
                return _build_mission_from_json(parsed, source="extract:anthropic", confidence=0.8)
            except Exception:
                # fall through to retry
                pass

    # Both attempts failed to produce valid JSON / MissionSpec
    _ = last_raw  # kept for debug only
    return None


def _extract_text(msg: Any) -> str:
    """Pull the text content out of an Anthropic Messages response robustly."""
    try:
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            parts: list[str] = []
            for blk in content:
                t = getattr(blk, "text", None)
                if t:
                    parts.append(t)
                elif isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(blk.get("text", ""))
            return "\n".join(parts)
        if isinstance(content, str):
            return content
    except Exception:
        pass
    return ""


def _try_parse_json_obj(raw: str) -> dict | None:
    """Be generous: try direct parse, then strip ```json fences, then find the first {...}."""
    if not raw:
        return None
    candidates = [raw, _strip_code_fences(raw)]
    # Also try to locate a balanced {...} block
    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidates.append(raw[brace_start:brace_end + 1])

    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    # ```json ... ``` or ``` ... ```
    m = re.match(r"^```(?:json)?\s*(.*?)```$", s, re.DOTALL)
    if m:
        return m.group(1).strip()
    return s


def _build_mission_from_json(data: dict, source: str, confidence: float) -> MissionSpec:
    objective = str(data.get("objective", "")).strip() or "(no objective)"

    subgoals_raw = data.get("subgoals") or []
    subgoals = [str(s).strip() for s in subgoals_raw if str(s).strip()]

    deliverables_raw = data.get("deliverables") or []
    deliverables: list[Deliverable] = []
    for d in deliverables_raw:
        if isinstance(d, dict):
            name = str(d.get("name") or d.get("target") or "").strip()
            acceptance = str(d.get("acceptance") or d.get("criterion") or "").strip()
            if name:
                deliverables.append(Deliverable(name=name, acceptance=acceptance))
        elif isinstance(d, str) and d.strip():
            deliverables.append(Deliverable(name=d.strip(), acceptance=""))

    def _wrap(texts: Any) -> list[Provenanced]:
        out: list[Provenanced] = []
        for t in (texts or []):
            s = str(t).strip()
            if s:
                out.append(Provenanced(text=s, provenance=make_provenance(source, s, confidence=confidence)))
        return out

    constraints = _wrap(data.get("constraints"))
    assumptions = _wrap(data.get("assumptions"))

    success_raw = data.get("success_signals") or []
    success_signals = [str(s).strip() for s in success_raw if str(s).strip()]

    return MissionSpec(
        objective=objective[:500],
        subgoals=subgoals,
        deliverables=deliverables,
        constraints=constraints,
        assumptions=assumptions,
        success_signals=success_signals,
    )


# ---------------------------------------------------------------------------
# Template path
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

# Bullet / numbered list detector
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$", re.MULTILINE)

# Sub-goal connectors inside flowing sentences
_SUBGOAL_CONNECTORS = re.compile(
    r"\b(?:and then|then|after that|next|followed by|, then)\b",
    re.IGNORECASE,
)

# Constraint triggers
_CONSTRAINT_PATTERNS = [
    # MUST / MUST NOT
    (re.compile(r"\b(?:must not|mustn't|must never)\b[^.!?;]+", re.IGNORECASE), True),
    (re.compile(r"\b(?:must)\b[^.!?;]+", re.IGNORECASE), True),
    # SHOULD / SHOULDN'T (weaker, still a constraint)
    (re.compile(r"\b(?:should not|shouldn't|should never)\b[^.!?;]+", re.IGNORECASE), True),
    (re.compile(r"\b(?:should)\b[^.!?;]+", re.IGNORECASE), True),
    # DO NOT / DON'T / NEVER
    (re.compile(r"\b(?:do not|don't|never)\b[^.!?;]+", re.IGNORECASE), True),
    # "no X" / "without X"
    (re.compile(r"\b(?:without)\s+[a-zA-Z][^.!?;]*", re.IGNORECASE), True),
]

# Deadline/time constraints
_DEADLINE_RE = re.compile(
    r"\b(?:by|before)\s+(?:tomorrow|tonight|today|\d{1,2}(?::\d{2})?\s*(?:am|pm)?|"
    r"mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?|"
    r"next\s+\w+|end\s+of\s+\w+|eod|eow|\w+\s+\d{1,2}(?:st|nd|rd|th)?)",
    re.IGNORECASE,
)

# Assumption triggers
_ASSUMPTION_PATTERNS = [
    re.compile(r"\b(?:assume|assuming|given that|given)\b[^.!?;]+", re.IGNORECASE),
    re.compile(r"\b(?:if)\b[^.!?;]+", re.IGNORECASE),
    re.compile(r"\b(?:we think|we believe|presumably)\b[^.!?;]+", re.IGNORECASE),
]

# Success-signal triggers (run over the whole prompt, not on constraint-remainders)
_SUCCESS_PATTERNS = [
    # "tests pass", "tests should pass", "unit tests pass" — allow words between
    re.compile(r"\btests?\b[^.!?;]{0,40}?\bpass(?:ing|es|ed)?\b", re.IGNORECASE),
    re.compile(r"\bpass(?:ing|es|ed)?\s+tests?\b", re.IGNORECASE),
    re.compile(r"\b(?:reaches|reach|achieves|hits)\b[^.!?;]+", re.IGNORECASE),
    re.compile(r"\b(?:handles|tolerates|supports)\b[^.!?;]+", re.IGNORECASE),
    re.compile(r"\bsuccess(?:ful)?(?:ly)?\b[^.!?;]*", re.IGNORECASE),
    re.compile(r"\b(?:works with|compatible with)\b[^.!?;]+", re.IGNORECASE),
]


def _first_sentence(text: str) -> str:
    parts = _SENTENCE_SPLIT.split(text.strip(), maxsplit=1)
    first = parts[0].strip() if parts else text.strip()
    # If still multi-sentence separated by a single period+space+lowercase, cut at first period anyway.
    dot = first.find(".")
    if dot > 10:
        first = first[:dot].strip()
    return first or text.strip()


def _dedupe_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = re.sub(r"\s+", " ", it).strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(it.strip())
    return out


def _extract_subgoals(prompt: str) -> list[str]:
    subgoals: list[str] = []

    # 1. Bullets / numbered lists
    for m in _BULLET_RE.finditer(prompt):
        subgoals.append(m.group(1).strip())

    # 2. Sentences joined by "and then" / "then" / etc.
    if not subgoals:
        parts = _SUBGOAL_CONNECTORS.split(prompt)
        if len(parts) > 1:
            # Don't treat the first part (likely the objective) as a subgoal
            for chunk in parts[1:]:
                chunk = chunk.strip(" ,.;:")
                if chunk and 3 < len(chunk) < 300:
                    subgoals.append(chunk)

    return _dedupe_preserve(subgoals)


def _extract_constraints(prompt: str) -> list[str]:
    out: list[str] = []
    for pat, _is_hard in _CONSTRAINT_PATTERNS:
        for m in pat.finditer(prompt):
            phrase = m.group(0).strip(" ,.;:")
            if phrase:
                out.append(phrase)
    # Deadline as a constraint
    for m in _DEADLINE_RE.finditer(prompt):
        phrase = f"deadline: {m.group(0).strip()}"
        out.append(phrase)
    return _dedupe_preserve(out)


def _extract_assumptions(prompt: str) -> list[str]:
    out: list[str] = []
    for pat in _ASSUMPTION_PATTERNS:
        for m in pat.finditer(prompt):
            phrase = m.group(0).strip(" ,.;:")
            if phrase and len(phrase) < 300:
                out.append(phrase)
    return _dedupe_preserve(out)


def _extract_success_signals(prompt: str) -> list[str]:
    out: list[str] = []
    for pat in _SUCCESS_PATTERNS:
        for m in pat.finditer(prompt):
            phrase = m.group(0).strip(" ,.;:")
            if phrase:
                out.append(phrase)
    return _dedupe_preserve(out)


def _extract_deliverables(prompt: str) -> list[Deliverable]:
    """Very light heuristic: look for 'build a X', 'write a Y', 'ship Z'."""
    out: list[Deliverable] = []
    patterns = [
        re.compile(r"\b(?:build|write|ship|deliver|create|make|produce|implement)\s+"
                   r"(?:an?\s+|the\s+)?([A-Za-z][\w\- ]{2,60})", re.IGNORECASE),
    ]
    for pat in patterns:
        for m in pat.finditer(prompt):
            raw_name = m.group(1).strip(" ,.;:")
            # Trim trailing connector words
            raw_name = re.sub(
                r"\s+(?:with|that|which|by|and)$", "", raw_name, flags=re.IGNORECASE
            )
            if raw_name and len(raw_name) > 2:
                out.append(Deliverable(name=raw_name, acceptance=""))
    # Dedupe by name
    seen: set[str] = set()
    unique: list[Deliverable] = []
    for d in out:
        key = d.name.lower()
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def _template_extract(prompt: str) -> MissionSpec:
    objective = _first_sentence(prompt)
    subgoals = _extract_subgoals(prompt)
    deliverables = _extract_deliverables(prompt)
    constraint_texts = _extract_constraints(prompt)
    assumption_texts = _extract_assumptions(prompt)
    success_signals = _extract_success_signals(prompt)

    source = "extract:template"
    conf = 0.5

    constraints = [
        Provenanced(text=t, provenance=make_provenance(source, t, confidence=conf))
        for t in constraint_texts
    ]
    assumptions = [
        Provenanced(text=t, provenance=make_provenance(source, t, confidence=conf))
        for t in assumption_texts
    ]

    return MissionSpec(
        objective=objective[:500],
        subgoals=subgoals,
        deliverables=deliverables,
        constraints=constraints,
        assumptions=assumptions,
        success_signals=success_signals,
    )
