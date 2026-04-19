"""Smoke tests for the HTTP daemon — start in-thread, probe endpoints."""
from __future__ import annotations

import json
import threading
import time
from http.server import ThreadingHTTPServer
from urllib import request as urlreq

import pytest

from anchord.server import AnchordHandler


@pytest.fixture
def running_server(isolated_anchor_home):
    server = ThreadingHTTPServer(("127.0.0.1", 0), AnchordHandler)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Tiny settle window
    time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _get(base: str, path: str) -> dict:
    with urlreq.urlopen(base + path, timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(base: str, path: str, payload: dict) -> dict:
    req = urlreq.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlreq.urlopen(req, timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


def test_healthz(running_server):
    r = _get(running_server, "/healthz")
    assert r["ok"] is True
    assert r["version"]


def test_mission_new_and_show(running_server):
    r = _post(running_server, "/mission/new", {
        "agent_id": "agent-t1", "prompt": "ship the sensor API by Friday",
    })
    assert "envelope" in r
    assert r["envelope"]["hash"].startswith("sha256:")

    shown = _get(running_server, "/mission/agent-t1")
    assert shown["hash"] == r["envelope"]["hash"]


def test_list_missions(running_server):
    _post(running_server, "/mission/new", {
        "agent_id": "agent-t2", "prompt": "do the thing",
    })
    r = _get(running_server, "/missions")
    assert any(m["agent_id"] == "agent-t2" for m in r["missions"])


def test_state_append_and_show(running_server):
    _post(running_server, "/mission/new", {
        "agent_id": "agent-t3", "prompt": "goal",
    })
    r = _post(running_server, "/state/append", {
        "agent_id": "agent-t3", "kind": "decision",
        "text": "use postgres 15", "source": "user", "confidence": 1.0,
    })
    assert r["ok"] is True

    shown = _get(running_server, "/state/agent-t3")
    assert len(shown["decisions"]) == 1


def test_tool_call_records(running_server):
    _post(running_server, "/mission/new", {
        "agent_id": "agent-t4", "prompt": "goal",
    })
    r = _post(running_server, "/tool-call", {
        "agent_id": "agent-t4", "tool": "Read", "ok": True, "summary": "x.py",
    })
    assert r["ok"] is True


def test_snapshot_and_resume(running_server):
    _post(running_server, "/mission/new", {
        "agent_id": "agent-t5", "prompt": "goal",
    })
    _post(running_server, "/state/append", {
        "agent_id": "agent-t5", "kind": "decision", "text": "use redis",
    })
    snap = _post(running_server, "/compact/snapshot", {"agent_id": "agent-t5"})
    assert snap["event_class"] == "compaction"

    resume = _get(running_server, "/compact/resume/agent-t5")
    assert "digest" in resume
    assert "use redis" in resume["digest"]


def test_memory_check(running_server):
    _post(running_server, "/mission/new", {
        "agent_id": "agent-t6", "prompt": "goal",
    })
    _post(running_server, "/state/append", {
        "agent_id": "agent-t6", "kind": "decision", "text": "the api must use postgres",
    })
    result = _post(running_server, "/memory/check", {
        "agent_id": "agent-t6",
        "item": {
            "text": "the api must not use postgres",
            "provenance": {
                "source": "user",
                "hash": "sha256:newitem",
                "confidence": 1.0,
            },
        },
    })
    assert "warnings" in result
    assert any(w["type"] == "contradiction" for w in result["warnings"])


def test_404_on_missing(running_server):
    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        _get(running_server, "/mission/no-such-agent")
