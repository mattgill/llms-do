# AGENTS.md

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env   # then edit with your OPENROUTER_API_KEY
python3 run.py --fetch-models   # pulls OpenRouter model metadata to cache/models.json
python3 run.py                   # benchmarks models listed in models.txt
python3 run.py --all-models --recent   # all paid text models from 2026+, no pro/fast/reasoning/latest variants
python3 backfill.py              # patches missing reasoning_tokens from generation stats API (free)
python3 generate.py              # produces public/index.html + public/models-results.csv
```

Use `python3`, not `python`. The `README.md` is wrong about this.

## Architecture

Two scripts (+ backfill utility), run in order:

1. **`run.py`** — async benchmark runner. Sends each fixture to each model via OpenRouter chat completions, caches results to `cache/results/{model_slug}/{fixture_name}.json`. Skips already-cached combinations. Uses `CONCURRENCY` env var (default 3). Fixtures are discovered from subdirectories of `fixtures/` by category. Retries 429 rate limits with exponential backoff.

2. **`backfill.py`** — queries OpenRouter generation stats API to patch missing `reasoning_tokens` into cached results. Idempotent, zero API cost.

3. **`generate.py`** — reads `cache/results/` and `cache/models.json`, writes a self-contained static HTML dashboard to `public/index.html` plus `public/models-results.csv`. No backend, no runtime costs.

Model source of truth: `models.txt` (one ID per line, `#` comments allowed). Flags: `--model`, `--all-models`, `--recent` (paid text, 2026+, excludes pro/fast/o1/o3/deep-research/latest variants).

`public/` is committed (GitHub Pages source). `cache/` is committed. `.env` is gitignored.

## HTML template quirks

The dashboard HTML is a **Python raw string** (`r"""..."""`) embedded in `generate.py`. Critical consequences:

- **Backslashes are literal.** `\'` in JS strings inside the template is written as-is and works in JS, but raw string rules mean you can't use Python escape sequences.
- **The `h()` JS helper only takes 3 args** — `(tag, attrs, children)`. Any additional children (4th arg, 5th arg, etc.) are silently dropped. Always wrap mixed text+element children in an array: `h('p', null, ['text ', h('a', ...), ' more text'])`.
- **colSpan values** must be updated whenever a column is added to or removed from the dashboard table. Check the expanded detail row, the empty-state row, and any merged cells.

## License

MIT