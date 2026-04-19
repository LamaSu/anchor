---
name: anchor
description: Mission anchor — typed invariants + drift-checked working state + Merkle-chained provenance. Use when the user wants to pin a mission, check drift, snapshot before compaction, or resume after one.
triggers:
  - /anchor
  - anchor mission
  - anchor drift
  - anchor resume
  - anchor status
---

# /anchor skill

The `anchor` daemon (`anchord` on localhost:3458) keeps a durable mission
envelope, a drift-checked working state, and a Merkle-chained provenance log
for every agent session. This skill wraps the daemon's HTTP API as a
human-friendly interface.

## Subcommands

### `/anchor status`
Quick health + mission list.
```bash
anchor status
```
If daemon is not running, start it: `anchord &` (or `python -m anchord`).

### `/anchor mission new <agent-id> "<prompt>"`
Extract a `MissionSpec` from the prompt, hash it, write a `MissionEnvelope`
to `~/.anchor/<agent-id>/missions/<hash>.json`. Starts a Langfuse trace keyed
on the mission hash.
```bash
anchor mission new sonnet-alpha "build a sensor API with tests; must be offline-tolerant; ship by Friday"
```

### `/anchor mission show <agent-id>`
Print the current mission envelope.

### `/anchor state show <agent-id>`
Print the WorkingState: current subgoal, decisions (with provenance),
running assumptions, open questions, recent tool calls.

### `/anchor findings <agent-id>`
List drift findings. Severity ≥ 0.6 means "nudge"; severity ≥ 0.9 means "halt".

### `/anchor snapshot <agent-id>`
Manually trigger a compaction snapshot. Wire into PreCompact hook for
automatic capture before `/compact`:

```json
{
  "hooks": {
    "PreCompact": [
      {"matcher": "*", "hooks": [
        {"type": "command", "command": "bash ~/anchor/hooks/pre-compact.sh"}
      ]}
    ]
  }
}
```

### `/anchor resume <agent-id>`
After `/compact` wipes the transcript, print a rehydration digest: current
mission, last compaction's decisions/open-questions, current mode. Feed this
straight back into the next turn so the agent remembers what it was doing.

## When to use

- User asks "what's my mission?" or "am I drifting?" → `/anchor mission show` + `/anchor findings`
- Starting a long task → `/anchor mission new`
- About to run `/compact` → `/anchor snapshot` first (or let the hook do it)
- Just resumed from `/compact` → `/anchor resume` to rehydrate
- Debugging "why did the agent go off-track?" → `/anchor findings` + look at severity ≥ 0.6

## Design notes

- `mission_hash` doubles as the Langfuse trace id → `https://us.cloud.langfuse.com/trace/<hash>` opens the full span tree.
- Fails open: missing daemon, missing Langfuse keys, missing intention-engine — all silently no-op.
- Every state-bearing item (decision, assumption, open question, constraint) carries a `ProvenanceEnvelope` for retrieval-time contradiction detection.
- Compaction envelopes (`event_class="compaction"`) chain from the mission root so lineage survives `/compact`.

## On-disk layout

```
~/.anchor/
  <agent-id>/
    mission.json             # head
    missions/<hash>.json     # every envelope
    state.json               # WorkingState
    findings.jsonl           # drift findings
    tool-calls.jsonl         # tool events
    lineage.json             # {envelopes: {...}, head}
    telemetry.jsonl          # local span fallback
```
