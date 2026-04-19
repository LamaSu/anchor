"""Tests for anchord.extract.

Focus is on the template fallback path (Anthropic path requires a live key and
is exercised implicitly when ANTHROPIC_API_KEY is present). Every test forces
the template path by clearing ANTHROPIC_API_KEY via the `no_sdk` fixture.
"""
from __future__ import annotations

import os
import sys

import pytest

# Ensure we can import anchord from the repo root when running pytest from cwd.
# The repo already has pyproject.toml so `pip install -e .` would be cleaner,
# but this keeps tests runnable without install.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from anchord import extract  # noqa: E402
from anchord.schemas import MissionSpec, Provenanced  # noqa: E402


@pytest.fixture(autouse=True)
def no_sdk(monkeypatch):
    """Force the template path — remove the key so _try_anthropic returns None early."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield


# ---------------------------------------------------------------------------
# Basic contract tests
# ---------------------------------------------------------------------------

def test_returns_missionspec_for_simple_prompt():
    spec = extract.extract_mission("Build a sensor API.")
    assert isinstance(spec, MissionSpec)
    assert spec.objective  # non-empty
    assert spec.objective.lower().startswith("build a sensor api")


def test_empty_prompt_fails_open():
    spec = extract.extract_mission("")
    assert isinstance(spec, MissionSpec)
    # Should not raise, should return something sensible
    assert spec.objective


def test_whitespace_only_prompt_fails_open():
    spec = extract.extract_mission("   \n\t  ")
    assert isinstance(spec, MissionSpec)
    assert spec.objective


# ---------------------------------------------------------------------------
# Template fallback — specific extractions
# ---------------------------------------------------------------------------

def test_template_fallback_when_sdk_missing(monkeypatch):
    """With no API key (and no 'anthropic' hit), we fall through to the template path.

    Also exercises the end-to-end: given a rich prompt, we should see multiple
    fields populated via heuristics.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    prompt = (
        "Build a sensor API with unit tests, must not use mocks, "
        "and assume Python 3.11 is available. Tests should pass by tomorrow."
    )
    spec = extract.extract_mission(prompt)

    assert isinstance(spec, MissionSpec)
    # Objective is the first sentence
    assert "sensor api" in spec.objective.lower()

    # Constraints should catch "must not"
    constraint_texts = [c.text.lower() for c in spec.constraints]
    assert any("must not" in t for t in constraint_texts), \
        f"expected 'must not' constraint, got: {constraint_texts}"

    # Assumptions should catch "assume"
    assumption_texts = [a.text.lower() for a in spec.assumptions]
    assert any("assume" in t for t in assumption_texts), \
        f"expected 'assume' assumption, got: {assumption_texts}"

    # Deadline should surface as a constraint ("by tomorrow")
    assert any("tomorrow" in t for t in constraint_texts), \
        f"expected deadline constraint with tomorrow, got: {constraint_texts}"

    # Success signal: "tests should pass"
    success_joined = " ".join(spec.success_signals).lower()
    assert "pass" in success_joined or "tests" in success_joined, \
        f"expected success signal about tests, got: {spec.success_signals}"


def test_template_catches_must_not_constraint():
    """'must not' in the prompt should become a constraint with template-source provenance."""
    prompt = "Refactor the storage layer. Must not break the existing JSON API."
    spec = extract.extract_mission(prompt)

    assert isinstance(spec, MissionSpec)
    # At least one constraint present
    assert len(spec.constraints) >= 1
    # Each constraint is a Provenanced with provenance from template source
    for c in spec.constraints:
        assert isinstance(c, Provenanced)
        assert c.provenance.source == "extract:template"
        assert c.provenance.confidence == pytest.approx(0.5)
        # Provenance hash is populated
        assert c.provenance.hash.startswith("sha256:")

    # At least one must contain "must not"
    texts_lc = [c.text.lower() for c in spec.constraints]
    assert any("must not" in t for t in texts_lc)


def test_first_line_objective_fallback():
    """If the prompt is a single line with no markers, the line itself is the objective."""
    prompt = "Port the storage backend to TiDB."
    spec = extract.extract_mission(prompt)

    assert isinstance(spec, MissionSpec)
    assert spec.objective.startswith("Port the storage backend")
    # No constraints/assumptions expected (no triggers in this phrasing)
    assert len(spec.constraints) == 0
    assert len(spec.assumptions) == 0


# ---------------------------------------------------------------------------
# Extra: subgoals, deliverables, deadline handling, provenance on assumptions
# ---------------------------------------------------------------------------

def test_bulleted_subgoals_populate_list():
    prompt = (
        "Ship the recall pipeline.\n"
        "- Write schema\n"
        "- Wire TiDB backend\n"
        "- Add cosine test\n"
    )
    spec = extract.extract_mission(prompt)
    assert len(spec.subgoals) >= 3
    joined = " ".join(spec.subgoals).lower()
    assert "schema" in joined and "cosine" in joined


def test_deadline_becomes_constraint():
    prompt = "Ship the demo by Friday."
    spec = extract.extract_mission(prompt)
    constraint_texts = [c.text.lower() for c in spec.constraints]
    assert any("friday" in t for t in constraint_texts), \
        f"expected Friday deadline captured, got: {constraint_texts}"


def test_assumption_carries_provenance_envelope():
    prompt = "Rewire the cache. Assume Redis is on port 6379."
    spec = extract.extract_mission(prompt)
    assert len(spec.assumptions) >= 1
    a = spec.assumptions[0]
    assert isinstance(a, Provenanced)
    assert a.provenance.source == "extract:template"
    assert 0.0 <= a.provenance.confidence <= 1.0
    assert a.provenance.recorded_at is not None


def test_missing_anthropic_sdk_does_not_raise(monkeypatch):
    """Even if ANTHROPIC_API_KEY is set, a broken/missing SDK must fail silently."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")

    # Shadow the anthropic module with something that raises on import-attribute-access
    # by replacing the module object with a stub that lacks Anthropic.
    import types
    fake = types.ModuleType("anthropic")

    class _BrokenClient:
        def __init__(self, **_: object) -> None:
            raise RuntimeError("network unreachable")

    fake.Anthropic = _BrokenClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    # Should still return a MissionSpec via template fallback; no exception.
    spec = extract.extract_mission("Build a thing. Must not break.")
    assert isinstance(spec, MissionSpec)
    # Because SDK path errored, we should have the template's constraint visible
    assert any("must not" in c.text.lower() for c in spec.constraints)
