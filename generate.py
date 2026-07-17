#!/usr/bin/env python3
"""TokenTax report generator. Reads cached benchmark results and model metadata,
produces public/index.html (self-contained) and public/models-results.csv."""

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"
RESULTS_DIR = CACHE_DIR / "results"
MODELS_JSON = CACHE_DIR / "models.json"
FIXTURES_DIR = ROOT / "fixtures"
OUTPUT_DIR = ROOT / "public"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("tokentax-generate")


FIXTURE_DESCRIPTIONS = {
    "react-component.tsx": "A production-grade React data table component with TypeScript generics, sorting, filtering, pagination, row selection, accessibility, and loading/error/empty states.",
    "backend-api.py": "A FastAPI backend for project management with CRUD endpoints, JWT auth, pagination, filtering, request validation, background tasks, and a WebSocket endpoint.",
    "openapi-spec.json": "A complete OpenAPI 3.1.0 specification for a Task Manager API with 10 endpoints, security schemes, reusable schemas, and request/response examples.",
    "architecture.md": "A technical architecture document for a real-time collaboration platform covering CRDTs, operational transformation, scaling, disaster recovery, and observability.",
    "agent-system-prompt.txt": "A 28KB production system prompt for an AI coding agent with tool definitions, code style rules, security guidelines, and anti-pattern warnings.",
}


def load_results() -> list[dict]:
    rows = []
    if not RESULTS_DIR.exists():
        logger.warning("No results directory found.")
        return rows
    for model_dir in sorted(RESULTS_DIR.iterdir()):
        if not model_dir.is_dir():
            continue
        for rf in sorted(model_dir.iterdir()):
            if not rf.suffix == ".json":
                continue
            try:
                row = json.loads(rf.read_text())
                rows.append(row)
            except Exception as e:
                logger.warning(f"Failed to load {rf}: {e}")
    return rows


def load_models_cache() -> dict[str, dict]:
    if not MODELS_JSON.exists():
        return {}
    models = json.loads(MODELS_JSON.read_text())
    return {m["id"]: m for m in models}


def compute_metrics(results: list[dict], models_cache: dict[str, dict]) -> list[dict]:
    dashboard = []
    for r in results:
        mid = r["model_id"]
        if "-latest" in mid.lower():
            continue
        model_info = models_cache.get(mid, {})

        char_count = r.get("fixture_char_count", 0) or 1
        byte_count = r.get("fixture_byte_count", 0) or 1
        mb = max(1, byte_count) / (1024 * 1024)
        total_tokens = r["input_tokens_billed"] + r["output_tokens_billed"]
        reasoning = r.get("reasoning_tokens", 0) or 0
        mult_in = round(r["input_tokens_billed"] / char_count, 4)
        mult_out = round(r["output_tokens_billed"] / char_count, 4)

        billed = r.get("total_cost_billed", 0) or 0
        cost_per_mb = round(billed / mb, 6)

        eff_mtok = round(billed / (total_tokens / 1_000_000), 4) if total_tokens else 0

        pricing = model_info.get("pricing", {})
        list_price_in = float(pricing.get("prompt", 0) or 0) * 1_000_000
        list_price_out = float(pricing.get("completion", 0) or 0) * 1_000_000
        hypot_cost = (r["input_tokens_billed"] / 1_000_000) * list_price_in + \
                     (r["output_tokens_billed"] / 1_000_000) * list_price_out
        hypot_per_mb = round(hypot_cost / mb, 6)

        scores = model_info.get("benchmarks", {}).get("artificial_analysis", {})
        if isinstance(scores, list):
            scores = {}

        dashboard.append({
            "model_id": mid,
            "model_name": model_info.get("name", mid),
            "model_slug": mid,
            "model_created": model_info.get("created"),
            "intelligence_index": scores.get("intelligence_index"),
            "coding_index": scores.get("coding_index"),
            "agentic_index": scores.get("agentic_index"),
            "list_price_in": round(list_price_in, 4),
            "list_price_out": round(list_price_out, 4),
            "fixture_name": r["fixture_name"],
            "fixture_category": r["fixture_category"],
            "fixture_char_count": char_count,
            "input_tokens_billed": r["input_tokens_billed"],
            "output_tokens_billed": r["output_tokens_billed"],
            "reasoning_tokens": reasoning,
            "total_tokens_billed": total_tokens,
            "multiplier_input": mult_in,
            "multiplier_output": mult_out,
            "provider_served": r.get("provider_served") or "",
            "execution_duration_sec": r.get("execution_duration_sec", 0),
            "total_cost_billed": billed,
            "cost_per_mb": cost_per_mb,
            "hypothetical_cost_per_mb": hypot_per_mb,
            "effective_cost_per_mtok": eff_mtok,
            "ran_at": (r.get("ran_at") or "")[:16].replace("T", " "),
            "generation_id": r.get("generation_id") or "",
            "raw_response": r.get("raw_response") or "",
            "ran_at": (r.get("ran_at") or "")[:16].replace("T", " "),
        })

    dashboard.sort(key=lambda x: (x["model_slug"], x["fixture_name"]))
    return dashboard


