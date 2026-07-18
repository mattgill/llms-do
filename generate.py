#!/usr/bin/env python3
"""TokenTax report generator. Reads cached benchmark results and model metadata,
produces the dashboard HTML, stylesheet, and CSV under public/."""

import csv
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"
RESULTS_DIR = CACHE_DIR / "results"
MODELS_JSON = CACHE_DIR / "models.json"
FIXTURES_DIR = ROOT / "fixtures"
OUTPUT_DIR = ROOT / "public"
ASSETS_DIR = ROOT / "assets"
CSS_SOURCE = ASSETS_DIR / "dashboard.css"
JS_SOURCE = ASSETS_DIR / "dashboard.js"

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


def extract_complexity_notes(model_response: str) -> str:
    """Extract the model-generated narrative from its JSON response."""
    candidate = (model_response or "").strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(candidate)
    except (TypeError, json.JSONDecodeError):
        return ""
    return parsed.get("complexity_notes", "") if isinstance(parsed, dict) else ""


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
            "model_response": r.get("raw_response") or "",
            "complexity_notes": extract_complexity_notes(r.get("raw_response") or ""),
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
    gen_at = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M %Z")

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


def write_assets():
    """Copy editable dashboard assets into the published site."""
    assets_path = OUTPUT_DIR / "assets"
    assets_path.mkdir(parents=True, exist_ok=True)
    for source in (CSS_SOURCE, JS_SOURCE):
        output_path = assets_path / source.name
        shutil.copyfile(source, output_path)
        logger.info(f"Wrote {output_path}")


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
    write_assets()
    write_html(dashboard)
    logger.info("Done.")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" class="bg-gray-950 text-gray-100">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LLMs Do — Give an LLM a thing and it costs you tokens and money</title>
<script>tailwind.config={theme:{extend:{colors:{brand:{50:'#f8f8f2',500:'#66d9ef',600:'#a6e22e',700:'#8fbf24'}}}}}</script>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<link rel="stylesheet" href="assets/dashboard.css">
</head>
<body class="min-h-screen">

<div id="app"></div>

<script>
var DASHBOARD = __DATA_JSON__;
var FIXTURES = __FIXTURES_JSON__;
var GEN_AT = '__GEN_AT__';
</script>
<script src="assets/dashboard.js"></script>
</body>
</html>"""


if __name__ == "__main__":
    main()
