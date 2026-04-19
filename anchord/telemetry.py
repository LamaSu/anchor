"""Langfuse telemetry wrapper keyed on mission_hash.

Fails open: if Langfuse isn't reachable or keys are missing, all operations
become no-ops and anchord continues working offline. The mission_hash doubles
as the Langfuse trace id so `https://us.cloud.langfuse.com/trace/<hash>` opens
the full span tree.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .store import agent_dir


class _NullSpan:
    def __init__(self, **kwargs: Any) -> None:
        self.id = kwargs.get("id", "null-span")

    def update(self, **_: Any) -> None:
        pass

    def end(self, **_: Any) -> None:
        pass

    # Context-manager protocol
    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, *_: Any) -> None:
        pass


class Telemetry:
    """Thin shim over Langfuse, plus a local JSONL fallback so traces are never lost."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.jsonl_path: Path = agent_dir(agent_id) / "telemetry.jsonl"
        self.lf: Any | None = None
        self._init_langfuse()

    def _init_langfuse(self) -> None:
        pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
        sk = os.environ.get("LANGFUSE_SECRET_KEY")
        if not pk or not sk:
            return
        try:
            from langfuse import Langfuse  # type: ignore[import-not-found]
            self.lf = Langfuse(
                public_key=pk,
                secret_key=sk,
                host=os.environ.get("LANGFUSE_HOST", "https://us.cloud.langfuse.com"),
            )
        except Exception:
            self.lf = None

    # ------------------------------------------------------------------
    # Traces
    # ------------------------------------------------------------------
    def start_trace(self, mission_hash: str, parent_hash: str | None = None,
                    name: str = "anchor-mission") -> None:
        self._jsonl({"event": "trace_start", "mission_hash": mission_hash,
                     "parent_hash": parent_hash, "name": name})
        if self.lf is None:
            return
        try:
            # Newer Langfuse SDK style: trace() or Trace class
            self.lf.trace(
                id=mission_hash,
                name=name,
                metadata={"parent_hash": parent_hash, "agent_id": self.agent_id},
            )
        except Exception:
            pass

    def finalize_trace(self, mission_hash: str, status: str = "done",
                       cost_usd: float | None = None) -> None:
        self._jsonl({"event": "trace_finalize", "mission_hash": mission_hash,
                     "status": status, "cost_usd": cost_usd})
        if self.lf is None:
            return
        try:
            self.lf.flush()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Spans
    # ------------------------------------------------------------------
    @contextmanager
    def span(self, mission_hash: str, name: str, **kwargs: Any) -> Iterator[Any]:
        self._jsonl({"event": "span_start", "mission_hash": mission_hash,
                     "name": name, "kwargs": kwargs})
        span: Any = _NullSpan()
        if self.lf is not None:
            try:
                span = self.lf.span(
                    trace_id=mission_hash,
                    name=name,
                    **{k: v for k, v in kwargs.items() if k in {"input", "output", "metadata", "level"}},
                )
            except Exception:
                span = _NullSpan()
        try:
            yield span
        finally:
            try:
                if hasattr(span, "end"):
                    span.end()
            except Exception:
                pass
            self._jsonl({"event": "span_end", "mission_hash": mission_hash, "name": name})

    def span_url(self, mission_hash: str) -> str:
        host = os.environ.get("LANGFUSE_HOST", "https://us.cloud.langfuse.com")
        return f"{host.rstrip('/')}/trace/{mission_hash}"

    # ------------------------------------------------------------------
    # Fallback: always write to a local jsonl so we have a trace even offline
    # ------------------------------------------------------------------
    def _jsonl(self, record: dict[str, Any]) -> None:
        record.setdefault("ts", datetime.utcnow().isoformat() + "Z")
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
