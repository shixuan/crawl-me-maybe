# Architecture

---

## Design principles

Only these constrain the shape of the code:

- **One job each, and no sideways calls.** The fetcher downloads, the extractor
  extracts, the ranker orders. Modules never call each other; all coordination
  happens in `CrawlScheduler`.
- **The pipeline carries typed objects**, not strings. Every stage takes and
  returns a pydantic model.
- **Dependency injection.** The engine imports no concrete class, only Protocols;
  `factory.py` is the single place that knows the implementations. Tests inject
  with `create_scheduler(cfg, fetcher=mock)`.
- **Resumable.** Whenever it exits, it can pick up from a `FrontierSnapshot`.
- **Replayable.** Raw HTML is kept forever, so a better model or a better prompt
  means re-analysing, not re-crawling.
- **Cost control.** Everything free runs before anything that costs; LLM calls are
  amortised over batches.
- **Event-sourced.** Every state change appends an event. Replay, debugging and
  incremental output are all built on that log.
- **Able to answer "how do you know?"** Every extracted field carries a quote from
  the page, and a quote that is not in the page throws the field away. The
  extractor also refuses the `date` trafilatura offers for free, because it will
  read "Copyright 2024" in a footer as a publication date. Better to admit not
  knowing than to hand back a guess.

---

## The whole thing

> Both kinds of source can appear in one run: a feed seed yields posts, an
> ordinary web seed yields outlinks, and they travel the same loop through the
> same components. **The only difference is who claims the page at step ⑤** — not
> a second code path.
>
> **There is one ranking stage.** A rule ranker and an embedding ranker used to sit
> between the pre-filter and the LLM, along with a steering layer that adjusted
> priorities by domain reputation. All three were removed in v0.3.0; the reasoning
> is at the top of `ranking.md`.

### One picture

A single `crawl run`, top to bottom. Boxes are the components that do the work,
the right-hand column is the package each one lives in, and `*` marks a slot the
engine only knows as a Protocol, filled in by the factory.

```
   crawl run "<goal>" --seeds … [--session …]                          cli/
                    │
                    ▼
   GoalEnhancer     one LLM call: goal statement, keywords,          pioneer/
                    which fields to extract
                    │
                    ▼
   create_scheduler(settings, goal)                        scheduler/factory
   the only place concrete classes are imported
                    │
 ┌──────────────────▼─────────────────────────────────────────────────────┐
 │ CrawlScheduler       fetch_pump ∥ rank_pump, one half-loop each        │
 │                      they meet only at the Frontier                    │
 └──────────────────┬─────────────────────────────────────────────────────┘
                    │
    ┌───────────────▼──────────────────────────────────────────┐
    │ Frontier*  everything found and not yet read, in halves  │  pioneer/
    │                                                          │
    │   ① PriorityQueue      scored, waiting for a fetch slot  │
    │   ⑥ RoundRobinBuffer*  unscored, a turn per seed         │
    └───┬─────────────────────────────────────────▲────────────┘
        │ pop one                                 │ push_batch
        ▼                                         │
    ② Fetcher*      httpx, or Playwright with a   │        digest/fetcher
       │            logged-in session             │
       ▼                                          │
    ③ Extractor*    trafilatura → text            │        digest/
       │                                          │
       ▼                                          │
    ④ Analyzer*     one LLM call: verdict,        │        analyzer/
       │            summary, and the goal's       │
       │            fields, each with a quote     │
       ▼                                          │
    ⑤ Harvester*    listing → post permalinks     │        digest/
       │            ordinary page → outlinks      │
       │            whichever FeedAdapter* claims │
       │            it (instagram · rss · none)   │
       │                                          │
       └──► back to ⑥ ──► ⑦ PreFilter ──► Ranker* ┘        pioneer/
                          10 URL-level   20 per batch,
                          checks, no LLM absent without credentials

    Side channel: ④'s endorsed_links ──► ⑦ ──► straight into ①, priority 1.0
                  the only way out of the platform a run started on — a feed
                  harvester only ever finds more of the same platform

 ┌────────────────────────────────────────────────────────────────────────┐
 │ Persistence: one timestamped directory per run, and no mutable state   │  storage/
 │ shared across runs                                                     │
 │   ③ → pages + raw HTML     ④ → analyses     ⑤ → links                  │
 │   ⑦ → rank_decisions       state changes → events                      │
 └────────────────────────────────────────────────────────────────────────┘

 Cross-cutting, used by every layer above:
   schemas/   the shared vocabulary, plain pydantic, no dependencies
   llm/       LLMClient · TokenBudget (input / output / cached / thinking,
              each counted separately)
   state/     CrawlContext (counters + stats) · EventEmitter
   config.py  Settings: defaults → .env → env → flag
```

