"""Prepare replay inputs, execute the replay pipeline and render its report."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from typing import TYPE_CHECKING

from crawlme.cli.report import Report, make_report
from crawlme.config import Settings
from crawlme.llm import TokenBudget
from crawlme.logging import setup_logging
from crawlme.persist import Storage
from crawlme.persist.read import find_run_dir, task_from_row
from crawlme.scheduler.engine import create_engine
from crawlme.scheduler.pipelines.factory import create_replay
from crawlme.scheduler.pipelines.replay import ReplayInput, ReplayResult

if TYPE_CHECKING:
    from crawlme.analysis import Analyzer


async def cmd_replay(args: argparse.Namespace) -> Report:
    """The ``crawl replay`` command: settings layering, run, report."""
    cfg = Settings()
    # Flags override env/defaults (same layering as run, see config.py).
    if args.analyzer_max_chars is not None:
        cfg.analyzer_max_chars = args.analyzer_max_chars
    if args.log_level is not None:
        cfg.log_level = args.log_level
    # Replay is the first (and only) logger configuration on this path.
    setup_logging(cfg)
    report = await run_replay(
        cfg,
        args.task_id,
        prompt=args.prompt,
        limit=args.limit,
        max_tokens=args.max_tokens,
        force=args.force,
        dedup_enabled=args.dedup == "on",
    )
    return make_report({"command": "replay", **asdict(report)})


async def run_replay(
    settings: Settings,
    task_id: str,
    *,
    prompt: str | None = None,
    limit: int | None = None,
    max_tokens: int | None = None,
    force: bool = False,
    analyzer: Analyzer | None = None,
    dedup_enabled: bool = True,
) -> ReplayResult:
    """Analyze stored pages and wait for bounded retries before closing.

    An injected analyzer avoids provider calls in tests. Otherwise settings build
    the analyzer with the replay token budget."""
    run_dir, task_row = await find_run_dir(settings.result_dir, task_id)
    budget = TokenBudget(limit=max_tokens or 0)
    storage = Storage(str(run_dir / "db" / "crawl.db"), str(run_dir / "raw"))
    pipeline = create_replay(
        settings,
        storage=storage,
        budget=budget,
        analyzer=analyzer,
        dedup_enabled=dedup_enabled,
    )
    return await create_engine(pipeline).run(
        ReplayInput(task_from_row(task_row), run_dir, prompt=prompt, limit=limit, force=force)
    )
