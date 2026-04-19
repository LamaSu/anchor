#!/usr/bin/env bash
# anchor tool-call hook. Fires after every tool call so anchord can count events
# and trigger drift checks per scheduler policy.
#
# Wire into ~/.claude/settings.json:
#   "hooks": {
#     "PostToolUse": [
#       {"matcher": "*", "hooks": [
#         {"type": "command", "command": "bash ~/anchor/hooks/tool-call.sh"}
#       ]}
#     ]
#   }
#
# Reads hook JSON from stdin. Extracts tool name + success, posts to /tool-call.

set -euo pipefail

ANCHORD_HOST="${ANCHORD_HOST:-127.0.0.1}"
ANCHORD_PORT="${ANCHORD_PORT:-3458}"
AGENT_ID="${ANCHOR_AGENT_ID:-${CLAUDE_SESSION_ID:-default}}"

PAYLOAD=$(cat || echo "{}")

# Extract tool_name and whether it errored from the hook payload.
# Hook schema: {tool_name, tool_input, tool_output, error}
TOOL=$(python -c "import sys,json; d=json.loads(sys.stdin.read() or '{}'); print(d.get('tool_name',''))" <<<"${PAYLOAD}" 2>/dev/null || echo "")
OK=$(python -c "import sys,json; d=json.loads(sys.stdin.read() or '{}'); print('false' if d.get('error') else 'true')" <<<"${PAYLOAD}" 2>/dev/null || echo "true")

if [[ -z "${TOOL}" ]]; then
  exit 0
fi

# Compute args_hash (sha256 prefix of tool_input) — no payload stored.
ARGS_HASH=$(python - "${PAYLOAD}" <<'PY' 2>/dev/null || echo ""
import sys, json, hashlib
try:
    d = json.loads(sys.argv[1])
    canon = json.dumps(d.get("tool_input", {}), sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    print(f"sha256:{h}")
except Exception:
    print("")
PY
)

SUMMARY=$(python - "${PAYLOAD}" <<'PY' 2>/dev/null || echo ""
import sys, json
try:
    d = json.loads(sys.argv[1])
    ti = d.get("tool_input", {})
    for key in ("command", "file_path", "query", "pattern", "url"):
        if key in ti:
            print(str(ti[key])[:200])
            break
    else:
        print("")
except Exception:
    print("")
PY
)

BODY=$(python -c "
import json, sys
print(json.dumps({
    'agent_id': '${AGENT_ID}',
    'tool': '${TOOL}',
    'ok': ${OK},
    'summary': '''${SUMMARY}'''[:200],
    'args_hash': '${ARGS_HASH}'
}))
" 2>/dev/null || echo "{}")

curl -sS --max-time 2 \
  -H "Content-Type: application/json" \
  -X POST -d "${BODY}" \
  "http://${ANCHORD_HOST}:${ANCHORD_PORT}/tool-call" >/dev/null 2>&1 || true

exit 0