How to read it:

- **The top half ①→②→③→④→⑤ is `fetch_pump`.** One page comes off the queue and
  goes all the way through; it never waits for ranking.
- **The bottom half ⑥→⑦→① is `rank_pump`.** The unscored half is ranked when it
  has a batch, or has been non-empty for 30s, or the scored half is starving.
- **The two coroutines meet only at the Frontier.** Exactly three pieces of state
  are shared: the scored half (an `asyncio.Lock`), the unscored half (an
  `asyncio.Condition`), and `page_contexts` (a plain dict only `fetch_pump` writes).
- **`PriorityQueue` is two levels.** It does not order anything itself: it groups
  by seed, gives each group its own queue, and uses another queue to decide whose
  turn it is. The same algorithm therefore plugs in at either level, and
  `--order best` is not a second code path — plug it in at the outer level and
  "the best of the best group" *is* "the best overall".
- **④ is the product, ⑦ is a prediction.** Ranking guesses which pages are worth
  reading; analysis establishes what they actually were. Only the analysis carries
  quotes from the page, and only it is what a user consumes.

One line per layer:

- **pioneer (discovery)** — decides where to go next: Canonicalizer, PreFilter,
  Frontier, Ranker, RobotsPolicy, GoalEnhancer
- **digest (processing)** — turns a page into content and candidates: Fetcher,
  Extractor, Harvester, FeedAdapter
- **analyzer (analysis, optional)** — one LLM call per page: verdict, summary,
  fields with evidence. `--analysis off` and none of it is constructed
- **scheduler (orchestration)** — the engine knows only Protocols, the factory is
  the single assembly point, `stop_conds` decides when to stop
- **storage (persistence)** — contracts in `contracts.py`, SQLite in `sqlite/`;
  a different storage technology is a sibling package plus one factory change

---

## Dependency injection: Protocol + Factory

The engine imports no concrete class; it depends only on Protocols. `factory.py`
is the only module that knows every implementation.

### Protocol → implementation

| Module | Protocol | Implementation | What it is |
|--------|----------|----------------|------------|
| `storage/contracts.py` + `storage/sqlite/crawl_db.py` | `CrawlDb` | `SqliteCrawlDb` | Per-run crawl state (SQLite + an async write queue) |
| `analyzer/page_analyzer.py` | `Analyzer` | `PageAnalyzer` | One LLM analysis per page |
| `digest/feed/base.py` | `FeedAdapter` | `instagram` / `rss` | Whoever claims the page parses it |
| `pioneer/frontier.py` | `Frontier` | `GatedFrontier` | Everything discovered and unread, both halves |
| `pioneer/buffer.py` | `Buffer` | `RoundRobinBuffer` | The unscored half, a turn per seed |
| `pioneer/queue.py` | — | `PriorityQueue` | The scored half, heapq plus cooldowns |
| `digest/fetcher.py` | `Fetcher` | `HttpFetcher` | Async HTTP over httpx |
| `digest/extractor.py` | `Extractor` | `TrafExtractor` | trafilatura extraction |
| `pioneer/ranker/base.py` | `Ranker` | `LLMRanker` | The only ranking stage; `None` without credentials |
| `pioneer/prefilter.py` | — | `PreFilter` | No Protocol, used directly |
| `pioneer/canonicalizer.py` | — | `Canonicalizer` | No Protocol, used directly |
| `pioneer/robots.py` | — | `RobotsPolicy` | No Protocol, used directly |

PreFilter, Canonicalizer and RobotsPolicy are simple classes with little or no
state to protect, so they get no Protocol layer.

### Factory

```python
# src/crawlme/scheduler/factory.py
def create_scheduler(settings: Settings, goal=None, budget=None, **overrides: Any) -> CrawlScheduler:
    kwargs = {
        "settings": settings,
        "storage": SqliteCrawlDb.create(settings.result_dir),
        "frontier": GatedFrontier(
            domain_budget=goal.domain_budget,
            buffer=RoundRobinBuffer(capacity=settings.candidate_buffer_size),
        ),
        "fetcher": HttpFetcher(user_agents=..., connect_timeout=..., ...),
        "extractor": TrafExtractor(),
        "robots": RobotsPolicy(ignore=settings.ignore_robots),
        "prefilter": PreFilter(),
        "ranker": _build_ranker(settings, llm=llm_ranker),
        "canonicalizer": Canonicalizer(),
        "analyzer": analyzer,
    }
    kwargs.update(overrides)  # tests inject: create_scheduler(cfg, goal, fetcher=mock)
    return CrawlScheduler(**kwargs)

# _build_ranker(settings, llm):
#   hands the llm straight back.  With credentials that is an LLMRanker, without
#   them it is None -- the engine accepts having no ranker at all and enqueues
#   candidates flat, in the order the frontier hands them out.

# Configuration layering: every knob lives on Settings (readable from env/.env),
# CLI flags override at runtime.  Precedence: defaults -> .env -> env -> flag.
# .env.example documents the set-once knobs; the env twins of per-run knobs exist
# but are not advertised, and a flag beats an env var.
```

