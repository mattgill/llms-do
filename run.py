#!/usr/bin/env python3
"""TokenTax benchmark runner. Hits OpenRouter chat completions, fetches provider + billing,
caches results to disk. Skips any model x fixture combination already cached."""

import argparse
import asyncio
import calendar
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("OPENROUTER_API_KEY", "")
BASE_URL = "https://openrouter.ai/api/v1"
TIMEOUT = 120
CONCURRENCY = int(os.getenv("BENCHMARK_CONCURRENCY", "3"))
ROOT = Path(__file__).resolve().parent
FIXTURES_DIR = ROOT / "fixtures"
CACHE_DIR = ROOT / "cache"
RESULTS_DIR = CACHE_DIR / "results"
MODELS_JSON = CACHE_DIR / "models.json"
MODELS_TXT = ROOT / "models.txt"

SYSTEM_PROMPT = (
    "You are a precise code analysis tool. Your task is to analyze the provided file content "
    "and output a JSON summary with the following fields: "
    '{"file_type": "type", "line_count": N, "functions": [...], "classes": [...], '
    '"imports": [...], "complexity_notes": "..."}. '
    "If the content is not code, output a JSON summary describing the text structure instead. "
    "Output ONLY valid JSON, no markdown wrapping."
)

USER_TEMPLATE = "Analyze the following file content and output a JSON summary as specified:\n\n{content}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("tokentax")

_semaphore = asyncio.Semaphore(CONCURRENCY)


def safe_slug(model_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", model_id)


def _is_paid_text(model: dict) -> bool:
    p = model.get("pricing", {})
    prompt = float(p.get("prompt", 0) or 0)
    completion = float(p.get("completion", 0) or 0)
    if prompt <= 0 or completion <= 0:
        return False
    modality = model.get("architecture", {}).get("modality", "")
    output_modality = modality.split("->")[-1] if "->" in modality else modality
    if "image" in output_modality or "audio" in output_modality:
        return False
    return "text" in output_modality


def _is_recent(model: dict, cutoff: int | None = None) -> bool:
    created = model.get("created")
    if created is None:
        return False
    if cutoff is None:
        cutoff = calendar.timegm((2026, 1, 1, 0, 0, 0))
    return created >= cutoff


def _is_variant(model_id: str) -> bool:
    mid = model_id.lower()
    if "-pro" in mid or mid.endswith("-pro"):
        return True
    if "-fast" in mid:
        return True
    if mid.startswith("openai/o1") or mid.startswith("openai/o3"):
        return True
    if "deep-research" in mid:
        return True
    if "-latest" in mid or mid.endswith("-latest"):
        return True
    return False


def get_headers():
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def discover_fixtures() -> list[dict]:
    fixtures = []
    for cat_dir in sorted(FIXTURES_DIR.iterdir()):
        if not cat_dir.is_dir():
            continue
        category = cat_dir.name
        for fpath in sorted(cat_dir.iterdir()):
            if not fpath.is_file():
                continue
            content = fpath.read_text(encoding="utf-8")
            fixtures.append({
                "name": fpath.name,
                "category": category,
                "path": str(fpath),
                "raw_content": content,
                "char_count": len(content),
                "byte_count": len(content.encode("utf-8")),
            })
    return fixtures


async def fetch_models() -> list[dict]:
    logger.info("Fetching OpenRouter model list...")
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(f"{BASE_URL}/models", headers=get_headers())
        r.raise_for_status()
        data = r.json().get("data", [])
    MODELS_JSON.parent.mkdir(parents=True, exist_ok=True)
    MODELS_JSON.write_text(json.dumps(data, indent=2))
    logger.info(f"Saved {len(data)} models to {MODELS_JSON}")
    return data


def load_models_cache() -> dict[str, dict]:
    if not MODELS_JSON.exists():
        logger.warning("No models cache found. Run with --fetch-models first.")
        return {}
    models = json.loads(MODELS_JSON.read_text())
    return {m["id"]: m for m in models}


async def fetch_generation_stats(client: httpx.AsyncClient, gen_id: str) -> tuple:
    try:
        r = await client.get(f"{BASE_URL}/generation", params={"id": gen_id}, headers=get_headers())
        if r.status_code == 404:
            logger.debug(f"Generation stats not yet available for {gen_id}")
            return None, 0.0, None
        r.raise_for_status()
        gen = r.json().get("data", {})
        provider = gen.get("provider_name")
        cost = float(gen.get("total_cost", 0) or 0)
        tokenizer = gen.get("tokenizer")
        return provider, cost, tokenizer
    except Exception as e:
        logger.debug(f"Failed to fetch generation stats for {gen_id}: {e}")
        return None, 0.0, None


async def run_single(model_id: str, fixture: dict) -> dict | None:
    async with _semaphore:
        await asyncio.sleep(random.uniform(0, 0.5))
        slug = safe_slug(model_id)
        result_dir = RESULTS_DIR / slug
        result_dir.mkdir(parents=True, exist_ok=True)
        result_file = result_dir / (fixture["name"] + ".json")
        dead_file = result_dir / "_DEAD"

        if dead_file.exists():
            return None

        if result_file.exists():
            logger.info(f"SKIP {model_id} x {fixture['name']} (cached)")
            return None

        logger.info(f"RUN  {model_id} x {fixture['name']}")
        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_TEMPLATE.format(content=fixture["raw_content"])},
            ],
            "temperature": 0.0,
            "max_tokens": 4096,
        }

        start = time.time()
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for attempt in range(4):
                try:
                    resp = await client.post(f"{BASE_URL}/chat/completions", headers=get_headers(), json=payload)
                    if resp.status_code == 403:
                        body = resp.json()
                        msg = body.get("error", {}).get("message", "")
                        if "limit exceeded" in msg.lower() or "credits" in msg.lower():
                            logger.error(f"BUDGET {model_id}: {msg}")
                            raise SystemExit(1)
                        logger.warning(f"DEAD {model_id}: {msg}, marking as dead")
                        dead_file.write_text(msg)
                        return None
                    if resp.status_code == 429:
                        wait = 2 ** attempt
                        logger.warning(f"RATE-LIMITED {model_id} x {fixture['name']} (attempt {attempt + 1}/4, waiting {wait}s)")
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    break
                except (httpx.HTTPError) as e:
                    if attempt < 3 and (hasattr(e, 'response') and e.response is not None and e.response.status_code >= 500):
                        await asyncio.sleep(2 ** attempt)
                        continue
                    logger.error(f"FAIL {model_id} x {fixture['name']}: {e}")
                    return None
            else:
                logger.error(f"FAIL {model_id} x {fixture['name']}: rate-limited after 4 retries")
                return None

            data = resp.json()
            elapsed = time.time() - start

            generation_id = data.get("id")
            provider = data.get("provider")
            choice = data.get("choices", [{}])[0]
            raw_response = choice.get("message", {}).get("content", "")
            usage = data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            reasoning_tokens = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0) or 0
            billed_cost = usage.get("cost", 0) or 0

            tokenizer = None

        result = {
            "model_id": model_id,
            "fixture_name": fixture["name"],
            "fixture_category": fixture["category"],
            "fixture_char_count": fixture["char_count"],
            "fixture_byte_count": fixture["byte_count"],
            "generation_id": generation_id,
            "provider_served": provider,
            "input_tokens_billed": input_tokens,
            "output_tokens_billed": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "tokenizer_used": tokenizer,
            "total_cost_billed": billed_cost,
            "execution_duration_sec": round(elapsed, 3),
            "raw_response": raw_response,
            "ran_at": datetime.now(timezone.utc).isoformat(),
        }

        result_file.write_text(json.dumps(result, indent=2))
        logger.info(f"DONE {model_id} x {fixture['name']} (${billed_cost:.6f}, {elapsed:.1f}s)")
        return result


