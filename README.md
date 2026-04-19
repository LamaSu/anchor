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
| GET | `/healthz` | Liveness probe + ANCHOR_HOME |
| GET | `/missions` | List all agents with a current mission |
| POST | `/mission/new` | Extract MissionSpec from raw prompt; returns envelope + trace URL |
| GET | `/mission/{agent_id}` | Current mission envelope |
| PATCH | `/mission/{agent_id}` | Amend assumption/constraint/deliverable/subgoal/objective; writes child envelope |
| POST | `/state/append` | Append decision/assumption/open_question (wrapped with provenance) |
| GET | `/state/{agent_id}` | Current WorkingState |
| POST | `/tool-call` | Record a tool call; may trigger drift tick |
| GET | `/findings/{agent_id}` | Drift findings log |
| GET | `/findings/stream?agent_id=X` | SSE: high-severity findings (≥ 0.6) live |
| POST | `/memory/check` | Retrieval-time contradiction check (< 50ms target) |
| POST | `/compact/snapshot` | PreCompact hook writes a compaction envelope |
| GET | `/compact/resume/{agent_id}` | Post-compact rehydration digest |

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

Two hooks wire anchord into the Claude Code harness (`hooks/pre-compact.sh`, `hooks/tool-call.sh`). Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreCompact": [{"matcher": "*", "hooks": [{"type": "command", "command": "bash ~/anchor/hooks/pre-compact.sh"}]}],
    "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "bash ~/anchor/hooks/tool-call.sh"}]}]
  }
}
```

Then use the client:
```bash
anchor status                                    # daemon health + mission list
anchor mission new sonnet-alpha "<prompt>"       # pin a mission
anchor mission show sonnet-alpha                 # current envelope + lineage
anchor state show sonnet-alpha                   # decisions + assumptions + open questions
anchor findings sonnet-alpha                     # drift findings
anchor snapshot sonnet-alpha                     # compaction envelope
anchor resume sonnet-alpha                       # post-compact digest
```

The skill at `skills/anchor/SKILL.md` can be copied into `~/.claude/skills/` so `/anchor` works inline.

## Benchmarks

`bench/` is a 6-suite benchmark that runs against a fresh daemon in an
isolated `ANCHOR_HOME`, writes a JSON results file, renders an HTML
dashboard, and gates regressions with SLO thresholds.

```bash
pip install -e ".[dev]"

python -m bench.main           # full suite: HTTP + lifecycle + drift + concurrent + ablation + SLO
python -m bench.main --quick   # faster — fewer iters, smaller loads
python -m bench.demo           # scripted 30-turn session + HTML dashboard
python -m bench.report         # (re)render bench/report.html from latest results
```

The SLO gate in `bench/slo.py` exits non-zero on p95 regressions, so it can
plug straight into CI. The ablation bench (`bench/ablation.py`) runs the
same 30-turn scripted mission twice — once with anchor, once with a naive
"last-500-chars" compaction — and reports:

| metric | baseline | anchor |
|---|---|---|
| contradiction catch rate | 0% | **100%** (2/2 caught via `/memory/check`) |
| drift warnings on off-mission turns | 0 | **3** (via `/tool-call` scheduler + drift engine) |
| post-compact constraint fidelity | 60% | **80%** (structured constraints survive `/compact/snapshot`) |

See `bench/report.html` (rendered after any `python -m bench.main` run) for
per-endpoint latency charts, drift-load scaling curves, and concurrency
throughput/tail-latency over 1–32 parallel agents.

## License

MIT.