def write_csv(dashboard: list[dict]):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_DIR / "models-results.csv"
    if not dashboard:
        csv_path.write_text("No data\n")
        return

    fields = [
        "model_name", "model_slug", "intelligence_index", "coding_index", "agentic_index",
        "list_price_in", "list_price_out",
        "fixture_name", "fixture_category", "fixture_char_count",
        "input_tokens_billed", "output_tokens_billed", "reasoning_tokens", "total_tokens_billed",
        "multiplier_input", "multiplier_output",
        "provider_served",
        "execution_duration_sec", "total_cost_billed", "cost_per_mb", "hypothetical_cost_per_mb", "effective_cost_per_mtok",
    ]

    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(dashboard)
    logger.info(f"Wrote {csv_path} ({len(dashboard)} rows)")


def write_html(dashboard: list[dict]):
    html_path = OUTPUT_DIR / "index.html"
    data_json = json.dumps(dashboard, indent=2, default=str)
    gen_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    fixtures = []
    for cat_dir in sorted(FIXTURES_DIR.iterdir()):
        if not cat_dir.is_dir():
            continue
        for fpath in sorted(cat_dir.iterdir()):
            if not fpath.is_file():
                continue
            content = fpath.read_text(encoding="utf-8")
            fixtures.append({
                "name": fpath.name,
                "category": cat_dir.name,
                "description": FIXTURE_DESCRIPTIONS.get(fpath.name, ""),
                "raw_content": content,
                "char_count": len(content),
                "byte_count": len(content.encode("utf-8")),
            })
    fixtures_json = json.dumps(fixtures, indent=2)

    html = HTML_TEMPLATE.replace("__DATA_JSON__", data_json).replace("__FIXTURES_JSON__", fixtures_json).replace("__GEN_AT__", gen_at)
    html_path.write_text(html)
    logger.info(f"Wrote {html_path}")


def main():
    results = load_results()
    if not results:
        logger.warning("No benchmark results found in cache/results/")
        # Still generate empty HTML + CSV
        dashboard = []
    else:
        models_cache = load_models_cache()
        dashboard = compute_metrics(results, models_cache)
        logger.info(f"Loaded {len(results)} results across {len(set(r['model_id'] for r in results))} models")

    write_csv(dashboard)
    write_html(dashboard)
    logger.info("Done.")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" class="bg-gray-950 text-gray-100">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LLMs Do — Give an LLM a thing and it costs you tokens and money</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>tailwind.config={theme:{extend:{colors:{brand:{50:'#eef2ff',500:'#6366f1',600:'#4f46e5',700:'#4338ca'}}}}}</script>
</head>
<body class="min-h-screen">

<div id="app"></div>

<script>
var DASHBOARD = __DATA_JSON__;
var FIXTURES = __FIXTURES_JSON__;
var GEN_AT = '__GEN_AT__';

