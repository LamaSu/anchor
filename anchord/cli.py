"""`anchor` CLI — thin client for the anchord HTTP API.

Commands:
  anchor status                       → GET /healthz + /missions
  anchor mission new <agent> <prompt> → POST /mission/new
  anchor mission show <agent>         → GET /mission/<agent>
  anchor state show <agent>           → GET /state/<agent>
  anchor findings <agent>             → GET /findings/<agent>
  anchor snapshot <agent>             → POST /compact/snapshot
  anchor resume <agent>               → GET /compact/resume/<agent>
  anchor probe                        → quick smoke-test of daemon
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib import error as urlerror
from urllib import request as urlreq

DEFAULT_HOST = os.environ.get("ANCHORD_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("ANCHORD_PORT", "3458"))


def _url(path: str) -> str:
    return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}{path}"


def _get(path: str) -> dict:
    with urlreq.urlopen(_url(path), timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path: str, payload: dict) -> dict:
    req = urlreq.Request(
        _url(path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlreq.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def _patch(path: str, payload: dict) -> dict:
    req = urlreq.Request(
        _url(path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    with urlreq.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def _pp(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _cmd_status(_args) -> int:
    try:
        health = _get("/healthz")
        missions = _get("/missions")
    except urlerror.URLError as e:
        print(f"anchord unreachable at {DEFAULT_HOST}:{DEFAULT_PORT} — {e}", file=sys.stderr)
        return 2
    _pp({"health": health, "missions": missions.get("missions", [])})
    return 0


def _cmd_mission_new(args) -> int:
    payload = {"agent_id": args.agent, "prompt": args.prompt}
    _pp(_post("/mission/new", payload))
    return 0


def _cmd_mission_show(args) -> int:
    _pp(_get(f"/mission/{args.agent}"))
    return 0


def _cmd_state_show(args) -> int:
    _pp(_get(f"/state/{args.agent}"))
    return 0


def _cmd_findings(args) -> int:
    _pp(_get(f"/findings/{args.agent}"))
    return 0


def _cmd_snapshot(args) -> int:
    _pp(_post("/compact/snapshot", {"agent_id": args.agent}))
    return 0


def _cmd_resume(args) -> int:
    _pp(_get(f"/compact/resume/{args.agent}"))
    return 0


def _cmd_probe(_args) -> int:
    try:
        health = _get("/healthz")
    except urlerror.URLError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2
    if not health.get("ok"):
        print("FAIL: /healthz returned not-ok", file=sys.stderr)
        return 2
    print(f"OK: anchord {health.get('version')} at {health.get('root')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="anchor", description="anchor client")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(func=_cmd_status)
    sub.add_parser("probe").set_defaults(func=_cmd_probe)

    m = sub.add_parser("mission").add_subparsers(dest="subcmd", required=True)
    mn = m.add_parser("new"); mn.add_argument("agent"); mn.add_argument("prompt"); mn.set_defaults(func=_cmd_mission_new)
    ms = m.add_parser("show"); ms.add_argument("agent"); ms.set_defaults(func=_cmd_mission_show)

    s = sub.add_parser("state").add_subparsers(dest="subcmd", required=True)
    ss = s.add_parser("show"); ss.add_argument("agent"); ss.set_defaults(func=_cmd_state_show)

    f = sub.add_parser("findings"); f.add_argument("agent"); f.set_defaults(func=_cmd_findings)
    sn = sub.add_parser("snapshot"); sn.add_argument("agent"); sn.set_defaults(func=_cmd_snapshot)
    r = sub.add_parser("resume"); r.add_argument("agent"); r.set_defaults(func=_cmd_resume)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
