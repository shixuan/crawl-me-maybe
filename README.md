# Crawl me maybe

> Hey, I just met you,
>
> And this is crazy,
>
> But here's my resources,
>
> So crawl me, maybe?

A goal-driven crawler. You tell what you are looking for, and it figures out where to go, what to skip, and when to stop. All on its own, within a budget.

Traditional crawlers try to grab everything. This one tries to grab *the right things*. Big difference.

---

## Quick start

```bash
pip install -e .
```

That's it, nothing else to configure. Then point it at something:

```bash
crawl run "recent funding news for AI startups" \
  --seeds "https://news.ycombinator.com,https://techcrunch.com" \
  --max-pages 200
```

It starts from the seeds, discovers links, filters out the noise, scores what's left, and only follows paths that actually look relevant. Stops when the budget runs out or the goal is satisfied, whichever comes first.

Semantic ranking is on by default (local embedding model). The first run downloads the model weights (~220MB) to a local cache, one time only.

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
| `--max-pages` | int | Page budget: 0 means no limit |
| `--max-tokens` | int | LLM token budget: the task stops when exhausted (default: 500000) |
| `--max-duration` | int | Time budget, in seconds |
| `--depth-limit` | int | How deep to go from seeds (default: 5) |
| `--draining` | flag | Ignore `--max-pages`, stop only when the frontier runs dry |
| `--embedding` | `local` \| `api` \| `off` | Semantic ranking provider (default: `local`) |
| `--embedding-model` | string | Model id, overriding the provider default |
| `--ignore-robots` | flag | Bypass robots.txt checks |
| `--domain-budget` | int | Max pages per domain |
| `--log-level` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` \| `OFF` | Log verbosity (overrides env `LOG_LEVEL`) |
| `--result-dir` | path | Where to put results (default: `results`) |

### `crawl pause <task-id>`

Pause a running task. Lets in-flight fetches finish, then saves a checkpoint. You can resume later.

### `crawl resume <task-id>`

Pick up where you left off. Restores from the last checkpoint.

### `crawl stop <task-id>`

Tell a running task to wrap it up gracefully.

### `crawl status <task-id>`

See how a task is doing. (stub: v0.2)

### `crawl results <task-id>`

Export what we found.

| Flag | Type | What it does |
|------|------|--------------|
| `--export` | `json` \| `csv` | Pick your format |

### `crawl replay <task-id>`

Re-analyze an already-crawled task with a new prompt. No re-fetching needed, since raw HTML is already on disk. (stub: v0.2)

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
  ▼  Layer 1: RuleRanker (7-factor heuristic, still zero LLM)
  ├─  Anchor text + snippet + title match + domain prior
  │   + depth + URL path + position. Score < 0.35 → dropped.
  │   With embedding on, it stops dropping and just orders.
  ▼  Layer 1.5: EmbeddingRanker (semantic similarity) ✅ v0.1.1
  ├─  Goal + candidate texts embedded, ranked by cosine similarity.
  │   Top 60 survive. Catches synonyms rule scoring misses.
  ▼  Layer 2: LLMRanker (batched inference) 📋 v0.2
  ├─  One batch call re-ranks the top 30
  ▼  Layer 3: Feedback multiplier (runtime) 📋 v0.2
  └─  Pages we already fetched feed back to adjust priorities
```

Under the hood, two async loops run side by side: `fetch_pump` downloads pages and discovers links; `rank_pump` scores candidates and pushes them into the frontier. They don't wait on each other; they just coordinate through the Frontier and Buffer when they need to.

---

## Current status

**v0.1 is done ✅**: a full pipeline at zero LLM cost. Canonicalizer, PreFilter, Frontier, HttpFetcher, Extractor, LinkExtractor, RobotsPolicy, RuleRanker, HybridRanker, CrawlScheduler, stop conditions, checkpoints, event emitter. The whole thing works end to end.

**v0.1.1** adds the EmbeddingRanker for semantic ranking at near-zero cost. It's on by default (local ONNX model); `--embedding off` for rule-only v0.1 behavior.

### What's next

| Version | Theme | Actually means |
|---------|-------|----------------|
| v0.2 | Brains | LLMRanker batched re-rank, PageAnalyzer, FeedbackStore, rebalanced weights, Replay |
| v0.3 | Polish | Playwright for JS-heavy pages, Prompt Cache, user feedback |

---

## Configuration

Two entry points, one rule of thumb:

- **`crawl run --help` flags**: per-run choices and things you experiment with (budgets, robots, embedding provider, log verbosity)
- **`.env` / env vars**: set once and forget (secrets, timeouts, deep-tuning knobs). See [`.env.example`](.env.example) for the full annotated list. Every knob has a default, so `.env` is entirely optional.

**Secrets (API keys) are env-only, never flags.** Priority is uniform: `defaults → env vars → CLI flags`. When a flag and an env var target the same knob, the flag wins.

Want the API embedding provider instead of the default local model? The key lives in `.env`, the choice is per run:

```bash
# .env (once)
EMBEDDING_API_KEY=jina_xxx
EMBEDDING_BASE_URL=https://api.jina.ai/v1

# per run
crawl run "..." --embedding api --embedding-model jina-embeddings-v3
```

---

## Design principles

Nothing groundbreaking, but we stick to them:

- **Each module does one thing.** Fetch downloads. Extractor extracts. Ranker ranks. They don't call each other. CrawlScheduler wires everything together.
- **Engine depends on interfaces, not implementations.** `factory.py` is the only place that imports concrete classes. Everything else talks to Protocols.
- **Swap components by implementing a Protocol.** Want a different ranker? Write one, drop it in. Nothing else changes.
- **Crash-safe.** Checkpoints save Frontier state. Restore and keep going.
- **Raw HTML stays on disk forever.** Upgrade your models later, re-analyze without re-crawling.
- **LLM comes last.** Every cheaper technique runs first. When we do call LLMs, we batch them.

---

## Development

```bash
pip install -e .

# Unit + integration (skip e2e, those hit the network)
pytest tests/ -q --ignore=tests/e2e

# Keep it clean
ruff check src/ tests/
mypy src/
```