async def run_benchmarks(model_ids: list[str]):
    fixtures = discover_fixtures()
    if not fixtures:
        logger.error("No fixtures found!")
        return

    logger.info(f"Models: {len(model_ids)}  Fixtures: {len(fixtures)}  Concurrency: {CONCURRENCY}")

    tasks = []
    for mid in model_ids:
        for fix in fixtures:
            tasks.append(run_single(mid, fix))

    results = await asyncio.gather(*tasks)
    completed = [r for r in results if r is not None]
    logger.info(f"Completed {len(completed)} new benchmark(s)")
    return completed


def main():
    parser = argparse.ArgumentParser(description="TokenTax benchmark runner")
    parser.add_argument("--fetch-models", action="store_true", help="Refresh cache/models.json from OpenRouter")
    parser.add_argument("--model", type=str, help="Run a single model ID")
    parser.add_argument("--all-models", action="store_true", help="Run all models in cache/models.json")
    parser.add_argument("--paid-text-only", action="store_true", help="Only run paid text models (filters cache/models.json)")
    parser.add_argument("--recent", action="store_true", help="Only run models from 2026+, excluding pro/fast/reasoning/latest variants")
    parser.add_argument("--back-to", type=int, default=2026, metavar="YEAR", help="Override --recent cutoff year (e.g. --back-to 2024)")
    args = parser.parse_args()

    if not API_KEY or "your-key" in API_KEY:
        logger.error("Set OPENROUTER_API_KEY in .env")
        return

    if args.fetch_models:
        asyncio.run(fetch_models())

    if args.model:
        model_ids = [args.model]
    elif args.all_models:
        cache = load_models_cache()
        model_ids = list(cache.keys())
    elif MODELS_TXT.exists():
        model_ids = [line.strip() for line in MODELS_TXT.read_text().splitlines() if line.strip() and not line.startswith("#")]
    else:
        logger.error("No models specified. Create models.txt or use --model / --all-models.")
        return

    if args.paid_text_only:
        cache = load_models_cache()
        model_ids = [mid for mid in model_ids if mid in cache and _is_paid_text(cache[mid])]

    if args.recent:
        if not (args.all_models or args.paid_text_only):
            cache = load_models_cache()
        cutoff = calendar.timegm((args.back_to, 1, 1, 0, 0, 0))
        model_ids = [mid for mid in model_ids if mid in cache and _is_paid_text(cache[mid]) and _is_recent(cache[mid], cutoff) and not _is_variant(mid)]

    if model_ids:
        asyncio.run(run_benchmarks(model_ids))


if __name__ == "__main__":
    main()
