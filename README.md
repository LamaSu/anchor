# anchor

Typed mission invariants + drift-checked working state + Merkle-chained provenance for agent runs.

`anchor` is a single daemon (`anchord`) that sits next to any agent runtime (Claude Code, LangGraph, a bare LLM loop) and gives it three things that ephemeral memory can't:

1. **A stable mission envelope** — the agent's objective, deliverables, constraints, and assumptions, signed by canonical-JSON Merkle hash and versioned by parent chain.
2. **A drift-checked working state** — six typed checks (`subgoal_detached`, `deliverable_loss`, `constraint_drop`, `assumption_conflict`, `branch_irrelevant`, `semantic_drift`) running on a schedule tied to tool-call count and event class.
3. **PROV-O provenance on every item** — every memory, decision, assumption, and finding carries `source`, `recorded_at`, `confidence`, and `hash` so contradictions surface at retrieval time, not after.

All of it keyed on `mission_hash`, which doubles as the Langfuse trace id — so `https://us.cloud.langfuse.com/trace/<mission_hash>` opens the full span tree for the run.

## Status

`v0.1.0` — built for AGI House Agent Harness Build Day 2026-04-18.

## Quickstart

```bash
git clone https://github.com/LamaSu/anchor ~/anchor
cd ~/anchor
pip install -e ".[all]"

# Run the daemon (stdlib http.server on localhost:3458)
python -m anchord

# In another shell:
curl -fsS http://localhost:3458/healthz
```

Environment variables (optional — anchor works offline without any of them):

```bash
# Telemetry — LangFuse traces keyed on mission_hash
export LANGFUSE_PUBLIC_KEY=pk-...
export LANGFUSE_SECRET_KEY=sk-...
export LANGFUSE_HOST=https://us.cloud.langfuse.com

# Mission extractor — falls back to a template extractor if unset
export ANTHROPIC_API_KEY=sk-ant-...
export ANCHOR_EXTRACT_MODEL=claude-haiku-4-5-20251001

# Semantic substrate — falls back to deterministic rules if unset
export INTENTION_ENGINE_URL=http://localhost:8080
```

## HTTP API (localhost:3458)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/healthz` | Liveness probe |
| POST | `/mission/new` | Extract MissionSpec from raw prompt; returns `{mission_hash, spec}` |
| GET | `/mission/:agent_id` | Current mission envelope (with lineage) |
| PATCH | `/mission/:agent_id` | Amend assumption / constraint / deliverable; writes new child envelope |
| POST | `/events/tool-call` | Record a tool call; may trigger drift engine |
| POST | `/state/append` | Append a decision/assumption/open-question (wrapped with provenance) |
| GET | `/state/:agent_id` | Current WorkingState |
| GET | `/drift/:agent_id` | Stream of DriftFindings for an agent |
| GET | `/lineage/:agent_id` | Full mission+compaction chain |
| POST | `/memory/check` | Retrieval-time contradiction check (<50ms target) |
| POST | `/compact/snapshot` | PreCompact hook writes a compaction envelope |
| GET | `/compact/latest/:agent_id` | Resume-time rehydration payload |
| POST | `/events/agent-stop` | Finalize the Langfuse trace |

## Architecture

Three layers, one daemon, thin adapters:

```
    agent prompt  ───►  anchord daemon  ───►  LangFuse trace
                          │                      ▲
                          ├── mission envelope   │
                          ├── working state      │
                          ├── drift checks  ─────┘
                          └── PROV-O findings
                                  │
                          intention-engine
                          (optional semantic substrate)
```

See `docs/ARCHITECTURE.md` for full detail. Design brief lives in the companion doc `anchor-brief.md`.

## What this is not

- Not an orchestrator. anchord doesn't schedule agents; it observes them.
- Not a memory store. It holds typed *invariants*, not chat history.
- Not a vector DB. Semantic scoring for drift checks 5–6 is delegated to intention-engine when present.

## Integration with Claude Code

Two tiny hooks (`hooks/pre-compact.sh` and `hooks/tool-call.sh`) wire anchord into the Claude Code harness. After installing:

```bash
/anchor show                 # current mission
/anchor amend "add constraint: budget ≤ $5"
/anchor check-now            # force a drift pass
/anchor resume               # post-compact rehydration
/anchor stop                 # finalize the trace
```

The skill at `skills/anchor.md` is copied into `~/.claude/commands/` by the installer.

## License

MIT.
