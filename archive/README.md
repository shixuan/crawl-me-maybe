# The runs behind the numbers

`surviving-runs-20260822.tar.gz` holds the crawl databases and logs for
four runs, without their raw HTML:

| run | task |
|---|---|
| `20260821_234041` | bubble tea, Instagram (first pass) |
| `20260822_084349` | bubble tea, Instagram |
| `20260822_094648` | bubble tea, Instagram |
| `20260822_080323` | software releases, RSS |

`score.py` reads a run directory, so unpack and point it at one.

**Four more runs are gone.** The calibration set — store info, coffee,
databases, ML papers — was archived to an untracked file and swept by a
`git clean` before it was committed. Their numbers survive in the root
README; the databases do not, so anything in the seven-task tables that
depends on those four cannot be recomputed from source.
