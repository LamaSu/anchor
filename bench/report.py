"""Render bench-results.json as a self-contained HTML dashboard.

Open the output file directly in a browser. Chart.js is pulled from a CDN, so
an internet connection is required for the rendered charts (but the data
table renders offline).

Usage:
  python -m bench.report                 # writes bench/report.html
  python -m bench.report --open          # writes and opens in default browser
"""
from __future__ import annotations

import argparse
import json
import webbrowser
from pathlib import Path

from bench._lib import REPO_ROOT, load_results, results_path


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>anchor — benchmark telemetry</title>
<style>
  :root {
    --bg:#0b0d12; --panel:#11151c; --ink:#e6eaf2; --mute:#8b93a7;
    --accent:#8cf0c3; --warn:#f7b955; --fail:#ff6b6b; --pass:#8cf0c3;
    --grid:#1b2130; --chip:#1f2635;
  }
  * { box-sizing:border-box; }
  html,body { margin:0; padding:0; background:var(--bg); color:var(--ink);
    font:14px/1.5 "SF Mono","JetBrains Mono",ui-monospace,Menlo,Consolas,monospace; }
  header { padding:24px 32px 12px; border-bottom:1px solid var(--grid); }
  h1 { margin:0; font-size:22px; letter-spacing:0.3px; }
  h2 { margin:28px 0 10px; font-size:16px; color:var(--accent); letter-spacing:0.2px; }
  h3 { margin:18px 0 8px; font-size:13px; color:var(--mute); text-transform:uppercase; letter-spacing:0.08em; }
  main { padding:20px 32px 48px; max-width:1360px; margin:0 auto; }
  .sub { color:var(--mute); font-size:12px; }
  .row { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; }
  .card { background:var(--panel); border:1px solid var(--grid); border-radius:10px; padding:16px; }
  .kpi { display:flex; justify-content:space-between; align-items:baseline; }
  .kpi .v { font-size:28px; color:var(--accent); }
  .kpi .l { font-size:11px; color:var(--mute); text-transform:uppercase; letter-spacing:0.08em; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th { text-align:left; padding:6px 8px; color:var(--mute); font-weight:500;
    border-bottom:1px solid var(--grid); }
  td { padding:6px 8px; border-bottom:1px solid var(--grid); }
  tr:last-child td { border-bottom:none; }
  .pill { display:inline-block; padding:1px 8px; border-radius:6px; font-size:11px; }
  .pass { background:rgba(140,240,195,0.15); color:var(--pass); }
  .warn { background:rgba(247,185,85,0.15); color:var(--warn); }
  .fail { background:rgba(255,107,107,0.15); color:var(--fail); }
  canvas { max-width:100%; }
  .chip { display:inline-block; padding:2px 10px; background:var(--chip); border-radius:99px;
    font-size:11px; color:var(--mute); margin-right:6px; }
  footer { margin-top:40px; padding-top:16px; border-top:1px solid var(--grid);
    color:var(--mute); font-size:11px; }
  .metric-big { font-size:40px; line-height:1; color:var(--accent); }
  .metric-lbl { font-size:11px; color:var(--mute); text-transform:uppercase; letter-spacing:0.1em; margin-top:6px;}
  .two { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
</style>
</head>
<body>
<header>
  <h1>anchor · benchmark telemetry</h1>
  <div class="sub" id="meta"></div>
</header>
<main>
  <section>
    <h2>Headline KPIs</h2>
    <div class="row" id="kpis"></div>
  </section>

  <section>
    <h2>SLO gate</h2>
    <div class="card">
      <div id="slo-verdict"></div>
      <table id="slo-table">
        <thead><tr><th>case</th><th>p95 (ms)</th><th>target (ms)</th><th>status</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>HTTP endpoint latency (per-request)</h2>
    <div class="card">
      <canvas id="http-chart" height="120"></canvas>
      <h3 style="margin-top:16px">Raw table</h3>
      <table id="http-table">
        <thead><tr><th>endpoint</th><th>n</th><th>p50</th><th>p95</th><th>p99</th><th>mean</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>End-to-end mission lifecycle</h2>
    <div class="card">
      <div id="lifecycle-summary" class="two"></div>
      <h3>Per-step latency</h3>
      <canvas id="lifecycle-chart" height="120"></canvas>
    </div>
  </section>

  <section>
    <h2>Drift-tick scaling</h2>
    <div class="card">
      <p class="sub">How does run_all() latency scale with WorkingState decision count?</p>
      <canvas id="drift-chart" height="120"></canvas>
    </div>
  </section>

  <section>
    <h2>Concurrency</h2>
    <div class="card">
      <p class="sub">Throughput + p95 tail latency as parallel agents increase.</p>
      <div class="two">
        <div><canvas id="conc-throughput" height="160"></canvas></div>
        <div><canvas id="conc-latency" height="160"></canvas></div>
      </div>
    </div>
  </section>

  <section>
    <h2>Ablation · with vs. without anchor</h2>
    <div class="card">
      <div class="row" id="ablation-kpis"></div>
      <h3>Side by side</h3>
      <canvas id="ablation-chart" height="180"></canvas>
      <h3>Script</h3>
      <div class="sub" id="ablation-script"></div>
    </div>
  </section>

  <footer>
    Generated by <code>bench/report.py</code> from <code>bench-results.json</code>.
    Numbers from the most recent <code>python -m bench.main</code> run.
  </footer>
</main>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script>
const DATA = __DATA__;
const fmt = (n, d=2) => (n === null || n === undefined) ? '—' : Number(n).toFixed(d);

function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html!==undefined) e.innerHTML = html; return e; }

