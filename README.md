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

The LLM stages (goal enhancement, per-page analysis, LLM re-ranking) turn on automatically when `LLM_API_KEY` (or a custom `LLM_BASE_URL`) is set in `.env` — see [Configuration](#configuration). Without credentials they degrade away and the crawl still runs.

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
| `--analysis` | `on` \| `off` | Per-page analysis and the steering it feeds; `off` disables the whole subsystem for a clean baseline |
| `--analyzer-max-chars` | int | Page text sent to the analyzer per page (default: 3000, benchmark-picked) |
| `--ignore-robots` | flag | Bypass robots.txt checks |
| `--domain-budget` | int | Max pages per domain |
| `--log-level` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` \| `OFF` | Log verbosity (overrides env `LOG_LEVEL`) |
| `--result-dir` | path | Where to put results (default: `results`) |

### `crawl inspect <task-id>`

Look at a task's results: goal, pages, analyses by classification, and the top relevant pages. Read-only, no LLM.

| Flag | Type | What it does |
|------|------|--------------|
| `--goal` | string | Which goal's analyses to show (default: the task's original goal; replay goals are listed) |
| `--export` | `json` \| `csv` | Dump the pages-and-analyses join to stdout instead of the summary |

### `crawl replay <task-id>`

Re-analyze an already-crawled task's pages. No re-fetching: the pages table is the frozen corpus, and replay only appends new rows to the analyses table. Replaying the same prompt is a no-op; `--force` re-runs it.

| Flag | Type | What it does |
|------|------|--------------|
| `--prompt` | string | New goal statement; analyses are stored under a new goal row (same prompt text reuses that goal) |
| `--limit` | int | Re-analyze at most N pages (default: all) |
| `--max-tokens` | int | Token budget for this replay (default: unlimited) |
| `--analyzer-max-chars` | int | Page text cap sent to the analyzer per page |
| `--force` | flag | Re-analyze pages that already have an identical analysis |

---

## How it works

Think of it as a funnel. Each layer filters harder and costs more:

```
~200 links per page
  │
  ▼  Layer 0: Pre-filter (pure rules, zero LLM cost)
  ├─  Dedup, robots.txt, file extensions, login pages, depth limit,
  │   domain budget. Fast and cheap.  ~200 → 10–30 links
  ▼  Layer 1: RuleRanker (7-factor heuristic, still zero LLM)
  ├─  Anchor text + snippet + title + domain prior + depth + URL path
  │   + position.  With an LLM stage on it pre-filters at a relaxed
  │   0.25; with embedding on it stops dropping and only orders.
  ▼  Layer 1.5: EmbeddingRanker (semantic similarity) ✅ v0.1.1
  ├─  Goal + link texts (anchor, snippet, heading) embedded, ranked by
  │   cosine similarity. Top 60 survive. Catches synonyms rule
  │   scoring misses.
  ▼  Layer 2: LLMRanker (batched inference) ✅ v0.2
  ├─  One batched call (≤30 links) fine-ranks the survivors;
  │   larger batches chunk automatically. Fails open to the earlier
  │   stages' scores.
```

Alongside the funnel, every fetched page gets one analyzer call: classification (RELEVANT / HUB / AGGREGATOR / IRRELEVANT / NAVIGATION), a summary, relevance and hub scores, and endorsed links. Those judgments are the product you read in the `analyses` table, and they steer the crawl in flight: hub/domain priority multipliers, endorsed links injected into the frontier, and cross-task domain reputation persisted to `results/feedback.db`. `--analysis off` turns the whole subsystem off for a clean baseline.

Under the hood, two async loops run side by side: `fetch_pump` downloads pages and discovers links; `rank_pump` scores links and pushes them into the frontier. They don't wait on each other; they just coordinate through the Frontier and Buffer when they need to.

---

## Current status

**v0.1 is done ✅**: a full pipeline at zero LLM cost. Canonicalizer, PreFilter, Frontier, HttpFetcher, Extractor, LinkExtractor, RobotsPolicy, RuleRanker, HybridRanker, CrawlScheduler, stop conditions, checkpoints, event emitter. The whole thing works end to end.

**v0.1.1** adds the EmbeddingRanker for semantic ranking at near-zero cost. It's on by default (local ONNX model); `--embedding off` for rule-only v0.1 behavior.

**v0.2 is in progress ✅**: the LLM core is in — Goal Enhancer, LLMRanker, per-page analysis with the steering loop it feeds (priority multipliers, endorsed links, cross-task domain reputation), analyzer tuning backed by a benchmark harness, and Replay (re-judge a finished run without re-crawling, append-only and idempotent). Left: the time horizon (`--since`), and release polish.

### What's next

| Version | Theme | Actually means |
|---------|-------|----------------|
| v0.2 | Brains | Time horizon (`--since`) and release polish |
| v0.3 | Polish | Playwright for JS-heavy pages, Prompt Cache, feed traversal |

---

## Configuration

Two entry points, one rule of thumb:

- **`crawl run --help` flags**: per-run choices and things you experiment with (budgets, robots, embedding provider, log verbosity)
- **`.env` / env vars**: set once and forget (secrets, timeouts, deep-tuning knobs). See [`.env.example`](.env.example) for the full annotated list. Every knob has a default, so `.env` is entirely optional.

Want the API embedding provider instead of the default local model? The key lives in `.env`, the choice is per run:

```bash
# .env (once)
EMBEDDING_API_KEY=jina_xxx
EMBEDDING_BASE_URL=https://api.jina.ai/v1

# per run
crawl run "..." --embedding api --embedding-model jina-embeddings-v3
```

Same pattern for the LLM stages: credentials live in `.env`, the LLM stages turn on automatically and never block a run when they fail:

```bash
# .env (once)
LLM_API_KEY=sk-xxx
LLM_MODEL=deepseek/deepseek-v4-flash   # optional; default openai/gpt-4o-mini
LLM_BASE_URL=                          # optional, for OpenAI-compatible endpoints
```

---

## Design principles

Nothing groundbreaking, but we stick to them:

- **Each module does one thing.** Fetch downloads. Extractor extracts. Ranker ranks. They don't call each other. CrawlScheduler wires everything together.
- **Engine depends on interfaces, not implementations.** `factory.py` is the only place that imports concrete classes. Everything else talks to Protocols.
- **Swap components by implementing a Protocol.** Want a different ranker? Write one, drop it in. Nothing else changes.
- **Crash-safe.** Checkpoints save Frontier state. Restore and keep going.
- **The corpus is frozen, judgments are append-only.** Replay re-judges a finished run without re-crawling; old analyses are never overwritten, and replaying the same prompt is a no-op.
- **Cheap stages first.** Rules, then embeddings, then LLM — and every LLM stage degrades away without credentials instead of blocking a run.

