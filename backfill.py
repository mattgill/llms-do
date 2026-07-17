#!/usr/bin/env python3
"""Backfill missing reasoning_tokens in cached results via OpenRouter generation stats API."""

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("OPENROUTER_API_KEY", "")
BASE_URL = "https://openrouter.ai/api/v1"
TIMEOUT = 60
CONCURRENCY = 5
ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "cache" / "results"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill")

_semaphore = asyncio.Semaphore(CONCURRENCY)


def get_headers():
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


async def backfill_result(client: httpx.AsyncClient, filepath: Path):
    data = json.loads(filepath.read_text())
    gen_id = data.get("generation_id")
    if not gen_id:
        return None

    old = data.get("reasoning_tokens")
    if old is not None:
        return None

    r = await client.get(f"{BASE_URL}/generation", params={"id": gen_id}, headers=get_headers())
    if r.status_code == 404:
        return None
    r.raise_for_status()
    resp = r.json()
    gen = resp.get("data", {})
    if not isinstance(gen, dict):
        return None
    reasoning = gen.get("native_tokens_reasoning", 0) or 0

    data["reasoning_tokens"] = reasoning
    filepath.write_text(json.dumps(data, indent=2))
    return reasoning


async def main():
    files = list(RESULTS_DIR.glob("**/*.json"))
    logger.info(f"Found {len(files)} cached result files")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async def process(f: Path):
            async with _semaphore:
                try:
                    r = await backfill_result(client, f)
                    if r is not None:
                        logger.info(f"Backfilled {f.parent.name}/{f.name}: reasoning_tokens={r}")
                except Exception as e:
                    logger.warning(f"Failed {f.parent.name}/{f.name}: {e}")

        await asyncio.gather(*[process(f) for f in files])

    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