function pill(v) { return `<span class="pill ${v.toLowerCase()}">${v}</span>`; }

// ---------- metadata ----------
const md = DATA.metadata || {};
document.getElementById('meta').innerHTML =
  `<span class="chip">run: ${md.started_at || '?'}</span>` +
  `<span class="chip">host: ${md.host || '?'}</span>` +
  `<span class="chip">anchor v${md.anchor_version || '?'}</span>` +
  `<span class="chip">python ${md.python_version || '?'}</span>`;

// ---------- KPIs ----------
const kpis = document.getElementById('kpis');
const http = (DATA.suites && DATA.suites.http) || {};
const life = (DATA.suites && DATA.suites.lifecycle) || {};
const slo = (DATA.suites && DATA.suites.slo) || {};
const abl = (DATA.suites && DATA.suites.ablation) || {};
const conc = (DATA.suites && DATA.suites.concurrency) || {};

const memcheck = (http.cases || []).find(c => c.name === 'POST /memory/check');
const toolcall = (http.cases || []).find(c => c.name === 'POST /tool-call');

const kpiData = [
  { v: fmt(memcheck?.p95_ms), l: 'memory_check p95 (ms)', target: '< 50', hint: '(SLO)' },
  { v: fmt(toolcall?.p95_ms), l: 'tool-call p95 (ms)' },
  { v: fmt((life.total_wallclock || {}).mean_ms), l: 'mission flow mean (ms)' },
  { v: slo.verdict || '—', l: 'SLO verdict', cls: (slo.verdict || '').toLowerCase() },
  { v: fmt((abl.with_anchor || {}).contradiction_catch_rate * 100, 0) + '%', l: 'contradictions caught (w/ anchor)' },
  { v: fmt((abl.with_anchor || {}).post_compact_fidelity * 100, 0) + '%', l: 'post-compact fidelity (w/ anchor)' },
];
for (const k of kpiData) {
  const c = el('div', 'card');
  const big = el('div', 'metric-big ' + (k.cls || ''), k.v);
  c.appendChild(big);
  c.appendChild(el('div', 'metric-lbl', k.l + (k.target ? ` · target ${k.target}` : '')));
  kpis.appendChild(c);
}

// ---------- SLO ----------
const sloBody = document.querySelector('#slo-table tbody');
document.getElementById('slo-verdict').innerHTML =
  `<strong>verdict:</strong> ${pill(slo.verdict || '—')} · ${slo.passed||0} passed · ${slo.failed||0} failed · ${slo.skipped||0} skipped`;
for (const r of (slo.rows || [])) {
  const row = el('tr');
  row.innerHTML = `<td>${r.case}</td><td>${fmt(r.p95_ms)}</td><td>${fmt(r.threshold_ms)}</td><td>${pill(r.status)}</td>`;
  sloBody.appendChild(row);
}

