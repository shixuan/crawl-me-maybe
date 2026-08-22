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
  --max-relevant 40 --page-budget 200
```

`--max-relevant` says when you have enough; `--page-budget` says how much you
are willing to spend looking. Whichever comes first ends the run.

Semantic ranking is on by default. The first run downloads a local embedding model (~220MB) once.

The LLM stages turn on when `LLM_API_KEY` or `LLM_BASE_URL` is set. Without credentials they degrade away and the crawl still runs.

```bash
# seeds from RSS feeds. A feed entry is not a bare link: it carries the
# title, the publication time, and often the whole post, so the ranker
# judges the text before anything is fetched.
crawl run "what is worth doing in Toronto this weekend, with the event, the place and the date" \
  --seeds-rss "https://www.reddit.com/r/askTO/.rss,https://www.reddit.com/r/toronto/.rss"

# seeds from a JSON file: ["https://a", "https://b"], or
# {"seeds": [...], "allowed_domains": [...]}
crawl run "release notes" --seeds-file ./seeds.json

# a feed: log in once, then read several accounts, taking a turn from each.
# --feed without --session is refused: a logged-out crawl of a walled
# platform fetches login pages and reports them as an empty platform.
crawl session ./ig-state.json --feed instagram

crawl run "nearby merchants giving something away, with the shop, the offer and the deadline" \
  --seeds-file ./seeds.json \
  --feed instagram --session ./ig-state.json \
  --max-relevant 40 --page-budget 150 \
  --since '2 weeks' --depth-limit 1 --ignore-robots

# ignore the page budget, stop when the frontier runs dry
crawl run "all press coverage" --seeds "..." --draining

