# Crawl me maybe

> Hey, I just met you,
>
> And this is crazy,
>
> But here's my resources,
>
> So crawl me, maybe?

A goal-driven crawler. You tell what you are looking for, and it figures out where to go, what to skip, and when to stop — all on its own, within a budget.

Traditional crawlers try to grab everything. This one tries to grab *the right things*. Big difference.

---

## Quick start

```bash
pip install -e .
```

Then point it at something:

```bash
crawl run "recent funding news for AI startups" \
  --seeds "https://news.ycombinator.com,https://techcrunch.com" \
  --max-pages 200
```

It starts from the seeds, discovers links, filters out the noise, scores what's left, and only follows paths that actually look relevant. Stops when the budget runs out or the goal is satisfied — whichever comes first.

A few more ways to launch:

```bash
# Pull seeds from an RSS feed
crawl run "C++ backend job postings" \
  --source rss --source-path "https://hnrss.org/newest" \
  --max-pages 100

# Dump a list of URLs in a file
crawl run "release notes for an open-source project" \
  --source file --source-path ./urls.txt

# Keep going until there's nothing left worth crawling
crawl run "all press coverage of an event" --seeds "..." --draining
```

---

## CLI reference

### `crawl run "<prompt>"`

Launch a new task.

| Flag | Type | What it does |
|------|------|--------------|
| `--seeds` | string | Comma-separated seed URLs |
| `--source` | `manual` \| `file` \| `rss` | Where seeds come from (default: `manual`) |
| `--source-path` | path | File path or RSS feed URL for seeds |
| `--max-pages` | int | Page budget — 0 means no limit |
| `--max-tokens` | int | Token budget (kicks in at v0.2) |
| `--max-duration` | int | Time budget, in seconds |
| `--depth-limit` | int | How deep to go from seeds (default: 5) |
| `--draining` | flag | Ignore `--max-pages`, stop only when the frontier runs dry |
| `--result-dir` | path | Where to put results (default: `results`) |

### `crawl pause <task-id>`

Pause a running task. Lets in-flight fetches finish, then saves a checkpoint. You can resume later.

### `crawl resume <task-id>`

Pick up where you left off. Restores from the last checkpoint.

### `crawl stop <task-id>`

Tell a running task to wrap it up gracefully.

### `crawl status <task-id>`

See how a task is doing. (stub — v0.2)

### `crawl results <task-id>`

Export what we found.

| Flag | Type | What it does |
|------|------|--------------|
| `--export` | `json` \| `csv` | Pick your format |

### `crawl replay <task-id>`

Re-analyze an already-crawled task with a new prompt. No re-fetching — raw HTML is already on disk. (stub — v0.2)

| Flag | Type | What it does |
|------|------|--------------|
| `--prompt` | string | A new question to ask the same data |

---

## How it works

Think of it as a funnel. Each layer filters harder and costs more:

```
~200 links per page
  │
  ▼  Layer 0: Pre-filter (pure rules, zero LLM cost)
  ├─  Dedup, blacklist, robots.txt, file extensions, login pages,
  │   emoji links, depth limit, domain budget. Fast and cheap.
  │   ~200 → 10–30 candidates
  ▼  Layer 1: RuleScorer (7-factor heuristic, still zero LLM)
  ├─  Anchor text + snippet + title match + domain prior
  │   + depth + URL path + position. Score < 0.35 → dropped.
  ▼  Layer 2: LLMScorer (batched inference) 📋 v0.2
  ├─  One batch call re-ranks the top 30
  ▼  Layer 3: Feedback multiplier (runtime) 📋 v0.2
  └─  Pages we already fetched feed back to adjust priorities
```

Under the hood, two async loops run side by side: `fetch_pump` downloads pages and discovers links; `rank_pump` scores candidates and pushes them into the frontier. They don't wait on each other — just coordinate through the Frontier and Buffer when they need to.

---

## Current status

**v0.1 is done ✅** — a full pipeline at zero LLM cost. Canonicalizer, PreFilter, Frontier, HttpFetcher, Extractor, LinkExtractor, RobotsPolicy, RuleScorer, HybridRanker, CrawlScheduler, stop conditions, checkpoints, event emitter — the whole thing works end to end.

### What's next

