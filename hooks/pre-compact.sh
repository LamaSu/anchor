#!/usr/bin/env bash
# anchor pre-compact hook.
#
# Wire into Claude Code by adding to ~/.claude/settings.json:
#   "hooks": {
#     "PreCompact": [
#       {"matcher": "*", "hooks": [
#         {"type": "command", "command": "bash ~/anchor/hooks/pre-compact.sh"}
#       ]}
#     ]
#   }
#
# Captures the pre-compaction state into a compaction MissionEnvelope so the
# session can resume with full provenance after /compact rewrites the transcript.

set -euo pipefail

ANCHORD_HOST="${ANCHORD_HOST:-127.0.0.1}"
ANCHORD_PORT="${ANCHORD_PORT:-3458}"
AGENT_ID="${ANCHOR_AGENT_ID:-${CLAUDE_SESSION_ID:-default}}"

URL="http://${ANCHORD_HOST}:${ANCHORD_PORT}/compact/snapshot"
BODY="{\"agent_id\":\"${AGENT_ID}\",\"created_by\":\"hook:pre-compact\"}"

# Silently no-op if daemon is not running. Hook must NEVER block compaction.
RESPONSE=$(curl -sS --max-time 3 \
  -H "Content-Type: application/json" \
  -X POST -d "${BODY}" "${URL}" 2>/dev/null) || true

if [[ -n "${RESPONSE:-}" ]]; then
  HASH=$(python -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('hash',''))" <<<"${RESPONSE}" 2>/dev/null) || HASH=""
  if [[ -n "${HASH}" ]]; then
    echo "[anchor] compaction snapshot: ${HASH}" >&2
  fi
fi

exit 0
