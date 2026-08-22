# Does the embedding stage earn its place?

This branch is an archive. It holds the investigation that answered
that question, and the answer was no. Nothing here is meant to be
merged; it is kept so the next person does not have to measure it
again.

The work started as "migrate the embedding model" and turned into
"find out whether the cheap half of the ranking funnel does anything
at all". It does not, and the reasons are worth writing down.

---

## The short version

**Two layers of the ranking funnel have never done anything.** The
rule ranker's threshold is 0, so it drops nothing. The embedding
stage's top-K gate keeps 60 candidates out of batches that hold at
most 20, so it drops nothing either. Whatever they score is then
overwritten by the LLM stage, which does not blend but replace.

In a normal run, **96% of discovered candidates reach the LLM ranker
unfiltered**, and the LLM is the only stage that removes anything (it
drops 68%). The cheap funnel that was supposed to prevent this has
been inert since it was built.

**Ranking quality, measured over seven crawls against the analyzer's
own verdicts** (AUC: the chance a random relevant candidate outranks a
random irrelevant one; 0.5 is a coin flip):

| ranker | median | worst | cost |
|---|---|---|---|
| embedding, cosine to the goal | 0.550 | **0.349** | free |
| rule | 0.560 | 0.485 | free |
| embedding, direction learned online | 0.619 | 0.479 | free |
| **LLM** | **0.890** | **0.687** | **35% of the run's tokens** |

The free tier tops out around 0.62. On four of the seven tasks the
shipped embedding ordering is at or below random, and on one it is
0.349 — it ranks relevant candidates *last*. There is no free
replacement for the LLM ranker in this data.

---

## How the funnel actually behaves

`HybridRanker.rank_batch` runs three stages inside one call and
returns one decision list. Nothing intermediate reaches the frontier.

```
candidate pool --take 20--> rank_batch()          ← one call, three stages inside
                      ├─ rule       scores, threshold 0, drops nothing
                      ├─ embedding  scores, blended 0.8/0.2 with rule
                      └─ LLM        scores, _merge OVERWRITES the rest
                           ↓
                    one final decision
                           ↓
                    priority queue → fetch
```

Three constants, in three files, that nobody reconciled:

| constant | where | effect |
|---|---|---|
| `_RANK_BATCH_SIZE = 20` | `scheduler/engine.py` | at most 20 candidates per ranking call |
| `embedding_keep = 60` | `config.py` | keep the top 60 of them — i.e. all of them |
| `RuleRanker(threshold=0.0)` | `scheduler/factory.py` | drop nothing |

Empirical confirmation: across every run inspected, `rank_decisions`
contains **zero rows tagged `rule` and zero tagged `embedding`**. Every
decision that survives to storage carries the `llm` tag, because the
LLM stage overwrote all of them.

The `0.8/0.2` blend has three independent reasons to be deleted:

1. With an LLM stage present it never reaches the output (dead code).
2. Its provenance is a sweep over a *survivor* set — its headline was
   AP 0.994, which is what survivor bias looks like, not what a ranker
   is worth. The script that produced it no longer exists.
3. In the one configuration where it *would* take effect (no LLM
   stage), it is measurably harmful: pure learned direction 0.908,
   blended 0.894, and worse as the rule weight rises.

---

## What was tried, in order

### 1. Migrating the model: paraphrase → E5

The shipped model was `paraphrase-multilingual-MiniLM-L12-v2`, trained
on paraphrase pairs — it answers "are these two sentences the same
thing". Feed ranking asks the asymmetric question: "does this post
satisfy this need". E5 is trained contrastively on (query, passage)
pairs with literal `query:` / `passage:` prefixes, which is that
question.

fastembed 0.8.0 ships no card for `multilingual-e5-small` (only the
2.24GB large), but `TextEmbedding.add_custom_model` registers one at
runtime — same ONNX runtime, no torch, 384 dims.

Measured on 114 Instagram candidates labelled by the analyzer:

| | AP | beats random |
|---|---|---|
| random ordering (median of 3,000) | 0.388 | — |
| MiniLM, 512-char cap (what shipped) | 0.382 | **44%** |
| e5-small + prefixes, 512-char cap | 0.544 | 99.7% |
| **e5-small + prefixes, 1600-char cap** | **0.615** | **100%** |
| e5-small, no prefixes | 0.523 | 98.8% |

Three things this bought:

- **The prefixes are worth 0.092 AP.** They are not decoration; they
  are the literal strings E5 saw in training.