| Version | Theme | Actually means |
|---------|-------|----------------|
| v0.2 | Brains | LLMScorer batched re-rank, PageAnalyzer, FeedbackStore, rebalanced weights, Replay |
| v0.3 | Polish | EmbeddingRanker, Playwright for JS-heavy pages, Prompt Cache, user feedback |

---

## Configuration

Configuration is handled by pydantic-settings — `.env` file or environment variables both work (env vars take precedence). Here's everything you can tweak:

```bash
# ---- Paths ----
# Where to store results (raw HTML, pages, database, logs).
# Override with --result-dir on the CLI.
RESULT_DIR=results

# ---- LLM (v0.2+) ----
# Model identifier in litellm format: provider/model-name.
LLM_MODEL=openai/gpt-4o-mini
# API key for the LLM provider. Leave empty to use provider defaults.
LLM_API_KEY=
# Custom base URL for self-hosted / proxied endpoints. Leave empty for default.
LLM_BASE_URL=
# Max concurrent LLM calls. Keep low to avoid rate limits.
LLM_CONCURRENCY=2

# ---- Fetch ----
# How many pages to download in parallel.
FETCH_CONCURRENCY=6
# TCP / TLS handshake timeout, in seconds.
FETCH_TIMEOUT_CONNECT=10.0
# Time to wait for the first byte of response, in seconds.
FETCH_TIMEOUT_READ=30.0
# Retries on transient errors (5xx, timeout, DNS). 429s add extra backoff.
FETCH_MAX_RETRIES=3

# ---- Extraction ----
# Hard timeout for trafilatura extraction + link parsing (per page).
# Safety valve for pathological HTML. Bump this for large / rich pages.
EXTRACT_TIMEOUT=120.0

# ---- Frontier ----
# Max candidates the in-memory buffer can hold before eviction kicks in.
CANDIDATE_BUFFER_SIZE=2000
# Number of candidates the ranker scores in one batch.
RANK_BATCH_SIZE=100
# Minimum interval between rank pump cycles, in seconds.
RANK_COOLDOWN_SEC=30.0
# Save a frontier snapshot every N pages fetched.
CHECKPOINT_INTERVAL=10
# Priority aging time window, in seconds. Older items get a gentle boost
# so they don't starve behind newer high-priority items.
PRIORITY_AGING_WINDOW=600.0

# ---- Budget defaults ----
# CLI flags --max-pages / --max-tokens / --max-duration override these.
DEFAULT_MAX_PAGES=500
DEFAULT_MAX_TOKENS=2000000
DEFAULT_MAX_DURATION_SEC=3600
# Max pages per domain before we stop accepting new links from that domain.
DEFAULT_DOMAIN_BUDGET=50

# ---- Robots ----
# Set to true to ignore robots.txt entirely (dev / intranet use).
IGNORE_ROBOTS=false
# How long to cache fetched robots.txt files, in hours.
ROBOTS_TTL_HOURS=24
# Number of consecutive 429/503 errors before a domain is circuit-broken.
CIRCUIT_BREAKER_THRESHOLD=5
# How long a circuit-broken domain stays blocked, in minutes.
CIRCUIT_BREAKER_COOLDOWN_MIN=10

# ---- Logging ----
# One of: DEBUG | INFO | WARNING | ERROR | CRITICAL | OFF
LOG_LEVEL=INFO
# One of: json | console
LOG_FORMAT=json
```

---

## Design principles

Nothing groundbreaking, but we stick to them:

- **Each module does one thing.** Fetch downloads. Extractor extracts. Ranker ranks. They don't call each other — CrawlScheduler wires everything together.
- **Engine depends on interfaces, not implementations.** `factory.py` is the only place that imports concrete classes. Everything else talks to Protocols.
- **Swap components by implementing a Protocol.** Want a different ranker? Write one, drop it in. Nothing else changes.
- **Crash-safe.** Checkpoints save Frontier state. Restore and keep going.
- **Raw HTML stays on disk forever.** Upgrade your models later, re-analyze without re-crawling.
- **LLM comes last.** Every cheaper technique runs first. When we do call LLMs, we batch them.

---

## Development

```bash
pip install -e ".[dev]"

# Unit + integration (skip e2e — those hit the network)
pytest tests/ -q --ignore=tests/e2e

```
