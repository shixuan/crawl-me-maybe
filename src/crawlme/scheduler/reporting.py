"""Read run statistics for the CLI without changing crawl state."""

from __future__ import annotations

import time
from typing import Any

from crawlme.state.tracking import RunTracking


def summary(tracking: RunTracking) -> dict[str, Any]:
    """End-of-run statistics for the CLI's terminal report.

    Everything reads from the run context; stages record into it
    as they work, so no merge step is needed here.
    """
    ledger = tracking.context.ledger
    report: dict[str, Any] = {
        "pages_fetched": tracking.context.progress.pages_fetched,
        "tokens_used": tracking.context.progress.tokens_used,
        "candidates_discovered": ledger.links_discovered,
        "candidates_ranked": ledger.candidates_ranked,
        "fetch_errors": ledger.fetch_errors,
        "analyses": dict(ledger.analyses_by_class),
        # Preserve user seed order in the per-source report.
        "seeds": {
            key: {
                "url": st.url,
                "funnel": st.funnel.as_tuple(),
                "retired": st.retired,
                "proposed": key in tracking.proposed_seeds,
            }
            for key, st in tracking.seeds.items()
            if st.url
        },
        # url -> (why it was proposed, relevant pages found through it)
        "proposed_seeds": {
            url: (why, tracking.seeds[key].funnel.as_tuple()) for key, (url, why) in tracking.proposed_seeds.items()
        },
        # Report why each source retired.
        "retired_seeds": sorted(st.retired for st in tracking.seeds.values() if st.retired),
        # The target, so the report can print the tally beside it.
        "max_relevant": tracking.context.limits.max_relevant,
        # None when the run never asked. A number means it did.
        "seeds_asked": tracking.seeds_asked,
        "rejected_seeds": tracking.rejected_seeds,
    }
    if tracking.context.progress.started_at:
        report["duration_sec"] = round(time.monotonic() - tracking.context.progress.started_at, 1)
    if ledger.not_content:
        report["not_content"] = dict(ledger.not_content)
    if tracking.context.progress.listings_seen:
        report["listings"] = [tracking.context.progress.listings_seen, tracking.context.progress.listings_empty]
    if tracking.context.ledger.listings_stale:
        report["listings_stale"] = tracking.context.ledger.listings_stale
    return report