// ---------- HTTP chart + table ----------
const httpTableBody = document.querySelector('#http-table tbody');
const labels = [], p50 = [], p95 = [], p99 = [];
for (const c of (http.cases || [])) {
  labels.push(c.name);
  p50.push(c.p50_ms); p95.push(c.p95_ms); p99.push(c.p99_ms);
  const row = el('tr');
  row.innerHTML = `<td>${c.name}</td><td>${c.n}</td><td>${fmt(c.p50_ms)}</td><td>${fmt(c.p95_ms)}</td><td>${fmt(c.p99_ms)}</td><td>${fmt(c.mean_ms)}</td>`;
  httpTableBody.appendChild(row);
}
if (labels.length) {
  new Chart(document.getElementById('http-chart'), {
    type: 'bar',
    data: { labels, datasets: [
      { label:'p50', data:p50, backgroundColor:'#8cf0c3' },
      { label:'p95', data:p95, backgroundColor:'#f7b955' },
      { label:'p99', data:p99, backgroundColor:'#ff6b6b' },
    ]},
    options: { responsive:true,
      scales: { y: { title:{display:true,text:'ms',color:'#8b93a7'}, grid:{color:'#1b2130'}, ticks:{color:'#8b93a7'} },
                x: { grid:{color:'#1b2130'}, ticks:{color:'#8b93a7'} } },
      plugins: { legend: { labels: {color:'#e6eaf2'} } } }
  });
}

// ---------- Lifecycle ----------
const ls = document.getElementById('lifecycle-summary');
const tot = life.total_wallclock || {};
ls.appendChild(Object.assign(el('div','card'),
  { innerHTML:`<div class="metric-big">${fmt(tot.p50_ms)}</div><div class="metric-lbl">flow total p50 (ms)</div>` }));
ls.appendChild(Object.assign(el('div','card'),
  { innerHTML:`<div class="metric-big">${fmt(tot.p95_ms)}</div><div class="metric-lbl">flow total p95 (ms)</div>` }));

const per = life.per_step || {};
const stepKeys = Object.keys(per).filter(k => k !== 'total');
if (stepKeys.length) {
  new Chart(document.getElementById('lifecycle-chart'), {
    type: 'bar',
    data: { labels: stepKeys, datasets: [
      { label:'p50', data: stepKeys.map(k=>per[k].p50_ms), backgroundColor:'#8cf0c3' },
      { label:'p95', data: stepKeys.map(k=>per[k].p95_ms), backgroundColor:'#f7b955' },
    ]},
    options: { responsive:true,
      scales: { y: { title:{display:true,text:'ms',color:'#8b93a7'}, grid:{color:'#1b2130'}, ticks:{color:'#8b93a7'} },
                x: { grid:{color:'#1b2130'}, ticks:{color:'#8b93a7'} } },
      plugins: { legend: { labels: {color:'#e6eaf2'} } } }
  });
}

// ---------- Drift load ----------
const drift = (DATA.suites && DATA.suites.drift_load) || {};
const dLabels = (drift.runs || []).map(r => r.decisions);
const dHttp = (drift.runs || []).map(r => r.http_tick_ms?.p95_ms);
const dInproc = (drift.runs || []).map(r => r.inproc_run_all_ms?.p95_ms);
if (dLabels.length) {
  new Chart(document.getElementById('drift-chart'), {
    type:'line',
    data:{ labels:dLabels, datasets: [
      { label:'HTTP tick p95 (ms)', data:dHttp, borderColor:'#f7b955', backgroundColor:'#f7b95533', fill:true, tension:0.3 },
      { label:'run_all inproc p95 (ms)', data:dInproc, borderColor:'#8cf0c3', backgroundColor:'#8cf0c333', fill:true, tension:0.3 },
    ]},
    options:{ responsive:true,
      scales:{ y:{ title:{display:true,text:'ms',color:'#8b93a7'}, grid:{color:'#1b2130'}, ticks:{color:'#8b93a7'} },
               x:{ title:{display:true,text:'state decisions',color:'#8b93a7'}, grid:{color:'#1b2130'}, ticks:{color:'#8b93a7'} } },
      plugins:{ legend:{ labels:{color:'#e6eaf2'} } } }
  });
}

