# Benchmark

The evidence factory for the ranking pipeline. It answers one question with two independent witnesses: **does the embedding ranker actually beat the plain rule ranker?**

- **Behavioral run**: two identical 300-page crawls (rule-only vs local embedding), compared on focus: where did the budget go?
- **Labeled eval**: 300 real candidates, gold-labeled by hand, scored by the production rankers with NDCG / precision / recall / AP.

Headline numbers so far: embedding keeps **72% of pages on the seed domain vs 30%** for rule-only, and wins **AP 0.89 vs 0.67** on the labeled set.

## The scripts

| Script | What it does | Keep around? |
|--------|-------------|--------------|
| `run_bench.sh` | The main event. Runs the crawl twice (rule baseline + local embedding), writes per-run summaries and a side-by-side comparison. | Yes: this is how you re-run the benchmark |
| `summarize.py` | Extracts stats from one run dir (pages, candidates, sim percentiles, domain spread). Called by `run_bench.sh`. | Yes |
| `build_eval_set.py` | Samples 300 candidates from finished run DBs in stratified layers and drafts labels by keyword rules. | Yes: deterministic (seed=42), regenerates the eval set |
| `review_dump.py` | Prints one line per eval candidate for human spot-checking. | Yes: how you audit labels |
| `apply_labels.py` | Applies a `[index, relevant]` patch to the eval set. | Yes: how label corrections land |
| `score_eval.py` | Scores the eval set with the REAL RuleRanker and EmbeddingRanker, prints the metric table. | Yes: the number-maker |

One-shot tools that served their purpose and were deleted: `enrich_eval.py` (only 13/300 candidates had fetched pages to attach) and the intermediate dump files.

## Data

| File | What |
|------|------|
| `data/embedding_eval.json` | The gold-labeled eval set: 300 candidates, 160 relevant / 140 irrelevant. `labeler: "gold-review"`. |
| `data/label_patch.json` | Audit trail: the 36 corrections applied over the keyword-drafted labels. |

Results of runs live in `results/` (gitignored): `<timestamp>/bench_summary.json` per run, `bench_comparison.json` for the head-to-head, `eval_scores.json` for the labeled metrics.

## How to run it

```bash
# Behavioral: two 300-page crawls, ~10-16 min (rule vs local MiniLM)
benchmark/run_bench.sh

# Same, but compare rule against another model
benchmark/run_bench.sh --embedding-model BAAI/bge-m3
benchmark/run_bench.sh --embedding api --embedding-model text-embedding-3-small

# Labeled metrics on the existing eval set
python3 benchmark/score_eval.py

# Spot-check the labels, fix wrong ones
python3 benchmark/review_dump.py > /tmp/review.txt   # eyeball it
# then write a patch and apply:
#   python3 benchmark/apply_labels.py /path/to/patch.json
```

The eval set is a living asset: it's tracked in git, corrections are cheap to apply, and `score_eval.py` re-runs in seconds, so every ranking change gets a before/after number.

## The fine print

The 300 labels are Deepseek's judgment (anchors + URLs, plus page excerpts where the runs actually fetched the page), corrected over keyword-rule drafts. Good enough to catch big regressions; spot-check the ambiguous single-word anchors if you're about to make decisions off them. The behavioral numbers need no labels at all and carry no such caveat.