### CrawlScheduler.__init__

```python
class CrawlScheduler:
    def __init__(
        self, *,
        settings: Settings,          # plain configuration
        storage: CrawlDb,            # Protocol
        frontier: Frontier,          # Protocol
        fetcher: Fetcher,            # Protocol
        extractor: Extractor,        # Protocol
        robots: RobotsPolicy,        # concrete
        prefilter: PreFilter,        # concrete
        ranker: Ranker | None,       # Protocol; None = no credentials, no ranking
        canonicalizer: Canonicalizer,# concrete
        analyzer: Analyzer | None = None,  # Protocol; None = analysis off
    ) -> None:
```

Every argument is required. No defaults, no `or Extractor()` fallbacks.

---

## Core flows

### Startup and seed ingestion

```
user submits a prompt
  → CrawlGoal (goal_id, prompt, max_pages, max_tokens, ...)
  → CrawlTask created (state=RUNNING)
  → create_scheduler(settings)
  → scheduler.ingest_seeds(goal, candidates, allowed_domains?):
      for each Candidate:
        canonicalizer.canonicalize(raw, base)
        frontier.get_prefilter_context(allow_fetch=..., allowed_domains=...)
        prefilter.check(c, goal, ctx)  # seeds only face dedup/blacklist/protocol/scope
        → FrontierItem(priority=1.0, score_source="seed")
      → frontier.push_batch(items)
  → scheduler.run(goal, task):
      starts fetch_pump and rank_pump as two asyncio coroutines
```

### fetch_pump

```
loop while state == "RUNNING":
  check_stop(task, frontier, counters)
    → independent checks, all of which can fire; returns every StopReason that did
    → any hit → state = "STOPPING", wake rank_pump, break

  # Page budget, a hard ceiling: what is already promised (pages_fetched +
  # in_flight) counts against it.  Without that, in-flight pages are invisible and
  # concurrency overshoots max_pages by up to fetch_concurrency-1.
  if pages_fetched + in_flight >= max_pages: sleep(0.2); continue
    # A failed in-flight fetch gives its slot back, so wait rather than break --
    # this neither under- nor over-fetches.

  item = frontier.pop_next(now, next_allowed, global_budget)
    # skips items whose domain is cooling; returns None when a domain or the
    # global budget is spent
  if item is None:
      if frontier.scoring > 0 → sleep(0.2); continue   # a batch is out being scored
      if the unscored half is empty and in_flight == 0 and frontier.cooling == 0 → break
        # cooling holds items time will release on its own.  "Nothing pops" is not
        # "nothing is left": once the host clock stepped backwards, the only seed
        # went into cooldown and the run reported success having fetched nothing.
      sleep(0.2); continue

  counters.in_flight++
  asyncio.create_task(_handle_fetch(item))  # not awaited

_handle_fetch(item):
  a semaphore bounds fetch concurrency
  result = fetcher.fetch(item)                 # HTTP GET with retries
  robots.record_response(domain, status)       # updates the domain cooldown
  failure → record_outcome(item, FAILED) → return

  storage.save_raw_html(url_key, item_id, raw)  # raw HTML to disk
  page = extractor.extract(result, raw_path)    # trafilatura → Page
  storage.save_page(page)

  candidates = harvester.harvest(page, document)   # posts, or outlinks
  ctx = frontier.get_prefilter_context(
      allow_fetch=lambda url: robots.allow_fetch(url)
  )
  for each candidate:
      decision, _ = prefilter.check(c, goal, ctx)
      ALLOW → frontier.push_candidates([c]), storage.save_link(c)
      DROP  → c.status = "FILTERED_OUT"

  frontier.record_outcome(item, COMPLETED)
  counters.pages_fetched++
  if pages_fetched % CHECKPOINT_INTERVAL == 0 → checkpoint()
```

### rank_pump

