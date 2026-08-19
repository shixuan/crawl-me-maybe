# Crawl me maybe

> Hey, I just met you,
>
> And this is crazy,
>
> But here's my sources,
>
> So crawl me, maybe?

A goal-driven crawler. You say what you are looking for; it decides where to go, what to skip, and when to stop, inside a budget.

---

## Quick start

```bash
pip install -e .

crawl run "recent funding news for AI startups" \
  --seeds "https://news.ycombinator.com,https://techcrunch.com" \
  --max-pages 200
```

Semantic ranking is on by default. The first run downloads a local embedding model (~220MB) once.

The LLM stages turn on when `LLM_API_KEY` or `LLM_BASE_URL` is set. Without credentials they degrade away and the crawl still runs.

```bash
# seeds from an RSS feed
crawl run "C++ backend job postings" --seeds-rss "https://hnrss.org/newest"

# seeds from a JSON file: ["https://a", "https://b"], or
# {"seeds": [...], "allowed_domains": [...]}
crawl run "release notes" --seeds-file ./seeds.json

# ignore --max-pages, stop when the frontier runs dry
crawl run "all press coverage" --seeds "..." --draining
```

---

## CLI

### `crawl run "<prompt>"`

| Flag | Type | What it does |
|------|------|--------------|
| `--seeds` | string | Comma-separated seed URLs |
| `--seeds-file` | path | JSON file of seed URLs |
| `--seeds-rss` | url | RSS or Atom feed to take seeds from |
| `--max-pages` | int | Page budget; 0 means no limit |
| `--max-tokens` | int | LLM token budget (default: 500000) |
| `--max-duration` | int | Time budget, seconds |
| `--depth-limit` | int | Max depth from seeds (default: 5) |
| `--draining` | flag | Ignore `--max-pages`, stop when the frontier runs dry |
| `--since` | `"1 week"` \| date | Time window. Stops on `TIME_HORIZON`; assumes the source is ordered newest first |
| `--embedding` | `local` \| `api` \| `off` | Semantic ranking (default: `local`) |
| `--embedding-model` | string | Overrides the provider default |
| `--analysis` | `on` \| `off` | Per-page analysis and the steering it feeds |
| `--analyzer-max-chars` | int | Page text per analyzer call (default: 3000) |
| `--ignore-robots` | flag | Bypass robots.txt |
| `--domain-budget` | int | Max pages per domain |
| `--log-level` | `DEBUG` … `OFF` | Overrides env `LOG_LEVEL` |
| `--result-dir` | path | Where results go (default: `results`) |

### `crawl inspect <task-id>`

Read-only summary of a finished task: goal, pages, analyses by classification, top relevant pages.

| Flag | Type | What it does |
|------|------|--------------|
| `--goal` | string | Which goal's analyses to show |
| `--export` | `json` \| `csv` | Dump the pages-and-analyses join to stdout |

### `crawl replay <task-id>`

Re-analyze a finished task's pages. No re-fetching: pages are a frozen corpus, and replay only appends to `analyses`. Replaying the same prompt is a no-op.

| Flag | Type | What it does |
|------|------|--------------|
| `--prompt` | string | New goal; analyses land under a new goal row |
| `--limit` | int | Re-analyze at most N pages |
| `--max-tokens` | int | Token budget for this replay |
| `--analyzer-max-chars` | int | Page text per analyzer call |
| `--force` | flag | Re-analyze pages that already have an identical analysis |

---

## How it works

A funnel. Each layer costs more and keeps fewer:

```
~200 links per page
  │
  ▼  Pre-filter          pure rules, zero LLM      → 10-30 links
  ▼  RuleRanker          weighted heuristics       → ordered
  ▼  EmbeddingRanker     cosine on a local model   → top 60
  ▼  LLMRanker           one batched call per 30   → final priority
```

Every fetched page also gets one analyzer call: classification, summary, relevance and hub scores, endorsed links. Those judgments land in `analyses` and steer the crawl in flight through priority multipliers, endorsed links, and cross-task domain reputation.

Two async loops run side by side. `fetch_pump` downloads and discovers links; `rank_pump` scores them and pushes them into the frontier. They coordinate only through the Frontier and the Buffer.

Every stage's decision is recorded: which rule dropped a link, what each ranker scored it, which model and prompt version produced a judgment. Raw HTML is kept, so a better prompt can re-judge a finished run without re-crawling.

---

## Configuration

Flags are per-run choices. `.env` is for things you set once — secrets, timeouts, deep-tuning knobs. Everything has a default, so `.env` is optional. See [`.env.example`](.env.example).

```bash
# .env
LLM_API_KEY=sk-xxx
LLM_MODEL=deepseek/deepseek-v4-flash   # default: openai/gpt-4o-mini
LLM_BASE_URL=                          # for OpenAI-compatible endpoints
EMBEDDING_API_KEY=jina_xxx             # only for --embedding api
EMBEDDING_BASE_URL=https://api.jina.ai/v1
```

---

## Status

| Version | State | What it adds |
|---------|-------|--------------|
| v0.1 | ✅ | Full pipeline at zero LLM cost |
| v0.1.1 | ✅ | EmbeddingRanker, semantic ranking on a local model |
| v0.2 | ✅ | Goal Enhancer, LLMRanker, per-page analysis and steering, replay, inspect, time horizon |
| v0.3 | planned | Playwright with login state, feed traversal, weekly digests |

---

## Design principles

- **Each module does one thing.** Fetch downloads, Extractor extracts, Ranker ranks. They never call each other; CrawlScheduler wires them.
- **The engine depends on Protocols.** `factory.py` is the only place that imports concrete classes.
- **Cheap stages first.** Rules, then embeddings, then LLM. Every LLM stage degrades away without credentials instead of blocking a run.
- **The corpus is frozen, judgments are append-only.** Old analyses are never overwritten.
- **Nothing is guessed.** A publication date comes from what a page declares or is left empty.
- **Crash-safe.** Checkpoints save frontier state; restore and keep going.
