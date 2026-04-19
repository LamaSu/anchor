"""HTTP daemon on localhost:3458 (stdlib http.server, no framework).

Endpoints (see README.md for the full table):
  GET    /healthz
  GET    /missions
  POST   /mission/new
  GET    /mission/{agent_id}
  PATCH  /mission/{agent_id}
  POST   /state/append
  GET    /state/{agent_id}
  POST   /tool-call
  GET    /findings/{agent_id}
  GET    /findings/stream              (SSE; high-severity only)
  POST   /compact/snapshot
  GET    /compact/resume/{agent_id}
  POST   /memory/check

Bodies are JSON. Responses are JSON unless otherwise noted.
"""
from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from . import compact as compaction
from . import memory_check as memcheck
from . import scheduler
from . import store
from .lineage import build_mission_envelope, make_provenance
from .schemas import (
    AmendMissionRequest,
    AppendStateRequest,
    CheckMemoryRequest,
    CompactSnapshotRequest,
    Deliverable,
    MissionSpec,
    NewMissionRequest,
    OpenQuestion,
    Provenanced,
    ToolCallEvent,
    ToolCallRef,
    WorkingState,
    now_utc,
)
from .telemetry import Telemetry


DEFAULT_PORT = int(os.environ.get("ANCHORD_PORT", "3458"))
DEFAULT_HOST = os.environ.get("ANCHORD_HOST", "127.0.0.1")


# ---------------------------------------------------------------------------
# Mission extraction (pluggable; falls back to a template if extract.py absent)
# ---------------------------------------------------------------------------

def _extract_mission(prompt: str) -> MissionSpec:
    """Turn a raw prompt into a MissionSpec. Prefers anchord.extract if present."""
    try:
        from . import extract  # optional module, written by implementer agent
        spec = extract.extract_mission(prompt)
        if isinstance(spec, MissionSpec):
            return spec
    except ImportError:
        pass
    except Exception:
        # Never let extraction crash the daemon; fall through to template
        pass

    # Template fallback: first line as objective, nothing else
    first_line = next((ln.strip() for ln in prompt.splitlines() if ln.strip()), prompt.strip())
    return MissionSpec(
        objective=first_line[:500],
        subgoals=[],
        deliverables=[],
        constraints=[],
        assumptions=[],
        success_signals=[],
    )


# ---------------------------------------------------------------------------
# Drift checks (pluggable)
# ---------------------------------------------------------------------------