```
loop while state == "RUNNING":
  await frontier.waiting.wait_until(ready or state != RUNNING)
  batch = frontier.take_for_ranking(RANK_BATCH_SIZE=20)
    # while it is out, this batch is in neither half; the frontier counts it
    # (scoring), or the stop checks read "being scored" as "nothing left"
  ... finally: frontier.finish_ranking(len(batch))

  history = RankHistorySummary(pages_seen=counters.pages_fetched)
  decisions = ranker.rank_batch(goal, batch, history, page_contexts)
    # or, with no ranker, one flat priority per candidate

  for each decision:
      storage.save_rank_decision(d)
      if dropped → continue
      → FrontierItem(priority=d.priority, score_source=d.ranker, ...)
  frontier.push_batch(items)
```

`fetch_pump` and `rank_pump` are independent coroutines. Fetching never waits for
ranking and ranking never waits for fetching. They coordinate on exactly two
primitives: the Frontier's `asyncio.Lock` and the Buffer's `asyncio.Condition`.

### The ranking funnel

The most cost-sensitive stretch of the whole pipeline:

```
200+ raw links per page
  ① Canonicalizer: normalise, fingerprint for dedup
  ② PreFilter (rules only) → 10-30 left → the frontier's unscored half
  ③ a ranking cycle fires when any of these holds:
       a full batch, non-empty for 30s, the scored half starving
  ④ LLMRanker.rank_batch (skipped without credentials; everything enqueues flat):
       a. 20 candidates per batch, split further by character budget
       b. the model scores each 0-1 with one clause of reasoning; the clearly
          worthless go on a drop list
       c. the score is the enqueue priority
  ⑤ decisions go to frontier.push_batch
```

There is only the LLM stage. The two free stages that preceded it were removed;
the reasoning is at the top of `ranking.md`.

### Pause / resume / stop

The engine supports it (the KeyboardInterrupt path uses it), but the CLI state
commands are parked — they need a daemon.

```
pause():
  stop popping; wait for in_flight to drain
  checkpoint() → FrontierSnapshot to the database
  state=PAUSED

resume():
  read the latest FrontierSnapshot → GatedFrontier.restore() rebuilds heap + buffer
  → state=RUNNING

crash recovery:
  on startup, state=RUNNING with a snapshot present → resume automatically
```

### Replay

```
crawl replay <task-id> [--prompt "new goal"] [--limit N] [--max-tokens N] [--force]
  → find_run_dir(task_id): scans results/<ts>/db/crawl.db (there is no task index)
  → reads crawl_goals / crawl_tasks, list_pages() rebuilds Page objects from the
    frozen corpus — nothing is re-fetched and nothing is re-extracted
  → calls the analyzer per page, appending to analyses only
  → idempotent: identity is (url_key, goal_id, prompt_version, model), so a
    replay of a replay is a no-op.  --force skips the check and appends new rows;
    old rows are never touched, which is what makes variance studies possible
  → --prompt makes a new goal (goal_id = sha256(prompt)[:12], so the same text
    reuses the already-enhanced goal row and GoalEnhancer runs only when the row
    is missing).  The old goal is left alone.
```

### Inspect

```
crawl inspect <task-id> [--goal <goal_id>] [--export json|csv]
  → read-only: task / run / pages / goals (marked original or replay) /
    the classification spread per goal / top relevant pages, deduplicated by url
  → --export writes the pages⋈analyses join to stdout (url, title, class,
    relevance, hub, summary, tags, model, timestamp)
```

### Feed traversal

Feeds and ordinary web pages travel the same loop through the same components;
**the only difference is who claims the page**. What follows is specific to the
feed side.

**Two kinds of source, with different priorities:**

| Mode | Entry point | Can deep search do it? | Priority |
|------|-------------|------------------------|----------|
| **Monitoring** | the timeline of a known account | **Structurally no** — it can only reach what others said about that account | First |
| **Discovery** | hashtag or place tags | Yes, and cheaply, across a wide surface | Later, possibly delegated entirely |

Monitoring is small (tens of accounts, once a week). The low request volume suits
platform rate limits, and it hits exactly the gap the measurements found.
Discovery is high volume and noisy, and is where the funnel genuinely strains.

**Where seeds come from:** deep search can act as a `UrlSource` provider offering
broad account and hashtag candidates, filtered by hand into a monitoring list; a
user can also skip it entirely and name accounts directly, which is also the
cheaper path in tokens. Note that links from deep search **should not be treated
as seeds** — seeds skip most PreFilter rules and get priority 1.0 outright, while
these are low-confidence candidates.

