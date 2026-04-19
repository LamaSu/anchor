"""Canonical-JSON Merkle hashing + envelope builders.

The `mission_hash` doubles as the Langfuse trace id (see telemetry.py) — one
identifier for lineage AND trace lookup.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .schemas import (
    CompactionDiff,
    MissionEnvelope,
    MissionSpec,
    ProvenanceEnvelope,
    now_utc,
)


def canonical_json(obj: Any) -> str:
    """Stable, deterministic JSON encoding for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=_json_default)


def _json_default(x: Any) -> Any:
    if hasattr(x, "isoformat"):
        return x.isoformat()
    if hasattr(x, "model_dump"):
        return x.model_dump(mode="json")
    raise TypeError(f"{type(x).__name__} is not JSON-serializable")


def sha256_prefix(payload: str, prefix_chars: int = 16) -> str:
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:prefix_chars]}"


def mission_hash(spec: MissionSpec | dict) -> str:
    payload = spec.model_dump(mode="json") if isinstance(spec, MissionSpec) else spec
    return sha256_prefix(canonical_json(payload))


def envelope_hash(envelope_without_hash: dict) -> str:
    """Hash the envelope itself (for compaction envelopes where we want the outer hash to diverge
    from the plain mission_hash)."""
    stripped = {k: v for k, v in envelope_without_hash.items() if k != "hash"}
    return sha256_prefix(canonical_json(stripped))


def provenance_hash(item: dict | str) -> str:
    """Hash for ProvenanceEnvelope.hash field."""
    if isinstance(item, str):
        return sha256_prefix(item)
    return sha256_prefix(canonical_json(item))


def make_provenance(
    source: str,
    text: str,
    confidence: float = 1.0,
    parent_hash: str | None = None,
) -> ProvenanceEnvelope:
    return ProvenanceEnvelope(
        source=source,
        recorded_at=now_utc(),
        confidence=confidence,
        hash=provenance_hash(text),
        parent_hash=parent_hash,
    )


def build_mission_envelope(
    spec: MissionSpec,
    created_by: str,
    parent_hash: str | None = None,
    event_class: str = "new",
    diff: CompactionDiff | None = None,
) -> MissionEnvelope:
    """Build a MissionEnvelope with its Merkle hash computed."""
    mh = mission_hash(spec)
    if event_class == "compaction" and diff is not None:
        body = {
            "parent_hash": parent_hash,
            "created_at": now_utc().isoformat(),
            "created_by": created_by,
            "event_class": event_class,
            "spec": spec.model_dump(mode="json"),
            "diff": diff.model_dump(mode="json"),
        }
        outer = envelope_hash(body)
        return MissionEnvelope(
            hash=outer,
            parent_hash=parent_hash,
            created_by=created_by,
            event_class="compaction",
            spec=spec,
            diff=diff,
        )
    return MissionEnvelope(
        hash=mh,
        parent_hash=parent_hash,
        created_by=created_by,
        event_class=event_class,  # type: ignore[arg-type]
        spec=spec,
        diff=None,
    )


def walk_chain(envelopes_by_hash: dict[str, MissionEnvelope], leaf_hash: str) -> list[MissionEnvelope]:
    """Walk parent_hash pointers from leaf back to root. Returns newest-first."""
    chain: list[MissionEnvelope] = []
    cursor: str | None = leaf_hash
    seen: set[str] = set()
    while cursor and cursor in envelopes_by_hash and cursor not in seen:
        seen.add(cursor)
        env = envelopes_by_hash[cursor]
        chain.append(env)
        cursor = env.parent_hash
    return chain
