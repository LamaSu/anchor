"""Atomic file I/O for ~/.anchor/<agent-id>/*.

Layout (created lazily, one dir per agent_id):

    ~/.anchor/
        <agent-id>/
            mission.json               latest mission envelope (symlink/copy)
            missions/<hash>.json       every envelope ever written
            state.json                 WorkingState (mutable)
            findings.jsonl             append-only DriftFinding log
            tool-calls.jsonl           append-only ToolCallRef log
            lineage.json               {"envelopes": {hash: path, ...}, "head": hash}
            telemetry.jsonl            Langfuse spans (if reachable)
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .schemas import (
    DriftFinding,
    MissionEnvelope,
    ToolCallRef,
    WorkingState,
)


def anchor_root() -> Path:
    root = Path(os.environ.get("ANCHOR_HOME", Path.home() / ".anchor"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def agent_dir(agent_id: str) -> Path:
    d = anchor_root() / _safe(agent_id)
    (d / "missions").mkdir(parents=True, exist_ok=True)
    return d


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)[:128]


# ---------------------------------------------------------------------------
# Atomic write primitives
# ---------------------------------------------------------------------------

def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if hasattr(payload, "model_dump"):
                json.dump(payload.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
            else:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=_default)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload,
        ensure_ascii=False, default=_default,
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _default(x: Any) -> Any:
    if hasattr(x, "isoformat"):
        return x.isoformat()
    if hasattr(x, "model_dump"):
        return x.model_dump(mode="json")
    raise TypeError(f"{type(x).__name__} is not JSON-serializable")


# ---------------------------------------------------------------------------
# Mission envelopes
# ---------------------------------------------------------------------------

def save_mission_envelope(agent_id: str, env: MissionEnvelope) -> Path:
    d = agent_dir(agent_id)
    path = d / "missions" / f"{_safe(env.hash)}.json"
    atomic_write_json(path, env)
    # Also write/update mission.json to point at head
    atomic_write_json(d / "mission.json", env)
    # Maintain lineage index
    _update_lineage_index(agent_id, env)
    return path


def load_mission_envelope(agent_id: str, env_hash: str) -> MissionEnvelope | None:
    path = agent_dir(agent_id) / "missions" / f"{_safe(env_hash)}.json"
    if not path.exists():
        return None
    return MissionEnvelope.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_current_mission(agent_id: str) -> MissionEnvelope | None:
    path = agent_dir(agent_id) / "mission.json"
    if not path.exists():
        return None
    return MissionEnvelope.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_all_envelopes(agent_id: str) -> dict[str, MissionEnvelope]:
    d = agent_dir(agent_id) / "missions"
    out: dict[str, MissionEnvelope] = {}
    if not d.exists():
        return out
    for p in d.glob("*.json"):
        try:
            env = MissionEnvelope.model_validate(json.loads(p.read_text(encoding="utf-8")))
            out[env.hash] = env
        except Exception:
            continue
    return out


def _update_lineage_index(agent_id: str, env: MissionEnvelope) -> None:
    d = agent_dir(agent_id)
    idx_path = d / "lineage.json"
    idx: dict[str, Any] = {"envelopes": {}, "head": None}
    if idx_path.exists():
        try:
            idx = json.loads(idx_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    idx.setdefault("envelopes", {})[env.hash] = {
        "parent_hash": env.parent_hash,
        "event_class": env.event_class,
        "created_at": env.created_at.isoformat(),
        "created_by": env.created_by,
    }
    idx["head"] = env.hash
    atomic_write_json(idx_path, idx)


def latest_compaction(agent_id: str) -> MissionEnvelope | None:
    """Return the most recent compaction envelope, or None."""
    envelopes = load_all_envelopes(agent_id)
    compactions = [e for e in envelopes.values() if e.event_class == "compaction"]
    if not compactions:
        return None
    return max(compactions, key=lambda e: e.created_at)


# ---------------------------------------------------------------------------
# Working state
# ---------------------------------------------------------------------------

def save_state(state: WorkingState) -> Path:
    path = agent_dir(state.agent_id) / "state.json"
    atomic_write_json(path, state)
    return path


def load_state(agent_id: str) -> WorkingState | None:
    path = agent_dir(agent_id) / "state.json"
    if not path.exists():
        return None
    return WorkingState.model_validate(json.loads(path.read_text(encoding="utf-8")))


def ensure_state(agent_id: str, mission_hash: str) -> WorkingState:
    s = load_state(agent_id)
    if s is None:
        s = WorkingState(agent_id=agent_id, mission_hash=mission_hash)
        save_state(s)
    return s


# ---------------------------------------------------------------------------
# Findings + tool calls
# ---------------------------------------------------------------------------

def append_finding(agent_id: str, finding: DriftFinding) -> None:
    append_jsonl(agent_dir(agent_id) / "findings.jsonl", finding)


def load_findings(agent_id: str) -> list[dict]:
    return read_jsonl(agent_dir(agent_id) / "findings.jsonl")


def append_tool_call(agent_id: str, call: ToolCallRef) -> None:
    append_jsonl(agent_dir(agent_id) / "tool-calls.jsonl", call)


def load_tool_calls(agent_id: str) -> list[dict]:
    return read_jsonl(agent_dir(agent_id) / "tool-calls.jsonl")