**Platform preconditions:** a platform like Instagram needs browser rendering plus
a logged-in session (`storage_state`) the user supplies. Without a session the
platform simply serves no content, and no crawler architecture gets around that.
The real operational risk is the account, not the technology, so the fetch budget
is naturally small — which reinforces something that runs against the
graph-traversal instinct of "fetch more, get more": **extraction quality matters
more than coverage**.

---

## Modules

Every replaceable module sits behind a `typing.Protocol`. Simple stateless classes
(PreFilter, Canonicalizer, RobotsPolicy) are used directly.

### CrawlScheduler

Owns the crawl loop. Drives `fetch_pump` and `rank_pump`, runs the stop checks,
handles pause/resume/stop, and checkpoints automatically.

- `__init__` takes everything explicitly, typed as Protocols or concrete classes,
  and imports no implementation
- `_page_contexts` caches `{title, link_count}` per page so the ranker can use
  per-page signals
- `CrawlCounters` is a `@dataclass` rather than a dict — typed, attribute access,
  mutable by design
- `PreFilterContext` comes from `frontier.get_prefilter_context(**overrides)`, so
  the engine never reaches into the frontier's private fields
- `ingest_seeds()` is separate: seeds face only dedup/blacklist/protocol/scope and
  skip robots/extension/depth/domain_budget

### Canonicalizer

Collapses every spelling of the same page into one URL and fingerprints it for
deduplication.

Seven steps: resolve relative links → lowercase scheme and host → drop the default
port → fold repeated slashes → strip 17 tracking parameters (utm_*, fbclid, gclid,
…) → sort the remaining parameters by key → sha256[:16] as `url_key`.

`reg_domain` comes from stripping 20 common subdomain prefixes (www, m, api, cdn, …).

### PreFilter

The second gate before ranking (the first is the canonicaliser's dedup
fingerprint). No LLM, rules only, each returning ALLOW or DROP, stopping at the
first hit. Fail-open: a rule that raises never blocks a candidate.

Ten rules in priority order: scope → dedup → blacklist → robots → protocol →
extension → url_pattern → depth → domain_budget → negative_anchor (off by default).

`PreFilterContext` is supplied by the Frontier and carries the `visited` set, the
`frontier_keys` set and the `domain_counters` dict, plus an `allow_fetch` callback
and `allowed_domains` injected by the scheduler.

### Frontier

Holds every URL discovered and not yet read. Internals:

- `_heap` — a heapq min-heap keyed `(-priority, seq, url_key)`, negated so high
  priority comes out first
- `_items` — `url_key → FrontierItem` for what is in the heap
- `_visited` — the set of url_keys already read
- `_pending` — items held by a gate (domain cooldown, backoff), returned to the
  heap by a periodic `_drain_pending()`
- `_domain_counters` / `_global_counter` — successful fetches, per domain and total

Two gates: **per item**, each `FrontierItem` carries a `next_available_at`; **per
domain**, an external `next_allowed` callback supplied by the RobotsPolicy.

Budgets: `pop_next()` returns None once a domain budget or the global budget is
spent.

Ageing: `effective = priority + age_factor * (now - enqueued_at) / aging_window`,
so low-priority items cannot starve forever.

### Buffer

The unscored half — where candidates wait between harvesting and ranking.

- **Backpressure**: capacity 2000; when full, the lowest-quality candidate is
  evicted (`quality = -depth*0.1 - position*0.001`)
- **Deduplication**: `url_key` against a `_seen` set that persists across drains
- `add()` never blocks; `wait_until()` blocks on an `asyncio.Condition`
- Hands candidates out **a turn per seed, oldest first within a seed**, so one
  loud account cannot take every turn

`ready()` fires on `size >= 100`, non-empty for 30s, or the frontier starving.

### HttpFetcher

Downloads pages. Async httpx, redirect chains followed by hand (the full hop path
is recorded), rotating user agents, exponential backoff up to 3 retries capped at
60s. 429 respects `Retry-After`.

Errors split in two: transient (5xx, timeout, DNS → retry) and permanent (4xx
other than 429 → raise `FetchError`).

### PlaywrightFetcher

The same `Fetcher` Protocol, rendering with a browser and optionally a logged-in
session. It also keeps the XHR payloads a page fetches for itself, which is where
some platforms put the text that never reaches the DOM.

A navigation wait that times out **does not discard the page**. `networkidle` is a
condition some pages never reach — a platform that polls or streams keeps a
request open forever — so a timeout means the condition failed, not the fetch.
Whatever rendered is taken; only an empty document counts as a failure and
retries.

### TrafExtractor

HTML → `Page`. trafilatura on the main path (boilerplate removal, markdown
conversion, metadata), BeautifulSoup as the fallback (title plus plain text).