- **The character cap was a real bottleneck.** The old model's
  tokenizer truncated at 128 tokens (≈447 characters) while the code
  capped at 512 characters, so the model read about a quarter of a
  long caption. e5-small truncates at 512 tokens. Raising the cap from
  512 to 1000 characters is worth 0.077 AP; past that it flattens.
- **The shipped configuration was below random.** MiniLM's 0.382 sat
  under the random median of 0.388 and beat only 44% of 3,000 random
  orderings. Replicated on a second, independent crawl: 0.384, 42%.

This part of the work is real and was kept. It just does not matter in
the default configuration, because the embedding ordering is
overwritten before anything consumes it.

### 2. Asking whether cosine was the problem

It is not the metric, and it is not the geometry.

| | run A | run B |
|---|---|---|
| cosine to the goal | 0.673 | 0.724 |
| **Euclidean distance** | **0.673** | **0.724** |
| after subtracting the candidate-set mean | 0.423 | 0.435 |
| after removing the top 1 / 2 / 3 principal components | 0.433 / 0.439 / 0.467 | 0.491 / 0.511 / 0.525 |
| **a direction learned from labels (same dot product)** | **0.955** | **0.923** |

Euclidean distance on L2-normalized vectors is monotonically equivalent
to cosine, so it cannot change any ordering — that closes the "try a
different distance" route. Removing the common component makes things
*worse*, below random: the shared component is not noise, it is the
only thing cosine was reading ("is this post about bubble tea at all"),
and every candidate shares it.

The last row is the point. **Same vectors, same dot product** — only
the reference vector changed, and AUC went from 0.67 to 0.96. The
information was always in the embedding. The goal statement is simply
not the direction along which relevance varies.

### 3. Learning the direction from the analyzer's verdicts

The analyzer already labels every page it reads. Average the vectors
of the relevant candidates, average the irrelevant ones, subtract:
that difference, normalized, is a direction. Score a new candidate by
its dot product with it. Two means and a subtraction — no training
loop, no hyperparameters, no new dependency.

**Trained on a whole run and tested on the same run** (leakage; this is
the ceiling, not an achievable number), it reaches 0.87–0.96 on *all
seven* tasks, including graph-mode candidates that carry no text at all
and a task with only 9 positive examples. The signal is there in every
case.

**Then the realistic simulation, replaying batches in time order and
letting the direction see only labels that had already arrived:**

| task | cosine | online direction | warm start (≥0.90) | LLM |
|---|---|---|---|---|
| bubble tea A | 0.724 | 0.795 | **0.939** | 0.890 |
| bubble tea B | 0.673 | 0.754 | 0.908 | 0.914 |
| store info | 0.533 | 0.704 | 0.781 | **0.888** |
| coffee | 0.349 | 0.518 | 0.769 | 0.772 |
| databases (graph) | 0.486 | **0.479** | no donor | 0.924 |
| ML papers | 0.550 | **0.512** | no donor | 0.687 |
| software releases | 0.738 | **0.619** | no donor | 0.961 |

**Online learning helped four tasks and hurt three**, and it hurt worst
exactly where cosine was working: software releases fell from 0.738 to
0.619. Too few labels make a noisy centroid, and a direction pointing
slightly wrong is worse than not learning at all.

### 4. Reusing a direction across runs, keyed by goal similarity

Goal vectors separate far more cleanly than candidate vectors do, so a
threshold is actually possible here. Over 42 ordered task pairs:

| goal cosine | pairs | median gain | worst | harmful |
|---|---|---|---|---|
| ≥ 0.95 | 6 | +0.225 | +0.105 | **0%** |
| 0.90–0.95 | 4 | +0.195 | +0.103 | **0%** |
| 0.85–0.90 | 6 | +0.155 | −0.196 | 33% |
| 0.80–0.85 | 24 | −0.061 | **−0.440** | **67%** |

Correlation between goal cosine and transfer gain is r = 0.639 —
usable, not reliable. **0.90 is the right cut** (same 0% harm as 0.95
while accepting 67% more pairs); below 0.85 transfer is actively
harmful two times in three, worst case −0.440.

Two things kill this as a product feature anyway:

- **Three of seven tasks had no qualifying donor at all.** Most new
  goals cannot warm start, so the machinery would sit unused.
- **Where it was possible, it beat the LLM twice, tied once, and lost
  once** — store info reached 0.781 against the LLM's 0.888. "At least
  as good as the LLM" does not hold.

### 5. Self-validation, the last attempt

