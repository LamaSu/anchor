# anchor demo — 60 seconds

Two flavors:

1. **Scripted bitchin-sweet demo** — one command, 30 turns in ~3s, renders an HTML dashboard:
   ```bash
   pip install -e ".[dev]"
   python -m bench.main           # populate bench/results/bench-results.json
   python -m bench.demo           # scripted 30-turn session + auto-opens dashboard
   ```
2. **Live walkthrough** — step-by-step against a running daemon (below).

## Prereqs
```bash
pip install -e .
anchord &                           # daemon on localhost:3458
```

## Walkthrough

### 1. Pin a mission
```bash
anchor mission new demo-agent "build a sensor API with tests; must tolerate offline mode; ship by Friday"
```
Output includes:
- `envelope.hash` (the Merkle root)
- `trace_url` (Langfuse URL; opens full span tree if keys are set)

### 2. Record decisions + tool calls
```bash
curl -X POST http://localhost:3458/state/append \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"demo-agent","kind":"decision","text":"use fastapi + sqlite","source":"user"}'

curl -X POST http://localhost:3458/tool-call \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"demo-agent","tool":"Write","summary":"api/server.py","ok":true}'
```

### 3. Check memory before storing
```bash
curl -X POST http://localhost:3458/memory/check \
  -H 'Content-Type: application/json' \
  -d '{
    "agent_id":"demo-agent",
    "item":{"text":"do not use fastapi","provenance":{"source":"user","hash":"sha256:x","confidence":1.0}}
  }'
```
Returns a `contradiction` warning against the earlier decision.

### 4. Simulate a compaction
```bash
bash hooks/pre-compact.sh    # or: anchor snapshot demo-agent
```
A new `event_class="compaction"` envelope is written with the diff.

### 5. Resume after compact
```bash
anchor resume demo-agent
```
Prints the rehydration digest: mission objective, current subgoal, recent
decisions, open questions, current mode. Feed this straight into the next
turn so the agent remembers what it was doing.

### 6. Inspect drift
```bash
anchor findings demo-agent
```
Empty at first. After 25+ tool calls (or an event-class trigger like
`rm -rf`), drift checks tick and any finding ≥ 0.6 streams live via
`GET /findings/stream?agent_id=demo-agent`.

### 7. Inspect lineage
```bash
ls ~/.anchor/demo-agent/missions/       # one file per envelope
cat ~/.anchor/demo-agent/lineage.json   # head + every envelope's parent
```