def _drift_tick(agent_id: str) -> list:
    try:
        from . import drift
        return drift.run_all(agent_id)
    except ImportError:
        return []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class AnchordHandler(BaseHTTPRequestHandler):
    server_version = "anchord/0.1"

    # Shared telemetry instance per-process (agent_id passed at call time)
    _telemetry_cache: dict[str, Telemetry] = {}

    def _telemetry(self, agent_id: str) -> Telemetry:
        if agent_id not in self._telemetry_cache:
            self._telemetry_cache[agent_id] = Telemetry(agent_id)
        return self._telemetry_cache[agent_id]

    # -- helpers ------------------------------------------------------------

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_err(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default log
        if os.environ.get("ANCHORD_VERBOSE"):
            super().log_message(fmt, *args)

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/healthz":
                return self._healthz()
            if path == "/missions":
                return self._list_missions()
            if path.startswith("/mission/"):
                return self._get_mission(path[len("/mission/"):])
            if path.startswith("/state/"):
                return self._get_state(path[len("/state/"):])
            if path.startswith("/findings/stream"):
                return self._findings_stream()
            if path.startswith("/findings/"):
                return self._get_findings(path[len("/findings/"):])
            if path.startswith("/compact/resume/"):
                return self._compact_resume(path[len("/compact/resume/"):])
            self._send_err(404, "not found")
        except Exception as e:
            self._send_err(500, f"{type(e).__name__}: {e}")

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/mission/new":
                return self._new_mission()
            if path == "/state/append":
                return self._state_append()
            if path == "/tool-call":
                return self._tool_call()
            if path == "/compact/snapshot":
                return self._compact_snapshot()
            if path == "/memory/check":
                return self._memory_check()
            self._send_err(404, "not found")
        except Exception as e:
            self._send_err(500, f"{type(e).__name__}: {e}")

    def do_PATCH(self) -> None:
        try:
            path = urlparse(self.path).path
            if path.startswith("/mission/"):
                return self._amend_mission(path[len("/mission/"):])
            self._send_err(404, "not found")
        except Exception as e:
            self._send_err(500, f"{type(e).__name__}: {e}")

    # -- handlers -----------------------------------------------------------

    def _healthz(self) -> None:
        self._send_json(200, {
            "ok": True,
            "version": _version(),
            "root": str(store.anchor_root()),
            "time": now_utc().isoformat(),
        })

    def _list_missions(self) -> None:
        root = store.anchor_root()
        out = []
        if root.exists():
            for d in root.iterdir():
                if not d.is_dir():
                    continue
                mission = d / "mission.json"
                if not mission.exists():
                    continue
                try:
                    j = json.loads(mission.read_text(encoding="utf-8"))
                    out.append({
                        "agent_id": d.name,
                        "hash": j.get("hash"),
                        "objective": j.get("spec", {}).get("objective"),
                        "event_class": j.get("event_class"),
                        "created_at": j.get("created_at"),
                    })
                except Exception:
                    continue
        self._send_json(200, {"missions": out})

    def _get_mission(self, agent_id: str) -> None:
        env = store.load_current_mission(agent_id)
        if env is None:
            return self._send_err(404, "no mission for agent")
        self._send_json(200, env.model_dump(mode="json"))

    def _new_mission(self) -> None:
        body = self._read_json()
        try:
            req = NewMissionRequest.model_validate(body)
        except Exception as e:
            return self._send_err(400, f"invalid request: {e}")

        spec = _extract_mission(req.prompt)
        env = build_mission_envelope(
            spec=spec,
            created_by=req.created_by,
            parent_hash=req.parent_hash,
            event_class="new" if req.parent_hash is None else "fork",
        )
        store.save_mission_envelope(req.agent_id, env)
        state = store.ensure_state(req.agent_id, env.hash)

        tel = self._telemetry(req.agent_id)
        tel.start_trace(env.hash, parent_hash=req.parent_hash, name="mission:new")

        self._send_json(200, {
            "envelope": env.model_dump(mode="json"),
            "state": state.model_dump(mode="json"),
            "trace_url": tel.span_url(env.hash),
        })

    def _amend_mission(self, agent_id: str) -> None:
        body = self._read_json()
        try:
            req = AmendMissionRequest.model_validate(body)
        except Exception as e:
            return self._send_err(400, f"invalid request: {e}")

        current = store.load_current_mission(agent_id)
        if current is None:
            return self._send_err(404, "no mission to amend")

        spec = current.spec.model_copy(deep=True)
        prov = make_provenance(
            source=req.created_by,
            text=str(req.value) if isinstance(req.value, str) else json.dumps(req.value, sort_keys=True),
            confidence=1.0 if req.created_by == "user" else 0.7,
        )

        # Apply op to spec
        if req.field == "objective" and req.op in {"add", "revise"}:
            spec.objective = str(req.value)
        elif req.field == "subgoal":
            if req.op == "add":
                spec.subgoals.append(str(req.value))
            elif req.op == "remove" and isinstance(req.value, str):
                spec.subgoals = [s for s in spec.subgoals if s != req.value]
        elif req.field == "deliverable" and req.op == "add":
            if isinstance(req.value, dict):
                spec.deliverables.append(Deliverable.model_validate(req.value))
        elif req.field == "constraint":
            if req.op == "add":
                spec.constraints.append(Provenanced(text=str(req.value), provenance=prov))
            elif req.op == "remove":
                spec.constraints = [c for c in spec.constraints if c.text != req.value]
        elif req.field == "assumption":
            if req.op == "add":
                spec.assumptions.append(Provenanced(text=str(req.value), provenance=prov))
            elif req.op == "remove":
                spec.assumptions = [a for a in spec.assumptions if a.text != req.value]

        env = build_mission_envelope(
            spec=spec,
            created_by=req.created_by,
            parent_hash=current.hash,
            event_class="amend",
        )
        store.save_mission_envelope(agent_id, env)
        self._send_json(200, env.model_dump(mode="json"))

    def _state_append(self) -> None:
        body = self._read_json()
        try:
            req = AppendStateRequest.model_validate(body)
        except Exception as e:
            return self._send_err(400, f"invalid request: {e}")

        state = store.load_state(req.agent_id)
        if state is None:
            cur = store.load_current_mission(req.agent_id)
            if cur is None:
                return self._send_err(404, "no mission; call /mission/new first")
            state = WorkingState(agent_id=req.agent_id, mission_hash=cur.hash)

        prov = make_provenance(
            source=req.source,
            text=req.text,
            confidence=req.confidence,
        )
        item = Provenanced(text=req.text, provenance=prov)

        if req.kind == "decision":
            if prov.hash in state.decision_hashes():
                return self._send_json(200, {"ok": True, "duplicate": True, "state": state.model_dump(mode="json")})
            state.decisions.append(item)
        elif req.kind == "assumption":
            state.running_assumptions.append(item)
        elif req.kind == "open_question":
            state.open_questions.append(OpenQuestion(
                text=req.text, provenance=prov, raised_by=req.source,
            ))

        state.last_updated = now_utc()
        store.save_state(state)
        self._send_json(200, {"ok": True, "state": state.model_dump(mode="json")})

    def _get_state(self, agent_id: str) -> None:
        state = store.load_state(agent_id)
        if state is None:
            return self._send_err(404, "no state for agent")
        self._send_json(200, state.model_dump(mode="json"))

    def _tool_call(self) -> None:
        body = self._read_json()
        try:
            req = ToolCallEvent.model_validate(body)
        except Exception as e:
            return self._send_err(400, f"invalid request: {e}")

        call = ToolCallRef(
            seq=0,  # scheduler will assign
            tool=req.tool,
            ok=req.ok,
            summary=req.summary,
            args_hash=req.args_hash,
        )
        findings = scheduler.run_if_triggered(
            req.agent_id, call, drift_fn=_drift_tick,
        )
        self._send_json(200, {"ok": True, "findings_triggered": len(findings)})

    def _get_findings(self, agent_id: str) -> None:
        findings = store.load_findings(agent_id)
        self._send_json(200, {"findings": findings})

    def _findings_stream(self) -> None:
        """SSE: tail findings.jsonl, push only severity >= 0.6."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        agent_id = urlparse(self.path).query.split("agent_id=")[-1] if "agent_id=" in self.path else ""
        if not agent_id:
            try:
                self.wfile.write(b"event: error\ndata: agent_id required\n\n")
                self.wfile.flush()
            except OSError:
                pass
            return

        path = store.agent_dir(agent_id) / "findings.jsonl"
        last_size = path.stat().st_size if path.exists() else 0

        try:
            while True:
                time.sleep(0.5)
                if not path.exists():
                    continue
                cur = path.stat().st_size
                if cur <= last_size:
                    continue
                with path.open("rb") as f:
                    f.seek(last_size)
                    new = f.read(cur - last_size).decode("utf-8", errors="replace")
                last_size = cur
                for line in new.splitlines():
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("severity", 0) < 0.6:
                        continue
                    self.wfile.write(f"data: {json.dumps(rec)}\n\n".encode("utf-8"))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _compact_snapshot(self) -> None:
        body = self._read_json()
        try:
            req = CompactSnapshotRequest.model_validate(body)
        except Exception as e:
            return self._send_err(400, f"invalid request: {e}")
        env = compaction.snapshot(
            req.agent_id,
            created_by=req.created_by,
            files_touched=body.get("files_touched"),
            mode_transitions=body.get("mode_transitions"),
        )
        if env is None:
            return self._send_err(404, "no mission/state to snapshot")
        self._send_json(200, env.model_dump(mode="json"))

    def _compact_resume(self, agent_id: str) -> None:
        payload = compaction.resume(agent_id)
        self._send_json(200, payload)

    def _memory_check(self) -> None:
        body = self._read_json()
        try:
            req = CheckMemoryRequest.model_validate(body)
        except Exception as e:
            return self._send_err(400, f"invalid request: {e}")

        if isinstance(req.item, dict):
            item = Provenanced.model_validate(req.item)
        else:
            item = req.item

        state = store.load_state(req.agent_id)
        current = store.load_current_mission(req.agent_id)
        constraints = current.spec.constraints if current else []
        assumptions = current.spec.assumptions if current else []

        result = memcheck.check(
            item=item, state=state,
            constraints=constraints, assumptions=assumptions,
        )
        self._send_json(200, result.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _default(x: Any) -> Any:
    if hasattr(x, "isoformat"):
        return x.isoformat()
    if hasattr(x, "model_dump"):
        return x.model_dump(mode="json")
    raise TypeError(f"{type(x).__name__} is not JSON-serializable")


def _version() -> str:
    from . import __version__
    return __version__


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = ThreadingHTTPServer((host, port), AnchordHandler)
    print(f"anchord listening on http://{host}:{port} (ANCHOR_HOME={store.anchor_root()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nanchord shutting down")
        server.server_close()