The failure mode was specific: a direction learned from too few labels
is worse than none. The system has labels, so it can check itself —
hold some out, learn on the rest, and adopt the direction only if it
beats cosine on the held-out part.

It rescued one of the three failures (ML papers, 0.512 → 0.583) and
left the other two: databases unchanged at 0.479, software releases
0.619 → 0.630 against a cosine baseline of 0.738 and a rule baseline of
0.829. The validation split is itself noisy at these sample sizes, and
under `--recall` the early and late batches are not drawn from the same
distribution, so the check does not hold.

---

## Where the tokens actually go

A separate finding, and the more actionable one. Measured over three
runs of 120 pages:

| | share of run | per unit |
|---|---|---|
| LLM ranking | 26–35% | ~530 tokens/candidate |
| page analysis | 65–74% | ~2,000–2,600 tokens/page |

**Most of the ranking cost is not the candidates.** Fitting batch cost
against batch contents gives roughly **2,400 tokens fixed per batch
plus 195 per candidate**. On a 17-batch run that fixed part is 40,800
tokens — **11% of the whole run** — spent re-sending the same prompt.

The clearest case is graph mode, where candidates carry no text at all:
one run spent **103,694 tokens ranking bare links**, about 317 tokens
per URL, essentially all of it prompt.

The analyzer has the same shape: ~522 tokens of system message plus the
goal and field list, identical on all 120 calls, in front of ~1,000
tokens of page text.

**And the token counts overstate the cost.** The configured provider
caches repeated prefixes automatically, and the prompt is assembled
prefix-stable (system → goal → fields → page → text). But
`llm/client.py` records only `prompt_tokens` and `completion_tokens`,
never the cache hit/miss split, so cached and uncached tokens — an
order of magnitude apart in price — are added together into one
number. **Nobody knows what these runs actually cost.**

---

## Method notes

Things that went wrong in the measuring, kept because they would go
wrong again:

- **Survivor bias.** Scoring an ordinary run measures only candidates
  something else already kept — the set is ~78% relevant by
  construction and every ranker looks equally good. Every number here
  comes from a `--recall` run, where nothing is discarded.
- **`--recall` fights the stop conditions.** A recall run reads its own
  rejects last, so its tail is dry, and `DIMINISHING_RETURNS` (fewer
  than 2 relevant in the last 20 pages) ends it early — cutting off
  exactly the stretch being measured. Three of four calibration runs
  died this way before the fix.
- **Simpson's paradox.** Mixing candidates that carry their own text
  with ordinary links inverted the conclusion: the mixed number said
  embedding beat the rule ranker five to one, while split it was 0.884
  vs 0.769 the other way on feed entries.
- **Duplicate rows.** `links` records one row per *discovery*, so a
  candidate found on four pages was scored four times. One irrelevant
  post with four copies moved AP by 0.05.
- **Tie-breaking.** The rule ranker produces many ties (64 distinct
  values over 114 candidates), so its AP moves ±0.008 with database
  row order. Not enough to change a conclusion, enough to explain a
  number that will not reproduce exactly.
- **Leakage through cross-validation.** 5-fold CV lets the direction
  see labels from later in the run. It reported 0.922 where the honest
  time-ordered replay gave 0.754.
- **A shared model inflates the LLM's score.** The ranker and the
  analyzer that labels it run on the same model, so part of the LLM
  ranker's 0.914 is self-agreement. That biases *in favour* of the
  LLM, so the conclusion above is the conservative one.

---

## What was decided

- **Keep the LLM ranker.** It is the only stage that ranks reliably
  across all seven tasks, and there is no free substitute.
- **Delete the machinery that never ran**: the top-K gate, the 0.8/0.2
  blend, and — following this investigation — the rule and embedding
  stages themselves, along with the steering subsystem.
- **Do not build cross-run direction reuse.** Its precondition failed.
- **Measure the cache split before optimizing anything else.** Token
  count is not cost, and right now only the token count is recorded.

## Reproducing

`benchmark/embedding/` holds the scorer. The runs behind every number
above are archived at `archive/benchmark-runs-20260822.tar.gz`
(databases and logs; the raw HTML was left out at 402MB).

```bash
python benchmark/embedding/score.py <run-dir>
python benchmark/embedding/score.py <run-dir> --model <another-model>
```

Scoring is offline: it re-embeds from stored candidate text and the
analyzer's stored verdicts, so sweeping a setting costs one local model
pass and never touches the network or an API.
