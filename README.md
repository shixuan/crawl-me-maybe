# Crawl me maybe

[![ci](https://github.com/shixuan/crawl-me-maybe/actions/workflows/ci.yml/badge.svg)](https://github.com/shixuan/crawl-me-maybe/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-750014)](LICENSE)

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
```

Four kinds of run, one command. What changes is what the seeds point at.

**A link graph.** Start anywhere, follow links, stop when you have enough.

```bash
crawl run "recent funding news for AI startups" \
  --seeds "https://news.ycombinator.com,https://techcrunch.com" \
  --max-relevant 40 --page-budget 200
```

**A feed.** A feed URL is an ordinary seed: it is fetched once, and whichever
adapter recognises the document reads it. An entry is not a bare link -- it
carries the title, the publication time, and usually the post itself -- so the
ranker judges the text before anything else is fetched.

```bash
pip install -e '.[rss]'

crawl run "language features shipped this year, with the version that carries each" \
  --seeds "https://blog.rust-lang.org/feed.xml,https://go.dev/blog/feed.atom" \
  --max-relevant 20
```

**A platform that needs a browser but no account.** Reddit builds its pages
with a script, so plain HTTP gets the shell: no posts, no error, nothing to
read. Nothing to pass, though -- the adapter says its pages need rendering, so
those addresses go through a browser and everything else keeps taking the
cheap route. A crawl that mixes the two pays for a browser only on the pages
that need one, and one that never meets a platform never starts one.

Reading Reddit needs no session: a subreddit is open to strangers.

```bash
pip install -e '.[browser]' && playwright install chromium

crawl run "what is worth doing in Toronto this weekend, with the event, the place and the date" \
  --seeds "https://www.reddit.com/r/askTO/" \
  --max-relevant 20 --page-budget 60
```

`--fetcher browser` still exists and still means *everything* through a
browser. A page that is no platform at all can need a script run before it
says anything, and only the person crawling it knows that.

**A login-walled platform.** Log in once by hand; the session file is what
enables the platform adapters, and it also defaults `--domain-budget 0`, since
every candidate on a platform shares one host and a per-domain ceiling would be
a ceiling on the crawl. The platform is read through the browser context holding
the cookies; a site the analyser endorses from there is not, because the
credentials mean nothing to it. How deep to go is left to `--depth-limit`, which
has to cover the way out as well as the platform. Seeds on such a platform without one are refused,
because a logged-out crawl fetches login pages and reports them as an empty
platform.

```bash
pip install -e '.[browser]' && playwright install chromium

crawl session ./ig-session.json --feed instagram

crawl run "nearby merchants giving something away, with the shop, the offer and the deadline" \
  --seeds ./accounts.json \
  --session ./ig-session.json \
  --depth-limit 2 \
  --max-relevant 40 --page-budget 150 \
  --since '2 weeks' --ignore-robots
```

Two, because that is what this goal needs: an account, a post, and the shop's
own site where the deadline is usually written. Leaving it at the default of 5
is not wrong, only more expensive -- past the shop the crawl is on the open web,
where a budget goes quickly.

Then read what it found:

```bash
python dashboard/serve.py
```

| Flag | Values | Default | Meaning |
|------|--------|---------|---------|
| `--port` | int | `8765` | Bound on `127.0.0.1` only |
| `--results-dir` | path | `results` | Where run directories live |

---

## Optional installs

The base install crawls a link graph. Two paths cost more than every user
should carry, so they are extras, and the flags that need them say so before
a run starts rather than failing partway through it.

| Extra | Install | What it buys | Cost |
|-------|---------|--------------|------|
| `rss` | `pip install -e '.[rss]'` | Reading a feed among the seeds; its entries arrive with their own text | feedparser, 0.3MB |
| `browser` | `pip install -e '.[browser]'`<br>then `playwright install chromium` | `--fetcher browser`, `--session`, `crawl session`, and any platform whose pages have to be rendered | 135MB package, ~650MB browser |

Both together: `pip install -e '.[rss,browser]'`.

Without them the crawl still runs; the flags that need them are refused,
naming the flag you typed and the one command that fixes it. A platform met
mid-crawl is the one case that is not refused: it degrades to plain HTTP and
says so, because one unreachable link is not a reason to end a crawl.

---

## CLI

### `crawl run "<prompt>"`

The prompt is the goal, in your own words. Naming the fields you want ("with
the shop, the offer and the deadline") is what makes the analyzer extract them.

**Where to start**

| Flag | Values | Default | Meaning |
|------|--------|---------|---------|
| `--seeds` | comma-separated URLs, or a path | *required in practice* | Where the crawl begins. Anything not starting with `http://` or `https://` is read as a JSON file: a list of URLs, or `{"seeds": [...], "allowed_domains": [...]}` |
| `--allowed-domains` | comma-separated domains | none | Registrable domains the crawl may not leave. Outranks the same key in a seeds file |

**How far to go**

| Flag | Values | Default | Meaning |
|------|--------|---------|---------|
| `--depth-limit` | int | `5` | Hops from a seed. A listing and its posts are two; a site an analyser endorsed off a post is three |
| `--since` | `"2 weeks"`, `"3 days"`, `2026-08-01` | none | Time window. Candidates a listing dated before it are dropped; with a single seed, the run also stops once content ages out |
| `--draining` | flag | off | Ignore the page budget and stop when the frontier runs dry. Mutually exclusive with `--page-budget` |

**How to fetch**

| Flag | Values | Default | Meaning |
|------|--------|---------|---------|
| `--fetcher` | `http` \| `browser` | per candidate | Left alone, each address takes the cheaper route its platform allows. `browser` forces one everywhere, for pages that are empty without a script run but belong to no platform |
| `--session` | path | none | A Playwright `storage_state` file. Enables the platform adapters, sends their addresses through the browser context holding it, and defaults `--domain-budget 0` |
| `--ignore-robots` | flag | off | Bypass robots.txt |

**What to spend**

| Flag | Values | Default | Meaning |
|------|--------|---------|---------|
| `--max-relevant` | int | `0` (no target) | Stop once this many pages are judged relevant. The only condition that states a goal rather than a ceiling |
| `--page-budget` | int | `500` | Pages this run may read; `0` means no limit |
| `--token-budget` | int | `500000` | LLM tokens across every stage |
| `--time-budget` | int seconds | `3600` | Wall clock |
| `--domain-budget` | int | `50`, or `0` with `--session` | Pages one registrable domain may contribute; `0` means no ceiling |

**How to judge**

| Flag | Values | Default | Meaning |
|------|--------|---------|---------|
| `--recall` | flag | off | Diagnostic. Nothing the ranker rejects is removed, only ranked last, so a finished run can be asked whether the rejections were right. Not for production: one measured run went from 87% to 37% hit rate |
| `--analysis` | `on` \| `off` | `on` | Per-page analysis: one LLM call per page for a verdict and the goal's fields. `off` disables it |
| `--analyzer-max-chars` | int | `3000` | Page text sent to the analyzer per page |

**Everything else**

| Flag | Values | Default | Meaning |
|------|--------|---------|---------|
| `--result-dir` | path | `results` | Where run directories go |
| `--log-level` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` \| `OFF` | `INFO` | Overrides env `LOG_LEVEL` |

Flags left off fall back to the environment, then to the defaults above.

### `crawl session <path>`

Opens a real browser at the platform, waits while you log in, and saves the
session. Your credentials are typed into the platform's own page and never
reach this process; what lands on disk is the session that login produced.

| Flag | Values | Default | Meaning |
|------|--------|---------|---------|
| `--feed` | `instagram` | the only walled platform | Which platform to open |
| `--force` | flag | off | Replace an existing session file |
| `--timeout` | int seconds | `600` | How long to wait for the login |

A visible browser needs a desktop: WSLg on WSL, an X display over SSH.

### `crawl inspect <task-id>`

Read-only look at a finished run: goals, pages, analyses by classification,
the top relevant pages.

| Flag | Values | Default | Meaning |
|------|--------|---------|---------|
| `--goal` | goal id | the task's own goal | Which goal's analyses to show |
| `--export` | `json` \| `csv` | none | Dump the pages-and-analyses join to stdout. `json` carries the extracted fields and their evidence; `csv` leaves them out, because every goal declares its own fields and there is no stable column set |

### `crawl replay <task-id>`

Re-analyze a finished run's stored pages under a new prompt. No fetching, so
a better prompt costs only the analyzer.

| Flag | Values | Default | Meaning |
|------|--------|---------|---------|
| `--prompt` | string | the original | New goal; its analyses land under a new goal row |
| `--limit` | int | all | Re-analyze at most this many pages |
| `--max-tokens` | int | `500000` | Token budget for the replay |
| `--analyzer-max-chars` | int | `3000` | Page text per analyzer call |
| `--force` | flag | off | Re-analyze pages that already have an identical analysis |
| `--log-level` | as above | `INFO` | |


---

## How it works

**A page is read by whoever claims it.** Each adapter is asked whether a
fetched page is theirs: Instagram answers from the host, RSS from the
document's root element. Nobody claiming is the ordinary case and the graph's
answer -- read the links. So one run can hold posts, feed entries and ordinary
web pages, and adding a platform is one adapter, not a new mode.

**Two stages, and only two.** A structural filter, then one that reads:

```
~200 links per page
  │
  ▼  Pre-filter   URL-level rules, zero LLM   → 10-30 candidates
  ▼  LLMRanker    one batched call per 20     → priority, or dropped
```

There were two cheap ranking stages between them, scoring keywords and cosine
similarity. Measured over seven crawls against the analyzer's own verdicts,
neither ever removed a candidate -- a top-K of 60 cannot cut a batch of 20 --
and neither ordered better than a coin flip on most tasks. They were removed
rather than tuned; the measurements are on the `archive/embedding-investigation`
branch. Without LLM credentials there is now no ranking stage at all, and the
crawl fetches in frontier order: a turn from each seed, oldest first.

**Ranking predicts; analysis verifies.** Every fetched page gets one analyzer
call: classification, summary, relevance, and the fields the goal asked for.
Each extracted value is checked against the page text before it is stored, and
a field the page does not state is simply absent -- there is no "unknown".
The analyzer also names the links on a page worth following, and those are
injected directly. It is the only way a crawl leaves the platform it started
on: a feed harvester only ever finds more of the same platform, so a merchant's
own site is reachable only because the analyzer pointed at it.

**Two loops, one meeting point.** `fetch_pump` downloads and harvests;
`rank_pump` scores. They coordinate only through the Frontier, which owns both
halves: candidates waiting for a score, and scored candidates waiting for a
fetch slot. Fairness lives upstream of the ranker -- a turn from each seed, so
one loud account cannot spend the whole LLM budget -- and priority downstream,
where the scarce thing is the page budget instead.

**A run says why it stopped.** Budgets, a target met, diminishing returns, a
drained frontier -- and the ones that exist because silence was the bug:
`RATE_LIMITED` and `LOGIN_REQUIRED` when the platform refuses the crawl,
`ADAPTER_EMPTY` when every listing parsed and held nothing, which is what a
platform redesign looks like from inside. All of them are reported together,
so "completed" never has to stand in for "found nothing and cannot say why".

**Everything is recorded.** Which rule dropped a link, what each ranker scored
it, which model and prompt version produced a judgment, and the sentence each
extracted value came from. Raw HTML is kept, so a better prompt can re-judge a
finished run without re-crawling.

---

## Configuration

Flags say what this run is doing; `.env` says what this machine and account can
do — credentials, endpoints, which model, how much memory to spend. Everything
has a default, so `.env` is optional.

See [`.env.example`](.env.example) for the full list.

---

## Status

| Version | State | What it adds |
|---------|-------|--------------|
| v0.1 | ✅ | Full pipeline at zero LLM cost |
| v0.1.1 | ❌ | (deprecated) EmbeddingRanker, semantic ranking on a local model. (see archive/embedding-investigation branch) |
| v0.2 | ✅ | Goal Enhancer, LLMRanker, per-page analysis, replay, inspect, time horizon |
| v0.3 | ✅ | Playwright with login state, feed traversal, extracted fields with evidence |

---