// ---------- Concurrency ----------
const cLabels = (conc.runs || []).map(r => r.agents);
const cThrough = (conc.runs || []).map(r => r.throughput_ops_s);
const cP95 = (conc.runs || []).map(r => r.latency?.p95_ms);
if (cLabels.length) {
  new Chart(document.getElementById('conc-throughput'), {
    type:'line',
    data:{ labels:cLabels, datasets:[{ label:'throughput (ops/s)', data:cThrough,
           borderColor:'#8cf0c3', backgroundColor:'#8cf0c333', fill:true, tension:0.3 }]},
    options:{ responsive:true,
      scales:{ y:{ grid:{color:'#1b2130'}, ticks:{color:'#8b93a7'} },
               x:{ title:{display:true,text:'parallel agents',color:'#8b93a7'}, grid:{color:'#1b2130'}, ticks:{color:'#8b93a7'} } },
      plugins:{ legend:{ labels:{color:'#e6eaf2'} } } }
  });
  new Chart(document.getElementById('conc-latency'), {
    type:'line',
    data:{ labels:cLabels, datasets:[{ label:'p95 latency (ms)', data:cP95,
           borderColor:'#f7b955', backgroundColor:'#f7b95533', fill:true, tension:0.3 }]},
    options:{ responsive:true,
      scales:{ y:{ grid:{color:'#1b2130'}, ticks:{color:'#8b93a7'} },
               x:{ title:{display:true,text:'parallel agents',color:'#8b93a7'}, grid:{color:'#1b2130'}, ticks:{color:'#8b93a7'} } },
      plugins:{ legend:{ labels:{color:'#e6eaf2'} } } }
  });
}

// ---------- Ablation ----------
if (abl.with_anchor) {
  const w = abl.with_anchor, wo = abl.without_anchor;
  const ak = document.getElementById('ablation-kpis');
  const makeKpi = (v, l) => { const c = el('div','card'); c.innerHTML =
    `<div class="metric-big">${v}</div><div class="metric-lbl">${l}</div>`; return c; };
  ak.appendChild(makeKpi(`${w.contradictions_caught}/${w.contradictions_injected}`,
    'contradictions caught (w/ anchor)'));
  ak.appendChild(makeKpi(`${wo.contradictions_caught}/${wo.contradictions_injected}`,
    'contradictions caught (baseline)'));
  ak.appendChild(makeKpi(`${Math.round(w.post_compact_fidelity*100)}%`,
    'post-compact fidelity (w/ anchor)'));
  ak.appendChild(makeKpi(`${Math.round(wo.post_compact_fidelity*100)}%`,
    'post-compact fidelity (baseline)'));

  new Chart(document.getElementById('ablation-chart'), {
    type:'bar',
    data:{
      labels:['contradiction catch rate','drift fire rate','post-compact fidelity'],
      datasets:[
        { label:'with anchor',    data:[w.contradiction_catch_rate, w.drift_fire_rate, w.post_compact_fidelity],
          backgroundColor:'#8cf0c3' },
        { label:'without anchor', data:[wo.contradiction_catch_rate, wo.drift_fire_rate, wo.post_compact_fidelity],
          backgroundColor:'#ff6b6b' },
      ]
    },
    options:{ responsive:true,
      scales:{ y:{ min:0, max:1, title:{display:true,text:'fraction',color:'#8b93a7'},
                   grid:{color:'#1b2130'}, ticks:{color:'#8b93a7'} },
               x:{ grid:{color:'#1b2130'}, ticks:{color:'#8b93a7'} } },
      plugins:{ legend:{ labels:{color:'#e6eaf2'} } } }
  });

  document.getElementById('ablation-script').innerHTML =
    `Identical ${abl.script_length}-turn scripted session run against both modes. ` +
    `Mission: "<em>${abl.mission_prompt}</em>". ` +
    `${abl.mission_constraints.length} constraints measured for recovery.`;
}
</script>
</body>
</html>
"""


def render(output: Path | None = None) -> Path:
    data = load_results()
    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(data, separators=(",", ":")))
    out = output or (REPO_ROOT / "bench" / "report.html")
    out.write_text(html, encoding="utf-8")
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--open", action="store_true", help="Open the rendered HTML in the default browser")
    p.add_argument("--output", type=Path, default=None)
    args = p.parse_args()

    path = render(args.output)
    print(f"  wrote {path}")
    print(f"  source: {results_path()}")
    if args.open:
        webbrowser.open(path.as_uri())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
