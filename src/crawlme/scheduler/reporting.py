"""Read run statistics for the CLI without changing crawl state."""

from __future__ import annotations

import time
from typing import Any

from crawlme.runtime.state import RunState, SeedState


def summary(state: RunState) -> dict[str, Any]:
    """Build CLI statistics from RunState without changing it."""
    stats = state.stats
    report: dict[str, Any] = {
        "pages_fetched": state.progress.pages_fetched,
        "tokens_used": state.progress.tokens_used,
        "candidates_discovered": stats.links_discovered,
        "candidates_ranked": stats.candidates_ranked,
        "fetch_errors": stats.fetch_errors,
        "analyses": dict(stats.analyses_by_class),
        # Preserve user seed order in the per-source report.
        "seeds": {
            key: {
                "url": st.url,
                "funnel": st.funnel.as_tuple(),
                "retired": st.retired,
                "proposed": key in state.proposed_seeds,
            }
            for key, st in state.seeds.items()
            if st.url
        },
        # url -> (why it was proposed, relevant pages found through it)
        "proposed_seeds": {
            url: (why, state.seeds.get(key, SeedState()).funnel.as_tuple())
            for key, (url, why) in state.proposed_seeds.items()
        },
        # Report why each source retired.
        "retired_seeds": sorted(st.retired for st in state.seeds.values() if st.retired),
        # The target, so the report can print the tally beside it.
        "max_relevant": state.limits.max_relevant,
        # None when the run never asked. A number means it did.
        "seeds_asked": state.seeds_asked,
        "rejected_seeds": list(state.rejected_seeds),
    }
    if state.progress.started_at:
        report["duration_sec"] = round(time.monotonic() - state.progress.started_at, 1)
    if stats.not_content:
        report["not_content"] = dict(stats.not_content)
    if state.progress.listings_seen:
        report["listings"] = [state.progress.listings_seen, state.progress.listings_empty]
    if state.stats.listings_stale:
        report["listings_stale"] = state.stats.listings_stale
    return report
