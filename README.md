# LLMs Do

**Give an LLM a thing and it costs you tokens and money.**

Built with [OpenCode](https://github.com/anomalyco/opencode) and DeepSeek V4 Pro.

Inspired by [Playcode's article on the real cost of frontier models](https://playcode.io/blog/real-price-of-frontier-models), this project benchmarks identical static files against multiple LLMs via OpenRouter. The thesis: **$/Mtok is an illusion** because different models split the same text into wildly different token counts — and different providers charge different rates.

## How it works

1. `models.txt` lists OpenRouter model IDs to benchmark.
2. `fixtures/` contains static test files across categories (TypeScript, Python, JSON, Markdown, Prose).
3. `python run.py` sends each fixture to each model, tracks token counts, actual billed cost, and the provider that served the request. Results are cached to disk.
4. `python generate.py` reads all cached results and produces a self-contained `output/dashboard.html` (no backend needed) plus `output/dashboard.csv`.

## Why a static dashboard?

I started building an interactive FastAPI site with a database, background workers, and a live API. Then I realized: once a model's results are cached there's no reason to re-run them, and I don't want an open service on the internet that could rack up API costs. A cron job + static HTML gets the same result with zero attack surface and zero runtime costs. Run the benchmarks once, host the generated HTML anywhere.

## Quick start

```bash
# Install dependencies
pip install httpx python-dotenv

# Set your OpenRouter API key
cp .env.example .env
# Edit .env with your key

# Fetch model metadata (scores, pricing)
python run.py --fetch-models

# Run benchmarks for models in models.txt
python run.py

# Generate dashboard
python generate.py
# Open output/dashboard.html
```

## Preview the dashboard

Serve the generated `public/` directory locally with:

```bash
python3 serve.py
# Open http://127.0.0.1:8000/
```

To view it from another machine, bind to all interfaces and open port 8000 in
the remote machine's firewall/security group:

```bash
python3 serve.py --host 0.0.0.0 --port 8000
```

This is a simple unauthenticated development server; do not expose it directly
to the public internet.

## Dashboard columns

| Column | Meaning |
|--------|---------|
| Int / Code / Agent | Intelligence, coding, and agentic scores from OpenRouter |
| $/Mtok In/Out | Published list price per million tokens |
| In Tok / Out Tok | Actual tokens billed |
| Mult In / Out | Tokens ÷ characters in fixture |
| Provider | Underlying provider OpenRouter routed to |
| Cost/MB | Actual billed cost per megabyte of source |
| Hypoth $/MB | Cost at list price, ignoring free credits |
| Eff $/Mtok | Effective $/Mtok rate from this specific provider |
| Total $ | Actual USD billed |