Publication time gets its own best-effort chain: nine `<meta>` spellings →
JSON-LD `datePublished` at any nesting depth → `<time datetime>`. Relative and
absolute formats are normalised to aware UTC and absurd dates are discarded. When
nothing is found the value is None — it is **never guessed**, because a wrong
guess corrupts the TIME_HORIZON decision.

### Harvester and FeedAdapter

`PageHarvester` asks each `FeedAdapter` whether it claims this page. Instagram
claims by host, RSS by the document's root element, and an ordinary web page is
claimed by nobody — which is itself the answer: fall back to extracting `<a href>`
links.

A claimed listing yields post permalinks; a claimed post is a leaf. A feed entry
arrives **carrying the post text**, so ranking judges content rather than guessing
from an anchor.

### PageAnalyzer

One LLM call per page (text truncated to `ANALYZER_MAX_CHARS`, 3000 by default),
producing:

- a classification (RELEVANT / HUB / AGGREGATOR / IRRELEVANT / NAVIGATION), a
  relevance score and a summary
- the fields the goal declared, **each with a quote from the page**. A quote that
  is not in the text throws the field away; so does a value that is a bare
  negation ("no", "none"), because no sentence on a page can prove an absence —
  and absence is already sayable, by the field not being there
- `endorsed_links`, covered under **Endorsement** below

Results go to the `analyses` table with `prompt_version`, `model` and
`spec_version`, so replays can be compared.

This stage **keeps the model's thinking on by default**: with it off, most of the
extra fields it produces are slot-filling (`benchmarks.md`, 2026-08-22).

On failure `analyze()` returns None immediately and the page goes to an internal
retry queue (up to 3 attempts) in the background; it never blocks the fetch loop.
A `TokenBudgetError` is not retried.

### LLMRanker

Answers "if only so many more links can be read, which ones?".

1. At most 20 candidates per batch (`_RANK_BATCH_SIZE`), split further by
   `LLM_MAX_BATCH_CHARS`
2. The model scores each candidate 0-1 with one clause of reasoning; the clearly
   worthless go to `candidates_to_drop`
3. A failed call retries once (appending "emit valid JSON only"); a second failure
   enqueues the batch flat rather than blocking
4. Thinking is off by default here (`LLM_RANK_REASONING_EFFORT=none`) — see the
   last section of `ranking.md`

**Without LLM credentials this stage does not exist**, and candidates enqueue flat
in the order the frontier hands them out.

### Endorsement

Alongside its verdict, the analyzer names the links on a page worth following.
The engine collects them and, at the next enqueue, resolves each one, runs it
through the PreFilter, and pushes it into the frontier at priority 1.0.

**This is the only way a crawl leaves the platform it started on.** A feed
harvester only ever finds more of the same platform — every candidate in an
Instagram run carries `reg_domain = instagram.com` — so a merchant's own site is
reachable only because the analyzer pointed at it.

### RobotsPolicy

Per-domain fetch policy, three mechanisms together:

1. **robots.txt cache** — 24h TTL per domain, consulted before every request
2. **crawl-delay** — a minimum interval between two requests to one domain
3. **circuit breaker** — five consecutive 429/503 responses cool the domain for
   ten minutes

`allow_fetch(url)` is injected into the `PreFilterContext`.

### SqliteCrawlDb

A per-run SQLite database (aiosqlite), one timestamped directory per run:
`results/<ts>/db/crawl.db`. Raw HTML is content-addressed at
`raw/{url_key}/{fetch_id}.html`.

**No mutable state is shared across runs.** Every database belongs to one task and
expires with its directory.

The methods take pydantic models rather than dicts:

- `save_page(page: Page)` — reads `page.page_id`, `page.title`, `page.url.model_dump()`
- `save_link(candidate: Candidate)` — reads `candidate.candidate_id`, `candidate.url.url_key`
- `save_rank_decision(rd: RankDecision)` — reads `rd.candidate_id`, `rd.priority`, …

Every write goes through a single-consumer `asyncio.Queue`, committing every 200
writes, so there is no concurrent-write race.

### EventEmitter

An append-only event stream covering the whole state machine:
`TASK_STARTED → URL_DISCOVERED → FETCH_STARTED → FETCH_COMPLETED → PAGE_EXTRACTED
→ CANDIDATE_ENQUEUED → CHECKPOINT_SAVED → TASK_PAUSED / TASK_RESUMED → STOPPED`.

### When a page is not content, whose problem is it?

A platform answers a request for a deleted account with 200 and a perfectly
healthy-looking page, so "is this content at all" can only be decided from the
text (`FeedAdapter.problem`). Once decided, it splits two ways, and getting either
side wrong is expensive:

- `UNAVAILABLE` — **about this one account.** Renamed or deleted. Count it, report
  it at the end, and **never stop the run**: monitoring 30 shops, three of them
  gone should not kill the other 27.
- `BLOCKED` / `LOGIN_REQUIRED` — **about this crawl.** Every later request will be
  refused the same way, and on a platform that keeps score, continuing to knock
  turns a rate limit into a ban. **Stop on the first one.**

The distinction lives on `PageProblem.refuses_the_run`, and it is written **by
exclusion**: a fourth kind of problem stops the run by default until somebody says
it should not. Being loud about an unfamiliar refusal is the cheaper of the two
mistakes.

The harvester carries the verdict out through `Harvest(candidates, problem)`.
Before that existed, the only thing it could say was an empty list — which made
"this account posted nothing this week" and "the platform refused us" the same
answer, and **a rate-limited run reported a quiet week, every week**.

---

## Data model

Core objects are pydantic models (serialisable, validated); counters are a
`@dataclass` (mutable, updated constantly).

| Model | Kind | What it holds |
|-------|------|---------------|
| `CrawlGoal` | BaseModel | The goal and its budgets. `goal_id = sha256(prompt)[:12]` — content-derived, so the same text is the same goal, which is what replay idempotency rests on. `keywords` / `since` are filled in by the GoalEnhancer |
| `URL` | BaseModel | raw / canonical / url_key / reg_domain / scheme / host / path / query |
| `RawLink` | BaseModel | Link-extractor output, not yet canonicalised |
| `Candidate` | BaseModel | A canonicalised candidate with its source page, depth and status; a feed entry also carries the post text |
| `FrontierItem` | BaseModel | An enqueued item: priority, retry state, domain gating |
| `FetchResult` | BaseModel | Status code, redirect chain, raw bytes |
| `Page` | BaseModel | The parsed page: markdown, `raw_html_path`, `published_at` (None when the page does not state one) |
| `AnalysisResult` | BaseModel | The verdict, the summary, and the extracted fields with their evidence |
| `RankDecision` | BaseModel | priority, rationale, which ranker, dropped flag |
| `RankHistorySummary` | BaseModel | A compact "what has been seen so far" |
| `CrawlTask` | BaseModel | Task lifecycle state |
| `FrontierSnapshot` | BaseModel | The checkpoint payload: heap, pending, visited, budgets |
| `CrawlCounters` | **dataclass** | Runtime counters |

```python
@dataclasses.dataclass
class CrawlCounters:
    max_pages: int = 0
    max_tokens: int = 0
    max_duration_sec: int = 0
    relevance_threshold: float = 0.7
    pages_fetched: int = 0
    tokens_used: int = 0
    started_at: float = 0.0
    in_flight: int = 0
    # Fixed-length sliding window; DIMINISHING_RETURNS reads it
    relevance_window: deque[bool] = field(default_factory=lambda: deque(maxlen=20))
    fatal_error: str = ""
    # Diagnostic mode: nothing is discarded, the rejects rank last
    recall: bool = False
    # Time horizon; the whole check sleeps when since is None
    since: datetime | None = None
    stale_streak: int = 0
    max_stale_streak: int = 5
```

---

## Stop conditions

`check_stop()` runs every cycle. Every check is independent and **all of them can
fire**; it returns every reason that did.

| Kind | Condition | Code |
|------|-----------|------|
| Budget | pages exhausted | BUDGET_PAGES |
| Budget | tokens exhausted | BUDGET_TOKENS |
| Budget | time exhausted | BUDGET_TIME |
| Natural end | both halves empty, nothing in flight, nothing being scored | FRONTIER_DRAINED |
| Natural end | the above, and a candidate was refused by a domain ceiling along the way | plus DOMAIN_BUDGET |
| Enough | relevant results reached `--max-relevant` | MAX_RELEVANT |
| Time window | a single-entry-point run walked past `--since` | TIME_HORIZON |
| Diminishing returns | fewer than 2 relevant in the last 20 pages | DIMINISHING_RETURNS |
| User | `task.state == "STOPPING"` | USER_REQUESTED |
| Adapter failure | three or more listings read, none yielding anything | plus ADAPTER_EMPTY |
| Platform refusal | the first BLOCKED page | RATE_LIMITED |
| Platform refusal | the first LOGIN_REQUIRED page | LOGIN_REQUIRED |
| Fatal | `counters.fatal_error` is set | FATAL |

**GOAL_SATISFIED was removed.** It stopped the whole run once the relevance window
held N hits, but "stop after N" contradicts "find as many as the budget allows",
and the budget conditions already cover finishing normally.