# read the results in a browser instead of a terminal
python dashboard/serve.py
```

---

## Optional installs

The base install crawls a link graph. Two paths cost more than every user
should carry, so they are extras, and the flags that need them say so before
a run starts rather than failing partway through it.

| Extra | Install | What it buys | Cost |
|-------|---------|--------------|------|
| `rss` | `pip install -e '.[rss]'` | `--seeds-rss`: read RSS/Atom feeds, entries arriving with their own text | feedparser, 0.3MB |
| `browser` | `pip install -e '.[browser]'`<br>then `playwright install chromium` | `--fetcher browser`, `--feed`, `--session`, `crawl session`: JS-rendered and login-walled pages | 135MB package, ~650MB browser |

Both together: `pip install -e '.[rss,browser]'`.

Without them the crawl still runs; only the flags that need them are refused,
naming the flag you typed and the one command that fixes it.

---

## CLI

### `crawl run "<prompt>"`

| Flag | Type | What it does |
|------|------|--------------|
| `--seeds` | string | Comma-separated seed URLs |
| `--seeds-file` | path | JSON file of seed URLs |
| `--seeds-rss` | urls | Comma-separated RSS or Atom feeds; entries arrive with their text |
| `--max-relevant` | int | Stop once this many pages are judged relevant (the goal) |
| `--page-budget` | int | Pages this run may read; 0 means no limit (the cost) |
| `--token-budget` | int | LLM tokens this run may spend (default: 500000) |
| `--time-budget` | int | Seconds this run may take |
| `--depth-limit` | int | Max depth from seeds (default: 5) |
| `--draining` | flag | Ignore `--max-pages`, stop when the frontier runs dry |
| `--since` | `"1 week"` \| date | Time window. Stops on `TIME_HORIZON`; assumes the source is ordered newest first |
| `--no-embedding` | flag | Skip semantic ranking this run (rules only) |
| `--recall` | flag | Miss less, read more: nothing is discarded, only ranked last |
| `--fetcher` | `http` \| `browser` | How to fetch; `browser` for JS-rendered or login-walled pages |
| `--feed` | `instagram` | Read the source as a platform feed |
| `--session` | path | Playwright storage_state, to crawl as a logged-in session |
| `--analysis` | `on` \| `off` | Per-page analysis and the steering it feeds |
| `--analyzer-max-chars` | int | Page text per analyzer call (default: 3000) |
| `--ignore-robots` | flag | Bypass robots.txt |
| `--domain-budget` | int | Max pages per domain |
| `--log-level` | `DEBUG` … `OFF` | Overrides env `LOG_LEVEL` |
| `--result-dir` | path | Where results go (default: `results`) |

### `crawl session <path>`

Opens a real browser at the platform, waits while you log in, and saves the
session Playwright needs. Your credentials are typed into the platform's own
page and never reach this process; what lands on disk is the session that
login produced.

| Flag | Type | Meaning |
|------|------|---------|
| `--feed` | `instagram` | Which platform to open (default: the first one) |
| `--force` | flag | Replace an existing session file |
| `--timeout` | int | Seconds to wait for the login (default: 600) |

A visible browser needs a desktop: WSLg on WSL, an X display over SSH.

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

## Dashboard

```bash
python dashboard/serve.py                       # http://127.0.0.1:8765
python dashboard/serve.py --port 9000 --results-dir ./results
```

A local, read-only page over what the crawls found: pick a run, filter by
classification, search across titles, summaries, extracted fields and page
text. Every extracted field is shown with the sentence it was checked
against, which is the point -- a value you cannot trace is a value you
cannot use.

Nothing about it is specific to any goal. Field names come from the run's own
extraction spec and are rendered as declared, so a run about shops and a run
about papers look the same and neither needed a line of code.

It binds to the loopback address and opens the run databases read-only: a run
database holds whatever a logged-in session could see, and browsing must never
be able to damage a run.

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

Two async loops run side by side. `fetch_pump` downloads and discovers links; `rank_pump` scores them and pushes them into the frontier. They coordinate only through the Frontier, which owns both halves: candidates waiting for a score, and scored candidates waiting for a fetch slot.

Every stage's decision is recorded: which rule dropped a link, what each ranker scored it, which model and prompt version produced a judgment. Raw HTML is kept, so a better prompt can re-judge a finished run without re-crawling.

---

## Configuration

Flags say what this run is doing; `.env` says what this machine and account can do — credentials, endpoints, which model, how much memory to spend. Everything has a default, so `.env` is optional. See [`.env.example`](.env.example).

```bash
# .env
LLM_API_KEY=sk-xxx
LLM_MODEL=deepseek/deepseek-v4-flash   # default: openai/gpt-4o-mini
LLM_BASE_URL=                          # for OpenAI-compatible endpoints
EMBEDDING_PROVIDER=local               # local | api
EMBEDDING_MODEL=                       # empty = the provider's default
EMBEDDING_API_KEY=jina_xxx             # only for the api provider
EMBEDDING_BASE_URL=https://api.jina.ai/v1
```

---

## Status

| Version | State | What it adds |
|---------|-------|--------------|
| v0.1 | ✅ | Full pipeline at zero LLM cost |
| v0.1.1 | ✅ | EmbeddingRanker, semantic ranking on a local model |
| v0.2 | ✅ | Goal Enhancer, LLMRanker, per-page analysis and steering, replay, inspect, time horizon |
| v0.3 | 🚧 | Playwright with login state, feed traversal, extracted fields with evidence |

---

## Design principles

- **Each module does one thing.** Fetch downloads, Extractor extracts, Ranker ranks. They never call each other; CrawlScheduler wires them.
- **The engine depends on Protocols.** `factory.py` is the only place that imports concrete classes.
- **Cheap stages first.** Rules, then embeddings, then LLM. Every LLM stage degrades away without credentials instead of blocking a run.
- **The corpus is frozen, judgments are append-only.** Old analyses are never overwritten.
- **Nothing is guessed.** A publication date comes from what a page declares or is left empty.
- **Crash-safe.** Checkpoints save frontier state; restore and keep going.
