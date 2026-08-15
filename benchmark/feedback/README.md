# Feedback benchmark

Does the feedback subsystem earn its tokens? One shared LLM judge, N replicate runs, medians as the verdict.

## The experiment

Every arm runs the same goal, seeds, and page budget. Only the feedback configuration differs:

| Arm | Config | Question it answers |
|-----|--------|--------------------|
| `off` | `--analysis off` | The clean baseline: no analyzer, no signals, no prior load |
| `C3000` | default (analyzer text cap 3000 chars) | Current production shape |
| `C4500` | `--analyzer-max-chars 4500` | Where does quality bend? |
| `C6000` | `--analyzer-max-chars 6000` | The previous default |
| `C12000` | `--analyzer-max-chars 12000` | Does more text ever help? |

The judge then labels every unique fetched page with the same classification schema but a 12k-char text cap, so its verdict has more information than any steering decision. A page counts as a hit when the judge says `RELEVANT` and scores `relevance >= 0.7`.

Every invocation creates a fresh timestamped run set under `results/bench/<goal-hash>/<timestamp>/<arm>/`, so ten invocations are ten independent data points. Judge labels live in `data/judge_<goal-hash>.json` shared across runs: each URL is judged once, later runs only pay for new pages.

## Usage

```bash
# Full pipeline: run arms -> collect -> judge -> report
python3 benchmark/feedback/feedback_bench.py

# Resume an interrupted run set (skips completed arms in it)
python3 benchmark/feedback/feedback_bench.py --resume 20260815_093000

# Report only, from existing data
python3 benchmark/feedback/feedback_bench.py --report-only

# Custom task
python3 benchmark/feedback/feedback_bench.py \
    --goal "recent funding news for AI startups" --seeds "https://news.ycombinator.com" \
    --max-pages 100 --caps 3000,4500,6000
```

The report prints the per-run history, medians, ranges, token cost, and each arm's median delta vs `off`. Single runs are data points; medians are the verdict.

## Metrics

- **Precision**: judge hits among the first k fetched pages (@5/@10/.../@100). What the user gets for their budget.
- **Recall proxy**: hits divided by all judge-relevant pages in the union of everything ever fetched. How much of the reachable good stuff an arm finds.
- **Token efficiency**: median hits per 10k tokens.

## Results (2026-08-15, coding-agent research task, 5 seeds, 100 pages, 10 replicates)

Task: real-world adoption of AI coding agents in production (evaluation, failure modes, IDE vs CLI), Chinese/English, blogs preferred.

| Arm | Median hits @100 | Range | vs `off` | Median tokens | Hits/10k tokens |
|-----|-----------------|-------|----------|---------------|-----------------|
| `off` | 23.0 | 4-30 | — | 35k | **6.56** |
| `C3000` | **27.5** | 9-30 | +4.5 (6W/3L/1T) | 206k | 1.34 |
| `C4500` | 23.5 | 9-29 | +0.5 (6W/4L) | 231k | 1.02 |
| `C6000` | 16.0 | 8-33 | -7.0 (5W/5L) | 257k | 0.62 |
| `C12000` | 18.5 | 8-34 | -4.5 (5W/4L/1T) | 330k | 0.56 |

Recall proxy (union across all arms and runs: 879 pages, 132 judge-relevant):

| Arm | Relevant pages found (union) | Recall | Median per-run recall | Domains |
|-----|------------------------------|--------|-----------------------|---------|
| `off` | 49 | 37.1% | 17.4% | 17 |
| `C3000` | 67 | 50.8% | **20.8%** | 23 |
| `C4500` | 67 | 50.8% | 17.8% | 20 |
| `C6000` | 62 | 47.0% | 12.1% | 17 |
| `C12000` | **83** | **62.9%** | 14.0% | 22 |

Conclusions:

1. **Feedback's value is task-dependent.** On an easy content-hunt task the feedback loop was decisive (10 vs 0 hits). On this complex research task the best arm only gains +4.5, and 6000/12000 actively hurt.
2. **The 6000-char window is the bad middle on long-form pages**: too much boilerplate, not enough signal. 3000 lands in the intro zone, where the thesis lives. 3000 is now the default.
3. **No single arm covers everything.** The best arm reaches 63% of the reachable relevant set; arms find complementary pages, not the same ones (lowest overlap: `off` vs `C12000` at 37.5% Jaccard). The real "don't miss anything" fix is diversity-aware exploration, not the text cap.
4. **The no-feedback baseline is 5x cheaper per hit.** Feedback buys a few extra hits, not a free lunch.

The earlier single-run experiment on the simpler task (10 vs 0 vs 1 for B/C/A) recorded the opposite cap ordering — single runs are noisy; this 10-replicate study supersedes it.

## Replication protocol

Run-to-run variance is huge (the `off` arm alone swung 4-30). Trust only medians:

1. Each invocation auto-creates a fresh run set — no manual moving needed.
2. `--resume <run_id>` resumes an interrupted set (completed arms are skipped).
3. Judge labels are shared per goal and only grow; deleting them re-judges everything.
4. Judge cost: ~4-5k tokens per page; arms cost ~35-330k tokens each per 100-page run.
