"""The jobs page: one HTML document, no build step, no dependencies.

WHY A SINGLE STRING AND NOT A TEMPLATE ENGINE
---------------------------------------------
The dashboard is a stdlib `http.server` with no template engine and no static
file pipeline. Adding Jinja for one page would mean a new dependency and a
new failure mode. A single f-string-free HTML constant is boring and works.

WHY THE BROWSER DOES THE TIMEZONE CONVERSION
--------------------------------------------
Everything is stored and served in UTC. Converting on the server means the
server has to know the viewer's timezone, which it does not. Converting in
the browser is one line and cannot be wrong.

WHAT THE PAGE SHOWS
-------------------
    header      last collected, counts by outcome, anything needing attention
    filters     search, category, source, outcome, include-disabled
    table       one row per job: name, schedule, outcome, last run, rule note
    detail      click a row to see the log excerpt and the raw detail JSON
    history     a date range picker that loads runs for the window

The "attention" strip at the top is the point of the whole thing: it lists
only the jobs that are `no_op`, `failed`, or `missed`, so a healthy system
shows an empty strip and a broken one does not.
"""
from __future__ import annotations

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Status</title>
<style>
  :root {
    --bg:#0f1115; --panel:#171a21; --line:#252a34; --text:#e6e9ef;
    --dim:#8b93a3; --ok:#2ecc71; --noop:#f1c40f; --fail:#e74c3c;
    --miss:#e67e22; --skip:#5a6272; --run:#3498db; --unk:#7f8c8d;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:16px 20px; border-bottom:1px solid var(--line);
           background:var(--panel); position:sticky; top:0; z-index:10; }
  h1 { margin:0 0 4px; font-size:17px; font-weight:600; }
  .sub { color:var(--dim); font-size:12px; }
  .counts { display:flex; gap:14px; flex-wrap:wrap; margin-top:10px; }
  .chip { display:flex; align-items:center; gap:6px; font-size:12px;
          color:var(--dim); }
  .dot { width:9px; height:9px; border-radius:50%; display:inline-block; }
  .attention { margin-top:12px; padding:10px 12px; border-radius:6px;
               background:#2a1f14; border:1px solid #4a3520; font-size:13px; }
  .attention.ok { background:#14251a; border-color:#1f4a2c; }
  .attention b { color:var(--noop); }
  .attention.ok b { color:var(--ok); }
  .filters { display:flex; gap:10px; flex-wrap:wrap; padding:12px 20px;
             border-bottom:1px solid var(--line); background:var(--panel); }
  input, select, button { background:#0d0f13; color:var(--text);
    border:1px solid var(--line); border-radius:5px; padding:6px 9px;
    font-size:13px; font-family:inherit; }
  input[type=search] { min-width:200px; }
  button { cursor:pointer; }
  button:hover { border-color:#3a4150; }
  button.primary { background:#1d4ed8; border-color:#1d4ed8; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:8px 12px; border-bottom:1px solid var(--line);
           font-size:13px; vertical-align:top; }
  th { color:var(--dim); font-weight:500; font-size:11px;
       text-transform:uppercase; letter-spacing:.04em;
       position:sticky; top:0; background:var(--bg); }
  tbody tr:hover { background:#141821; cursor:pointer; }
  .name { font-weight:500; }
  .meta { color:var(--dim); font-size:11px; }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px;
           font-size:11px; font-weight:600; text-transform:uppercase;
           letter-spacing:.03em; }
  .b-ok{background:#14351f;color:var(--ok)} .b-no_op{background:#332c10;color:var(--noop)}
  .b-failed{background:#3a1a17;color:var(--fail)} .b-missed{background:#33220f;color:var(--miss)}
  .b-skipped{background:#1e222a;color:var(--skip)} .b-running{background:#12293a;color:var(--run)}
  .b-unknown{background:#22262e;color:var(--unk)}
  .rule { color:var(--dim); font-size:11px; }
  .rule.bad { color:var(--noop); }
  .disabled td { opacity:.45; }
  #detail { position:fixed; inset:0; background:rgba(0,0,0,.72);
            display:none; padding:40px 20px; overflow:auto; z-index:50; }
  #detail.open { display:block; }
  .card { max-width:900px; margin:0 auto; background:var(--panel);
          border:1px solid var(--line); border-radius:8px; padding:20px; }
  .card h2 { margin:0 0 4px; font-size:16px; }
  pre { background:#0b0d11; border:1px solid var(--line); border-radius:6px;
        padding:12px; overflow:auto; font-size:12px; line-height:1.5;
        max-height:340px; white-space:pre-wrap; word-break:break-word; }
  .close { float:right; }
  .empty { padding:40px; text-align:center; color:var(--dim); }
  .hist { padding:16px 20px; border-top:1px solid var(--line); }
  .hist h3 { margin:0 0 10px; font-size:14px; font-weight:600; }
  .hist table { font-size:12px; }
</style>
</head>
<body>

<header>
  <h1>Job Status</h1>
  <div class="sub" id="sub">loading…</div>
  <div class="counts" id="counts"></div>
  <div class="attention" id="attention" style="display:none"></div>
</header>

<div class="filters">
  <input type="search" id="q" placeholder="search job name…">
  <select id="category"><option value="">all categories</option></select>
  <select id="source"><option value="">all sources</option></select>
  <select id="outcome"><option value="">all outcomes</option></select>
  <label class="chip"><input type="checkbox" id="showDisabled" checked>
    show disabled</label>
  <button class="primary" id="refresh">Refresh now</button>
  <span class="sub" id="refreshNote"></span>
</div>

<table>
  <thead>
    <tr>
      <th style="width:26%">Job</th>
      <th style="width:14%">Schedule</th>
      <th style="width:9%">Outcome</th>
      <th style="width:8%">Work</th>
      <th style="width:17%">Last run</th>
      <th style="width:26%">Rule / note</th>
    </tr>
  </thead>
  <tbody id="rows"></tbody>
</table>

<div class="hist">
  <h3>History</h3>
  <div class="filters" style="padding:0 0 10px;border:0;background:none">
    <label class="chip">from <input type="date" id="since"></label>
    <label class="chip">to <input type="date" id="until"></label>
    <button id="loadHist">Load history</button>
    <span class="sub" id="histNote"></span>
  </div>
  <table>
    <thead><tr>
      <th style="width:26%">Job</th><th style="width:10%">Outcome</th>
      <th style="width:20%">Started</th><th style="width:10%">Duration</th>
      <th style="width:34%">Detail</th>
    </tr></thead>
    <tbody id="histRows"></tbody>
  </table>
</div>

<div id="detail"><div class="card">
  <button class="close" onclick="closeDetail()">close</button>
  <h2 id="dName"></h2>
  <div class="sub" id="dMeta"></div>
  <h3 style="font-size:13px;margin:16px 0 6px">Detail</h3>
  <pre id="dJson"></pre>
  <h3 style="font-size:13px;margin:16px 0 6px">Log excerpt</h3>
  <pre id="dLog"></pre>
</div></div>

<script>
const $ = id => document.getElementById(id);
let DATA = null;

function fmtLocal(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, {
    year:'numeric', month:'short', day:'2-digit',
    hour:'2-digit', minute:'2-digit', second:'2-digit'
  });
}

function ago(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}

function badge(o) {
  return '<span class="badge b-' + o + '">' + o.replace('_',' ') + '</span>';
}

function workCell(v) {
  if (v === 1 || v === true) return '<span style="color:var(--ok)">yes</span>';
  if (v === 0 || v === false) return '<span style="color:var(--noop)">NO</span>';
  return '<span class="meta">?</span>';
}

function render() {
  if (!DATA) return;
  const lc = DATA.last_collect;
  $('sub').textContent =
    DATA.total_jobs + ' jobs · showing ' + DATA.shown +
    (lc ? ' · last collected ' + fmtLocal(lc.finished_at || lc.started_at) +
          ' (' + ago(lc.finished_at || lc.started_at) + ')' : ' · never collected');

  const order = ['ok','no_op','failed','missed','running','skipped','unknown'];
  $('counts').innerHTML = order
    .filter(k => DATA.counts[k])
    .map(k => '<span class="chip"><span class="dot" style="background:var(--' +
              (k==='no_op'?'noop':k==='failed'?'fail':k==='missed'?'miss':
               k==='skipped'?'skip':k==='running'?'run':k==='unknown'?'unk':'ok') +
              ')"></span>' + k.replace('_',' ') + ' ' + DATA.counts[k] + '</span>')
    .join('');

  const att = $('attention');
  if (DATA.attention.length) {
    att.className = 'attention';
    att.style.display = 'block';
    att.innerHTML = '<b>' + DATA.attention.length + ' need attention:</b> ' +
      DATA.attention.map(a => a.name + ' (' + a.outcome.replace('_',' ') + ')').join(' · ');
  } else {
    att.className = 'attention ok';
    att.style.display = 'block';
    att.innerHTML = '<b>All clear.</b> No enabled job is failed, no-op, or missed.';
  }

  $('rows').innerHTML = DATA.jobs.map(j => {
    const cls = j.enabled ? '' : ' class="disabled"';
    const rule = j.rule_note || (j.has_rule ? '' : 'exit code only');
    const bad = j.outcome === 'no_op' || j.outcome === 'failed';
    return '<tr' + cls + ' onclick="openDetail(\'' + j.job_id + '\')">' +
      '<td><div class="name">' + j.name + '</div>' +
        '<div class="meta">' + j.source + ' · ' + j.category +
        (j.enabled ? '' : ' · disabled') + '</div></td>' +
      '<td class="meta">' + j.schedule + '<br>' + j.timezone + '</td>' +
      '<td>' + badge(j.outcome) + '</td>' +
      '<td>' + workCell(j.did_work) + '</td>' +
      '<td class="meta">' + fmtLocal(j.started_at) +
        (j.started_at ? '<br>' + ago(j.started_at) : '') + '</td>' +
      '<td class="rule' + (bad ? ' bad' : '') + '">' + (rule || '—') + '</td>' +
    '</tr>';
  }).join('') || '<tr><td colspan="6" class="empty">No jobs match the filter.</td></tr>';
}

function openDetail(jobId) {
  const j = DATA.jobs.find(x => x.job_id === jobId);
  if (!j) return;
  $('dName').textContent = j.name;
  $('dMeta').textContent = j.job_id + ' · ' + j.source + ' · ' + j.schedule +
    ' · ' + j.timezone + (j.notes ? ' · ' + j.notes : '');
  $('dJson').textContent = JSON.stringify(j.detail || {}, null, 2);
  $('dLog').textContent = 'loading…';
  $('detail').classList.add('open');
  // The log excerpt is fetched on demand -- sending all 32 up front made the
  // main payload 717 KB.
  fetch('/api/jobs/detail?job_id=' + encodeURIComponent(jobId))
    .then(r => r.json())
    .then(d => {
      $('dLog').textContent = d.log_excerpt || '(no log excerpt captured)';
      if (d.history && d.history.length) {
        $('dJson').textContent = JSON.stringify(
          {latest: j.detail || {}, recent_runs: d.history}, null, 2);
      }
    })
    .catch(e => { $('dLog').textContent = 'failed to load log: ' + e; });
}
function closeDetail() { $('detail').classList.remove('open'); }
$('detail').addEventListener('click', e => {
  if (e.target.id === 'detail') closeDetail();
});

function params() {
  const p = new URLSearchParams();
  if ($('q').value) p.set('q', $('q').value);
  if ($('category').value) p.set('category', $('category').value);
  if ($('source').value) p.set('source', $('source').value);
  if ($('outcome').value) p.set('outcome', $('outcome').value);
  if (!$('showDisabled').checked) p.set('include_disabled', '0');
  return p;
}

async function load() {
  try {
    const r = await fetch('/api/jobs?' + params().toString());
    DATA = await r.json();
    if (DATA.error) { $('sub').textContent = 'error: ' + DATA.error; return; }
    if (!DATA._populated) {
      const c = $('category');
      if (c.options.length === 1) {
        DATA.categories.forEach(v => c.add(new Option(v, v)));
        DATA.sources.forEach(v => $('source').add(new Option(v, v)));
        DATA.outcomes.forEach(v => $('outcome').add(new Option(v, v)));
      }
    }
    render();
  } catch (e) {
    $('sub').textContent = 'failed to load: ' + e;
  }
}

async function refreshNow() {
  $('refreshNote').textContent = 'collecting…';
  try {
    const r = await fetch('/api/jobs/collect');
    const j = await r.json();
    $('refreshNote').textContent = j.error
      ? 'error: ' + j.error
      : 'collected ' + j.runs_written + ' runs' +
        (j.errors && j.errors.length ? ' (' + j.errors.length + ' errors)' : '');
    await load();
  } catch (e) {
    $('refreshNote').textContent = 'failed: ' + e;
  }
}

async function loadHistory() {
  const p = new URLSearchParams();
  if ($('since').value) p.set('since', $('since').value);
  if ($('until').value) p.set('until', $('until').value);
  p.set('limit', '500');
  $('histNote').textContent = 'loading…';
  try {
    const r = await fetch('/api/jobs/history?' + p.toString());
    const d = await r.json();
    if (d.error) { $('histNote').textContent = 'error: ' + d.error; return; }
    $('histNote').textContent = d.count + ' runs';
    $('histRows').innerHTML = d.runs.map(x =>
      '<tr><td>' + x.name + '</td><td>' + badge(x.outcome) + '</td>' +
      '<td class="meta">' + fmtLocal(x.started_at) + '</td>' +
      '<td class="meta">' + (x.duration_s != null ? x.duration_s.toFixed(1) + 's' : '—') + '</td>' +
      '<td class="meta">' + JSON.stringify(x.detail || {}).slice(0, 120) + '</td></tr>'
    ).join('') || '<tr><td colspan="5" class="empty">No runs in this window.</td></tr>';
  } catch (e) {
    $('histNote').textContent = 'failed: ' + e;
  }
}

['q','category','source','outcome','showDisabled'].forEach(id => {
  $(id).addEventListener('change', load);
  if (id === 'q') $(id).addEventListener('input', () => {
    clearTimeout(window._t); window._t = setTimeout(load, 250);
  });
});
$('refresh').addEventListener('click', refreshNow);
$('loadHist').addEventListener('click', loadHistory);

// default history window: last 7 days
const today = new Date();
const weekAgo = new Date(Date.now() - 7*86400*1000);
$('until').value = today.toISOString().slice(0,10);
$('since').value = weekAgo.toISOString().slice(0,10);

load();
// The page is a report, not a live feed: refresh every 5 minutes so a tab
// left open does not go stale, but not so often that it is noise.
setInterval(load, 5 * 60 * 1000);
</script>
</body>
</html>
"""