(function() {
  var root = document.getElementById('app');
  var tab = 'dashboard';
  var expandedRow = null;
  var expandedFixture = null;
  var filterModel = '';
  var filterFixture = '__summary__';
  var filterSearch = '';
  var sortCol = 'model_created';
  var sortDir = 'asc';

  function ageStr(ts) {
    if (!ts) return '-';
    var created = new Date(ts * 1000);
    var now = new Date();
    var months = (now.getFullYear() - created.getFullYear()) * 12 + now.getMonth() - created.getMonth();
    if (months < 1) return '<1 month';
    if (months < 12) return months + ' months';
    var years = Math.floor(months / 12);
    var rem = months % 12;
    return rem ? years + 'y ' + rem + 'm' : years + ' years';
  }

  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === 'className') el.className = attrs[k];
      else if (k.startsWith('on')) el.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
      else el.setAttribute(k, attrs[k]);
    }
    if (children) {
      if (typeof children === 'string') el.textContent = children;
      else if (Array.isArray(children)) children.forEach(function(c) { if (c) el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
      else el.appendChild(typeof children === 'string' ? document.createTextNode(children) : children);
    }
    return el;
  }

  function fmt(n, d) { d = d || 2; return typeof n === 'number' ? n.toFixed(d) : '-'; }
  function fmti(n) { return typeof n === 'number' ? n.toLocaleString() : '-'; }
  function scoreClass(v) {
    if (v == null) return 'text-gray-600';
    if (v >= 50) return 'text-green-400';
    if (v >= 25) return 'text-amber-400';
    return 'text-red-400';
  }
  function multClass(v) {
    if (v <= 0) return 'text-gray-600';
    if (v < 1.5) return 'text-green-400';
    if (v < 3.0) return 'text-amber-400';
    return 'text-red-400';
  }
  function costClass(v) {
    if (v <= 0) return 'text-gray-600';
    if (v > 0.01) return 'text-red-400';
    if (v > 0.001) return 'text-amber-400';
    return 'text-green-400';
  }

  var TT = 'title';

  function renderTooltip(label, tooltip) {
    return label;
  }

  function th(cls, label, tooltip) {
    var attrs = {className: cls};
    if (tooltip) attrs.title = tooltip;
    return h('th', attrs, label);
  }

  function renderHeader() {
    return h('header', {className: 'flex items-center justify-between mb-8'}, [
      h('div', {className: 'flex items-center gap-3'}, [
        h('div', {className: 'h-10 w-10 rounded-lg bg-brand-600 flex items-center justify-center text-lg font-bold'}, 'LD'),
        h('div', null, [
          h('h1', {className: 'text-2xl font-bold tracking-tight'}, 'LLMs Do'),
          h('p', {className: 'text-sm text-gray-400'}, 'Give an LLM a thing and it costs you tokens and money'),
        ]),
      ]),
      h('div', {className: 'text-xs text-gray-500 text-right'}, [
        h('div', null, 'Generated: ' + GEN_AT),
      ]),
    ]);
  }

  function renderTabs() {
    return h('nav', {className: 'flex gap-1 mb-6 border-b border-gray-800'}, [
      h('button', {
        className: 'px-4 py-2 text-sm font-medium border-b-2 transition-colors ' + (tab === 'dashboard' ? 'border-brand-500 text-white' : 'border-transparent text-gray-400 hover:text-gray-200'),
        onclick: function() { tab = 'dashboard'; render(); }
      }, 'Dashboard'),
      h('button', {
        className: 'px-4 py-2 text-sm font-medium border-b-2 transition-colors ' + (tab === 'fixtures' ? 'border-brand-500 text-white' : 'border-transparent text-gray-400 hover:text-gray-200'),
        onclick: function() { tab = 'fixtures'; render(); }
      }, 'Fixtures'),
      h('button', {
        className: 'px-4 py-2 text-sm font-medium border-b-2 transition-colors ' + (tab === 'about' ? 'border-brand-500 text-white' : 'border-transparent text-gray-400 hover:text-gray-200'),
        onclick: function() { tab = 'about'; render(); }
      }, 'About'),
    ]);
  }

  function renderDashboard() {
    var models = [];
    var seen = {};
    DASHBOARD.forEach(function(r) {
      if (!seen[r.model_slug]) { seen[r.model_slug] = true; models.push({id: r.model_id, slug: r.model_slug, name: r.model_name}); }
    });
    var fixtureNames = [];
    seen = {};
    DASHBOARD.forEach(function(r) {
      if (!seen[r.fixture_name]) { seen[r.fixture_name] = true; fixtureNames.push({name: r.fixture_name, category: r.fixture_category}); }
    });

    var filtered = DASHBOARD;
    if (filterModel) filtered = filtered.filter(function(r) { return r.model_slug === filterModel; });
    if (filterSearch) {
      var q = filterSearch.toLowerCase();
      filtered = filtered.filter(function(r) { return r.model_name.toLowerCase().indexOf(q) !== -1 || r.model_slug.toLowerCase().indexOf(q) !== -1; });
    }

    var showDetail = filterFixture !== '__summary__';
    if (filterFixture !== '__summary__' && filterFixture !== '__all__') {
      filtered = filtered.filter(function(r) { return r.fixture_name === filterFixture; });
    }

    var summaryData = (filterFixture === '__summary__') ? (filterModel || filterSearch ? filtered : DASHBOARD)
        : filtered;

    if (sortCol) {
      filtered = filtered.slice().sort(function(a, b) {
        var va = a[sortCol], vb = b[sortCol];
        if (va == null && vb == null) return 0;
        if (va == null) return 1;
        if (vb == null) return -1;
        if (typeof va === 'string') va = va.toLowerCase(), vb = vb.toLowerCase();
        return va < vb ? (sortDir === 'asc' ? -1 : 1) : va > vb ? (sortDir === 'asc' ? 1 : -1) : 0;
      });
    }

    var children = [];

    // Filters
    var filters = h('div', {className: 'flex flex-wrap gap-3 mb-4'});
    var selM = h('select', {className: 'bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm'});
    selM.onchange = function() { filterModel = this.value; render(); };
    selM.appendChild(h('option', {value: ''}, 'All Models (' + models.length + ')'));
    models.forEach(function(m) {
      var opt = h('option', {value: m.slug}, m.name + ' (' + m.slug + ')');
      if (filterModel === m.slug) opt.selected = true;
      selM.appendChild(opt);
    });
    filters.appendChild(selM);

    var selF = h('select', {className: 'bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm'});
    selF.onchange = function() { filterFixture = this.value; expandedRow = null; render(); };
    var sumOpt = h('option', {value: '__summary__'}, 'Summary (per model)');
    sumOpt.selected = filterFixture === '__summary__';
    selF.appendChild(sumOpt);
    var allOpt = h('option', {value: '__all__'}, 'All Fixtures (' + fixtureNames.length + ')');
    allOpt.selected = filterFixture === '__all__';
    selF.appendChild(allOpt);
    fixtureNames.forEach(function(f) {
      var opt = h('option', {value: f.name}, f.name + ' [' + f.category + ']');
      if (filterFixture === f.name) opt.selected = true;
      selF.appendChild(opt);
    });
    filters.appendChild(selF);

    var searchBox = h('input', {type: 'text', placeholder: 'Search models...', id: 'model-search', className: 'bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm w-48'});
    searchBox.value = filterSearch || '';
    searchBox.oninput = function() { filterSearch = this.value; render(); };
    filters.appendChild(searchBox);
    filters.appendChild(h('span', {className: 'text-xs text-gray-500 self-center ml-auto'}, filtered.length + ' rows'));
    children.push(filters);

    // Table
    var table = h('div', {className: 'overflow-x-auto rounded-lg border border-gray-800'});
    var tbl = h('table', {className: 'w-full text-sm'});

    var thead = h('thead', {className: 'bg-gray-900 text-gray-400 text-xs tracking-wider'});
    function hdr(col, label, tooltip) {
      var cls = 'px-2 py-2 whitespace-normal cursor-pointer hover:text-gray-200 select-none';
      var arrow = '';
      if (sortCol === col) arrow = sortDir === 'asc' ? ' \u25B2' : ' \u25BC';
      var attrs = {className: cls};
      if (tooltip) attrs.title = tooltip;
      var el = h('th', attrs, label + arrow);
      el.addEventListener('click', function() {
        if (sortCol === col) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
        else { sortCol = col; sortDir = 'asc'; }
        render();
      });
      return el;
    }
    var tr = h('tr', null, [
      hdr('model_name', 'Model'),
      hdr('model_created', 'Age'),
      hdr('fixture_name', 'Fixture'),
      hdr('intelligence_index', 'Int', 'Intelligence score from OpenRouter'),
      hdr('coding_index', 'Code', 'Coding score from OpenRouter'),
      hdr('agentic_index', 'Agent', 'Agentic score from OpenRouter'),
      hdr('list_price_in', '$/Mtok In', 'Published list price per million input tokens'),
      hdr('list_price_out', '$/Mtok Out', 'Published list price per million output tokens'),
      hdr('input_tokens_billed', 'In Tok'),
      hdr('output_tokens_billed', 'Out Tok'),
      hdr('reasoning_tokens', 'Reasoning', 'Hidden reasoning/CoT tokens billed separately'),
      hdr('multiplier_input', 'Mult In', 'Input tokens billed divided by characters in fixture. Higher = less efficient tokenizer.'),
      hdr('multiplier_output', 'Mult Out', 'Output tokens billed divided by characters in fixture.'),
      hdr('provider_served', 'Provider'),
      hdr('execution_duration_sec', 'Time'),
      hdr('cost_per_mb', 'Cost/MB', 'Actual billed cost per megabyte of source data. $0 = free model.'),
      hdr('hypothetical_cost_per_mb', 'Hypoth $/MB', 'What this would cost at list price per MB, ignoring free credits.'),
      hdr('effective_cost_per_mtok', 'Eff $/Mtok', 'Effective cost per million tokens from this run. Accounts for provider-specific pricing.'),
      hdr('total_cost_billed', 'Total $', 'Actual USD billed by OpenRouter for this run.'),
      hdr('ran_at', 'Ran at', 'When this benchmark was executed.'),
    ]);
    thead.appendChild(tr);
    tbl.appendChild(thead);

    var tbody = h('tbody', {className: 'divide-y divide-gray-800'});
    if (showDetail) {
    filtered.forEach(function(row, i) {
      var r = h('tr', {
        className: 'hover:bg-gray-900/50 transition-colors cursor-pointer' + (expandedRow === i ? ' bg-gray-900/70' : ''),
        onclick: function() { expandedRow = expandedRow === i ? null : i; render(); }
      }, [
        h('td', {className: 'px-3 py-2.5'}, [
          h('div', {className: 'font-medium text-xs'}, row.model_name),
          h('div', {className: 'text-xs text-gray-500'}, row.model_slug),
        ]),
        h('td', {className: 'px-3 py-2.5 text-xs text-gray-400'}, ageStr(row.model_created)),
        h('td', {className: 'px-3 py-2.5'}, [
          h('div', {className: 'text-xs'}, row.fixture_name),
          h('div', {className: 'text-xs text-gray-500'}, row.fixture_category + ' \u00b7 ' + fmti(row.fixture_char_count) + ' chars'),
        ]),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums text-xs ' + scoreClass(row.intelligence_index)}, row.intelligence_index != null ? row.intelligence_index.toFixed(1) : '-'),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums text-xs ' + scoreClass(row.coding_index)}, row.coding_index != null ? row.coding_index.toFixed(1) : '-'),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums text-xs ' + scoreClass(row.agentic_index)}, row.agentic_index != null ? row.agentic_index.toFixed(1) : '-'),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums text-xs text-gray-400 font-mono'}, '$' + fmt(row.list_price_in, 2)),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums text-xs text-gray-400 font-mono'}, '$' + fmt(row.list_price_out, 2)),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums text-xs'}, fmti(row.input_tokens_billed)),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums text-xs'}, fmti(row.output_tokens_billed)),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums text-xs text-amber-400'}, row.reasoning_tokens ? fmti(row.reasoning_tokens) : '-'),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums text-xs ' + multClass(row.multiplier_input)}, fmt(row.multiplier_input, 3)),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums text-xs ' + multClass(row.multiplier_output)}, fmt(row.multiplier_output, 3)),
        h('td', {className: 'px-3 py-2.5 text-xs'}, row.provider_served || h('span', {className: 'text-gray-600'}, 'unknown')),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums text-xs text-gray-400'}, row.execution_duration_sec.toFixed(1) + 's'),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums font-mono text-xs ' + costClass(row.cost_per_mb)}, '$' + fmt(row.cost_per_mb, 4)),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums font-mono text-xs text-gray-400'}, '$' + fmt(row.hypothetical_cost_per_mb, 4)),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums font-mono text-xs ' + costClass(row.effective_cost_per_mtok)}, '$' + fmt(row.effective_cost_per_mtok, 4)),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums font-mono text-xs'}, '$' + fmt(row.total_cost_billed, 6)),
        h('td', {className: 'px-3 py-2.5 text-right tabular-nums text-xs text-gray-500'}, row.ran_at || '-'),
      ]);
      tbody.appendChild(r);

      if (expandedRow === i) {
        var detail = h('tr', {className: 'bg-gray-900/70'});
        var detailTd = h('td', {colSpan: '23', className: 'px-4 py-3'}, [
          h('div', {className: 'grid grid-cols-2 lg:grid-cols-4 gap-3 text-xs mb-3'}, [
            h('div', null, [h('span', {className: 'text-gray-500'}, 'Generation ID: '), h('code', {className: 'text-gray-300 font-mono'}, row.generation_id)]),
            h('div', null, [h('span', {className: 'text-gray-500'}, 'Input tokens: '), h('span', {className: 'text-gray-300'}, fmti(row.input_tokens_billed))]),
            h('div', null, [h('span', {className: 'text-gray-500'}, 'Output tokens: '), h('span', {className: 'text-gray-300'}, fmti(row.output_tokens_billed))]),
          ]),
          h('details', null, [
            h('summary', {className: 'text-xs text-brand-500 cursor-pointer hover:text-brand-400'}, 'Show LLM response'),
            row.raw_response
              ? h('div', {className: 'text-xs text-gray-300 overflow-auto max-h-96 border border-gray-700 rounded p-3 mt-2 prose prose-sm prose-invert', id: 'resp-' + i}, '')
              : h('p', {className: 'text-xs text-gray-500 italic mt-2'}, 'No visible output \u2014 all tokens spent on internal reasoning.'),
            row.raw_response ? h('script', null, 'document.getElementById("resp-' + i + '").innerHTML = marked.parse(' + JSON.stringify(row.raw_response) + ');') : null,
          ]),
          h('details', null, [
            h('summary', {className: 'text-xs text-brand-500 cursor-pointer hover:text-brand-400'}, 'Show raw LLM response'),
            row.raw_response
              ? h('pre', {className: 'text-xs text-gray-400 font-mono overflow-auto max-h-96 whitespace-pre-wrap border border-gray-700 rounded p-3 mt-2'}, row.raw_response)
              : h('p', {className: 'text-xs text-gray-500 italic mt-2'}, 'No output. Reasoning-only model \u2014 visible content was null.'),
          ]),
        ]);
        detail.appendChild(detailTd);
        tbody.appendChild(detail);
      }
    });
    }
    tbl.appendChild(tbody);

    // Per-model summary rows
    if (filtered.length > 0 && !showDetail) {
      var modelGroups = {};
      filtered.forEach(function(row) {
        if (!modelGroups[row.model_slug]) {
          modelGroups[row.model_slug] = { name: row.model_name, slug: row.model_slug, created: row.model_created,
            li: row.list_price_in, lo: row.list_price_out,
            intel: row.intelligence_index, code: row.coding_index, agent: row.agentic_index,
            totalIn: 0, totalOut: 0, totalReasoning: 0, totalCost: 0, totalDuration: 0, count: 0, sumMultIn: 0, sumMultOut: 0 };
        }
        var g = modelGroups[row.model_slug];
        g.totalIn += row.input_tokens_billed;
        g.totalOut += row.output_tokens_billed;
        g.totalReasoning += (row.reasoning_tokens || 0);
        g.totalCost += row.total_cost_billed;
        g.totalDuration += row.execution_duration_sec;
        g.sumMultIn += row.multiplier_input;
        g.sumMultOut += row.multiplier_output;
        g.count += 1;
      });

      var tfoot = h('tfoot', {className: 'border-t-2 border-brand-600'});
      Object.values(modelGroups).forEach(function(g) {
        var avgMultIn = g.sumMultIn / g.count;
        var avgMultOut = g.sumMultOut / g.count;
        var totalTokens = g.totalIn + g.totalOut;
        var effMtok = totalTokens > 0 ? g.totalCost / (totalTokens / 1000000) : 0;
        var tr = h('tr', {className: 'bg-brand-600/10'}, [
          h('td', {className: 'px-3 py-2 font-medium text-xs text-brand-400'}, g.name),
          h('td', {className: 'px-3 py-2 text-xs text-brand-300'}, ageStr(g.created)),
          h('td', {className: 'px-3 py-2 text-xs text-brand-400'}, 'TOTAL (' + g.count + ' fixtures)'),
          h('td', {className: 'px-3 py-2 text-right tabular-nums text-xs ' + scoreClass(g.intel)}, g.intel != null ? g.intel.toFixed(1) : '-'),
          h('td', {className: 'px-3 py-2 text-right tabular-nums text-xs ' + scoreClass(g.code)}, g.code != null ? g.code.toFixed(1) : '-'),
          h('td', {className: 'px-3 py-2 text-right tabular-nums text-xs ' + scoreClass(g.agent)}, g.agent != null ? g.agent.toFixed(1) : '-'),
          h('td', {className: 'px-3 py-2 text-right tabular-nums text-xs text-gray-400 font-mono'}, '$' + fmt(g.li, 2)),
          h('td', {className: 'px-3 py-2 text-right tabular-nums text-xs text-gray-400 font-mono'}, '$' + fmt(g.lo, 2)),
          h('td', {className: 'px-3 py-2 text-right tabular-nums text-xs font-bold text-brand-400'}, fmti(g.totalIn)),
          h('td', {className: 'px-3 py-2 text-right tabular-nums text-xs font-bold text-brand-400'}, fmti(g.totalOut)),
          h('td', {className: 'px-3 py-2 text-right tabular-nums text-xs font-bold text-brand-400'}, g.totalReasoning ? fmti(g.totalReasoning) : '-'),
          h('td', {className: 'px-3 py-2 text-right tabular-nums text-xs ' + multClass(avgMultIn)}, fmt(avgMultIn, 3)),
          h('td', {className: 'px-3 py-2 text-right tabular-nums text-xs ' + multClass(avgMultOut)}, fmt(avgMultOut, 3)),
          h('td', {className: 'px-3 py-2 text-xs text-gray-500 italic'}, ''),
          h('td', {className: 'px-3 py-2 text-right tabular-nums text-xs text-gray-400'}, g.totalDuration.toFixed(1) + 's'),
          h('td', {className: 'px-3 py-2 text-xs'}),
          h('td', {className: 'px-3 py-2 text-xs'}),
          h('td', {className: 'px-3 py-2 text-right tabular-nums font-mono text-xs font-bold text-brand-400'}, '$' + fmt(effMtok, 4)),
          h('td', {className: 'px-3 py-2 text-right tabular-nums font-mono text-xs font-bold text-brand-400'}, '$' + fmt(g.totalCost, 6)),
          h('td', {className: 'px-3 py-2 text-xs text-gray-500'}, ''),
        ]);
        tfoot.appendChild(tr);
      });
      tbl.appendChild(tfoot);
    }

    if (filtered.length === 0) {
      var empty = h('tr', null, [h('td', {colSpan: '23', className: 'px-3 py-8 text-center text-gray-500'}, 'No benchmark data. Run python run.py to collect results.')]);
      tbody.appendChild(empty);
    }

    table.appendChild(tbl);
    children.push(table);
    return h('div', null, children);
  }

  function renderFixtures() {
    return h('div', {className: 'bg-gray-900 border border-gray-800 rounded-lg p-4'}, [
      h('h3', {className: 'font-medium mb-3'}, 'Test Fixtures (' + FIXTURES.length + ')'),
      h('p', {className: 'text-sm text-gray-500 mb-4'}, 'Fixtures are static test files that every model is benchmarked against. Click to view content.'),
      h('div', {className: 'space-y-2'}, FIXTURES.map(function(f, i) {
        var isOpen = expandedFixture === i;
        return h('div', null, [
          h('div', {
            className: 'flex items-center justify-between bg-gray-800/50 rounded-lg px-3 py-2 cursor-pointer hover:bg-gray-800 transition-colors',
            onclick: function() { expandedFixture = isOpen ? null : i; render(); }
          }, [
            h('div', null, [
              h('span', {className: 'font-medium text-sm'}, f.name),
              h('span', {className: 'text-xs bg-gray-700 px-1.5 py-0.5 rounded ml-2'}, f.category),
              h('span', {className: 'text-xs text-gray-500 ml-2'}, fmti(f.char_count) + ' chars \u00b7 ' + fmti(f.byte_count) + ' bytes'),
            ]),
            h('span', {className: 'text-xs text-gray-500'}, isOpen ? '\u25B2' : '\u25BC'),
          ]),
          isOpen ? h('div', {className: 'mt-2 bg-gray-800 rounded-lg p-4'}, [
            h('p', {className: 'text-sm text-gray-300 mb-3'}, f.description || 'No description.'),
            h('pre', {className: 'text-xs text-gray-400 font-mono overflow-auto max-h-96 whitespace-pre-wrap border border-gray-700 rounded p-3'}, f.raw_content),
          ]) : null,
        ]);
      })),
    ]);
  }

  function renderAbout() {
    return h('div', {className: 'bg-gray-900 border border-gray-800 rounded-lg p-6 max-w-2xl'}, [
      h('h2', {className: 'text-lg font-medium mb-4'}, 'About LLMs Do'),
      h('div', {className: 'space-y-3 text-sm text-gray-300'}, [
        h('p', null, ['I\'m indecisive about committing to a frontier model subscription. I use OpenCode and OpenRouter, saw the ',
          h('a', {href: 'https://playcode.io/blog/real-price-of-frontier-models', className: 'text-brand-500 hover:underline', target: '_blank'}, 'PlayCode article'),
          ' on the real cost of tokenization across models, and wanted to measure more models.']),
        h('p', null, 'This dashboard benchmarks the same static files against multiple models via OpenRouter, tracking token counts, actual billed costs, and which provider served each request. The thesis: $/Mtok is an illusion because different models split identical text into wildly different token counts.'),
        h('p', null, ['Models are benchmarked once and results cached. Add models to ',
          h('code', {className: 'bg-gray-800 px-1 py-0.5 rounded text-xs'}, 'models.txt'),
          ', run ',
          h('code', {className: 'bg-gray-800 px-1 py-0.5 rounded text-xs'}, 'python run.py'),
          ', then ',
          h('code', {className: 'bg-gray-800 px-1 py-0.5 rounded text-xs'}, 'python generate.py'),
          '.']),
      ]),
    ]);
  }

  function renderFooter() {
    return h('footer', {className: 'mt-8 pt-4 border-t border-gray-800 text-center text-xs text-gray-600'}, [
      '\u00a9 ' + new Date().getFullYear() + ' ',
      h('a', {href: 'https://github.com/mattgill/llms-do', className: 'hover:text-gray-400', target: '_blank'}, 'LLMs-Do'),
      ' \u00b7 ',
      h('a', {href: 'models-results.csv', className: 'hover:text-gray-400'}, 'Download CSV'),
    ]);
  }

  function render() {
    var active = document.activeElement;
    var fid = active ? active.id : null;
    var cursor = active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA') ? active.selectionStart : null;
    var container = h('div', {className: 'max-w-[95vw] mx-auto px-4 py-6'}, [
      renderHeader(),
      renderTabs(),
      tab === 'dashboard' ? renderDashboard() : tab === 'fixtures' ? renderFixtures() : renderAbout(),
      renderFooter(),
    ]);
    root.innerHTML = '';
    root.appendChild(container);
    if (fid) {
      var el = document.getElementById(fid);
      if (el) { el.focus(); if (cursor != null) { el.selectionStart = cursor; el.selectionEnd = cursor; } }
    }
  }

  render();
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