**DIMINISHING_RETURNS actually fires now.** `relevance_window` used to be declared
and read but never written, which made it dead. `engine._on_analysis` now writes
`relevance_score >= goal.relevance_threshold` into it, and the window is a
`deque(maxlen=20)` so "the last 20 pages" is guaranteed by the type rather than by
the caller remembering to trim.

**It is suppressed under `--recall`.** That mode deliberately reads the candidates
the ranker rejected, and reads them last, so a tail of misses is the point of the
mode rather than evidence the crawl is finished. Stopping on it cut off exactly
the stretch the run was made to measure.

**TIME_HORIZON** assumes traversal in reverse chronological order (a feed, a
listing, an archive): the first item older than the window means everything after
it is older too. Pages in a link graph have no order, so **passing `--since` is the
user asserting the source is ordered**, and the check only arms itself for runs
with a single entry point.

Three implementation rules: with `since=None` the whole check sleeps, so existing
runs are unaffected; a page that reports no date **neither advances nor resets**
the streak, because silence is not evidence either way; and an absurd date (before
1990, or more than a year ahead) is treated as no date at all, so template
leftovers cannot poison the decision.

---

## Concurrency

One process, asyncio. `fetch_pump` and `rank_pump` run concurrently. Fetching is
bounded by `asyncio.Semaphore(fetch_concurrency)` and LLM calls by
`asyncio.Semaphore(llm_concurrency)`, and **the two are independent**.

That sentence used to be false. The per-page analyzer call ran inside the fetch
semaphore's critical section, so a page waiting on an LLM held a fetch slot and
the two knobs were effectively nested — the inner one starved the outer one and
HTTP fetching stalled behind analysis. The fetch slot now covers only the network
request and the parse it feeds (`_fetch_and_extract`); analysis runs outside it.

Per-domain serialisation is enforced through `next_available_at`. HTML parsing
goes through `asyncio.to_thread`, and every lxml/libxml2 parse is serialised by a
global lock in `digest/lxml.py` — libxml2's global dictionary has a concurrency
race that produced a SIGABRT. Writes go through a single-consumer queue.

Backpressure: the candidate buffer is bounded at 2000 and evicts the
lowest-quality candidate when full.

---

## Error handling

Two categories throughout: transient (retry) and permanent (mark failed).

| Stage | Error | Policy |
|-------|-------|--------|
| Fetch | timeout / 5xx | Exponential backoff, up to 3 attempts |
| Fetch | 429 | Respect `Retry-After`, cool the domain |
| Fetch | 404 / 403 / SSL | Permanent → `FetchError` |
| Fetch | wait condition timed out | Keep whatever rendered; only an empty document fails |
| Domain | more than 5 consecutive failures | Circuit breaker, 10-minute cooldown |
| Extract | parse failure | Degrade to DEGRADED/FAILED, do not interrupt |
| Extract | timeout | `asyncio.wait_for`, mark SKIPPED |
| Rank | LLM failure | Retry once; then enqueue the batch flat, without blocking |
| Analyze | LLM failure | Background retry queue, up to 3 attempts; never blocks fetching |
| Storage | write failure | Retry 3 times → checkpoint and PAUSE |

---

## Observability

- **Structured logs** — JSON lines with `task_id` / `url_key` / `stage` / `event`
- **Event stream** — the append-only `events` table, covering the state machine
- **Counters** — live on `CrawlCounters`: `pages_fetched`, `tokens_used`, `in_flight`
- **Token accounting** — `TokenBudget` separates input, output, the input a
  provider served from its cache, and the output the model spent thinking. A total
  that does not separate them is not a bill: cached input costs about a tenth of
  fresh input, and on one measured run 84% of all output was thinking

---

## Storage

SQLite (aiosqlite), one database per run:

| Table | What it holds |
|-------|---------------|
| `crawl_goals` | Goals and budgets |
| `crawl_tasks` | Task lifecycle |
| `urls` | The URL dedup table |
| `pages` | Page content (replayable), including `published_at` |
| `links` | Candidates as discovered — one row per discovery, not per candidate |
| `rank_decisions` | Ranking records, auditable |
| `analyses` | Analysis results, append-only |
| `frontier_snapshots` | Checkpoints |
| `events` | Event sourcing |
| `errors` | Error audit |
| `robots_cache` | robots.txt cache |

---

## What extends without touching the architecture

New page types (PDF, GitHub), a different analyzer, a different ranker, a new URL
source, a different LLM, learning from user feedback. All of it goes through the
Protocols.
