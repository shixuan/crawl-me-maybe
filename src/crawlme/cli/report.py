"""Format command reports and derive exit codes without performing output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crawlme.analysis import CLASSIFICATIONS
from crawlme.llm import Stage
from crawlme.platforms import PageProblem


@dataclass(frozen=True)
class Report:
    text: str
    code: int = 0


# Stop reasons that mean the crawl was prevented rather than finished.
_REFUSALS = frozenset({"LOGIN_REQUIRED", "RATE_LIMITED", "FATAL"})


def exit_code(stopping_reason: str | None) -> int:
    """Return 1 if any combined stop reason is a refusal or fatal error, otherwise 0."""
    return 1 if _REFUSALS.intersection((stopping_reason or "").split("+")) else 0


def make_report(summary: dict[str, Any]) -> Report:
    """Render command statistics through one formatter, without performing output."""
    summary = dict(summary)
    if usage := summary.get("usage"):
        summary.update(
            tokens_used=usage["used"],
            tokens_in=usage["input_tokens"],
            tokens_out=usage["output_tokens"],
            tokens_cached=usage["cached_input_tokens"],
            tokens_thinking=usage["reasoning_tokens"],
            llm_calls=usage["calls"],
            tokens_by_stage={
                name: {
                    "used": u["input_tokens"] + u["output_tokens"],
                    "calls": u["calls"],
                    "in": u["input_tokens"],
                    "out": u["output_tokens"],
                    "cached": u["cached_input_tokens"],
                    "thinking": u["reasoning_tokens"],
                }
                for name, u in usage["by_stage"].items()
            },
        )
    code = 1 if summary.get("error") is not None else exit_code(summary.get("reason"))
    return Report(format_summary(summary), code)


# Between the report's parts. Indentation alone left them reading as
# one block.
_RULE = "-" * 62
# Where a report line's own text starts, past its label.
_INDENT = " " * 14


def _in_order(counts: dict[str, int], vocabulary: tuple[str, ...]) -> list[str]:
    """Return present keys in declared order, followed by undeclared keys."""
    named = [k for k in vocabulary if k in counts]
    return named + sorted(k for k in counts if k not in vocabulary)


def stage_lines(s: dict[str, Any]) -> list[str]:
    """Format token usage by LLM stage."""
    stages: dict[str, dict[str, int]] = s.get("tokens_by_stage") or {}
    if not stages:
        return []
    total = sum(u["used"] for u in stages.values())
    order = _in_order(stages, Stage.ORDER)  # type: ignore[arg-type]
    name_w = max(len(name) for name in order)
    used_w = max(len(str(u["used"])) for u in stages.values())
    lines = []
    for i, name in enumerate(order):
        u = stages[name]
        share = f"{u['used'] / total:.0%}" if total else "0%"
        calls = u["calls"]
        lines.append(
            f"{'  by stage:' if i == 0 else '':<14}"
            f"{name:<{name_w}}  {u['used']:>{used_w}} ({share:>3}), "
            f"{calls} call{'' if calls == 1 else 's'}, "
            f"{u['in']} in / {u['out']} out, {u['cached']} cached, {u['thinking']} thinking"
        )
    return lines


def _refusal_advice(s: dict[str, Any]) -> list[str]:
    """Return recovery instructions for recognized refusal codes."""
    if "LOGIN_REQUIRED" not in str(s.get("reason", "")):
        return []
    path = s.get("session") or "./session.json"
    platform = s.get("platform") or ""
    feed = f" --feed {platform}" if platform else ""
    return [
        _RULE,
        "the platform asked for a login. Make a fresh session with:",
        f"  crawl session {path}{feed} --force",
    ]


def _own_seed_lines(s: dict[str, Any]) -> list[str]:
    """Report progress for user-supplied seeds."""
    seeds = [v for v in (s.get("seeds") or {}).values() if not v.get("proposed")]
    if not seeds:
        return []
    out = [_RULE, "the sources you gave, in the order you gave them:"]
    for seed in seeds:
        found, judged, fetched, wanted, scored, discovered = seed["funnel"]
        out.append(f"  {f'{found} relevant' if found else 'nothing':>12}  {seed['url']}")
        out.append(
            f"{_INDENT}  {discovered} found, {scored} scored, {wanted} wanted, {fetched} fetched, {judged} judged"
        )
        if seed.get("retired"):
            out.append(f"{_INDENT}  stopped reading it: {seed['retired']}")
    return out


def _proposed_seed_lines(s: dict[str, Any]) -> list[str]:
    """Report accepted and rejected seed proposals."""
    proposed = s.get("proposed_seeds") or {}
    asked = s.get("seeds_asked")
    if asked is None:
        return []
    # Report rejected proposals as well as accepted ones.
    turned_away = [f"                {url}  --  {why}" for url, why in sorted(s.get("rejected_seeds") or [])]
    if not proposed:
        # Silence here is what a run that was never asked looks like,
        # and one run lost its proposals to an empty model reply.
        out = [_RULE]
        if asked:
            out.append(f"the model named {asked} more sources; none survived verification:")
            out.extend(turned_away)
        else:
            out.append("the model was asked for more sources and named none usable (see the log).")
        return out
    out = [_RULE, "seeds this run added for itself, best first:"]
    for url, (why, funnel) in sorted(proposed.items(), key=lambda kv: -kv[1][1][0]):
        found, judged, fetched, wanted, scored, discovered = funnel
        out.append(f"  {f'{found} relevant' if found else 'nothing':>12}  {url}")
        # Report each stage separately to distinguish unread work from rejected pages.
        out.append(
            f"                {discovered} found, {scored} scored, {wanted} wanted, {fetched} fetched, {judged} judged"
        )
        if why:
            out.append(f"                {why}")
    if turned_away:
        out.append(f"  and {len(turned_away)} more it named that did not survive:")
        out.extend(turned_away)
    out.append("  Worth one? Add it to --seeds yourself.")
    return out


def format_summary(s: dict[str, Any]) -> str:
    """Render the summary dict as aligned terminal lines."""
    outcome = "failed" if s.get("error") is not None else "finished"
    identity = s.get("task_id") or s.get("state", "?")
    heading = f"{s.get('command', 'crawl')} {outcome}: {identity}"
    if s.get("reason"):
        heading += f" ({s['reason']})"
    lines = [heading, _RULE]
    if s.get("run_dir"):
        state = f" (state={s['state']})" if s.get("state") else ""
        lines.append(f"  run:        {s['run_dir']}{state}")
    if s.get("goal_id"):
        suffix = " (new prompt)" if s.get("new_goal") else ""
        lines.append(f"  goal:       {s['goal_id']}{suffix}")
    if s.get("error") is not None:
        lines.append(f"  error:      {s['error']}")
    if "pages_fetched" in s:
        pages = f"{s['pages_fetched']} fetched"
        if s.get("candidates_discovered"):
            pages += f", {s['candidates_discovered']} links discovered"
        if s.get("candidates_ranked"):
            pages += f", {s['candidates_ranked']} ranked"
        lines.append(f"  pages:      {pages}")
    elif "pages_total" in s:
        lines.append(f"  pages:      {s['pages_total']} in run")

    calls = s.get("llm_calls", 0)
    tokens = f"{s.get('tokens_used', 0)}"
    if "llm_calls" in s:
        tokens += f" ({s.get('tokens_in', 0)} in / {s.get('tokens_out', 0)} out), {calls} calls"
    lines.append(f"  tokens:     {tokens}")
    # Zero cached input may mean the provider did not report it.
    cached = s.get("tokens_cached", 0)
    if calls:
        tin = s.get("tokens_in", 0)
        share = f" ({cached / tin:.0%} of input)" if tin else ""
        lines.append(f"  cached in:  {cached}{share}")
    # Output spent thinking is billed and then thrown away, so a large
    # share here is the cheapest thing in the run to argue with.
    think = s.get("tokens_thinking", 0)
    if calls:
        tout = s.get("tokens_out", 0)
        share = f" ({think / tout:.0%} of output)" if tout else ""
        lines.append(f"  thinking:   {think}{share}")
    lines.extend(stage_lines(s))

    dedup = s.get("dedup") or {}
    if dedup.get("status") == "complete":
        lines.append(
            f"  dedup:      {dedup.get('items', dedup['sources'])} items from "
            f"{dedup['sources']} pages -> {dedup['groups']} results"
        )
    elif dedup:
        lines.append(f"  dedup:      {dedup['status']} (original results)")

    if "fetch_errors" in s:
        lines.append(f"  errors:     {s['fetch_errors']} fetch failures")

    retired = s.get("retired_seeds") or []
    if retired:
        counts: dict[str, int] = {}
        for why in retired:
            counts[why] = counts.get(why, 0) + 1
        lines.append(f"  retired:    {len(retired)} sources stopped paying off")
        lines.extend(
            f"{_INDENT}{why} ({counts[why]} source{'' if counts[why] == 1 else 's'})" for why in sorted(counts)
        )

    analyses = s.get("analyses") or {}
    if "items" in s:
        lines.append(f"  items:      {s['items']}")
    if analyses:
        parts = ", ".join(f"{analyses[c]} {c}" for c in _in_order(analyses, CLASSIFICATIONS))
        lines.append(f"  analyses:   {sum(analyses.values())} ({parts})")
        # Distinguish fetched pages from those actually analyzed.
        target = s.get("max_relevant", 0)
        found = analyses.get("RELEVANT", 0)
        if target and found > target:
            lines.append(
                f"              {found} relevant against a target of {target}; the extra landed from work already sent"
            )
        unjudged = s.get("pages_fetched", 0) - sum(analyses.values())
        if unjudged > 0:
            lines.append(f"              {unjudged} fetched pages were never judged; the run stopped first")

    if "published" in s:
        attempts = [f"{s['analyzed']} first-try"]
        if s.get("retried_ok"):
            attempts.append(f"{s['retried_ok']} retried")
        lines.append(f"  analyses:   {s['published']} written ({', '.join(attempts)})")
    skipped = []
    if s.get("skipped"):
        skipped.append(f"{s['skipped']} identical")
    if s.get("empty"):
        skipped.append(f"{s['empty']} empty text")
    if skipped:
        lines.append(f"  skipped:    {', '.join(skipped)}")
    if s.get("failed"):
        lines.append(f"  failed:     {s['failed']}")

    # Report unavailable or refused pages separately from relevance.
    refused = s.get("not_content") or {}
    if refused:
        kinds = tuple(p.value for p in PageProblem)
        parts = ", ".join(f"{refused[k]} {k}" for k in _in_order(refused, kinds))
        lines.append(f"  no content: {sum(refused.values())} pages ({parts})")

    # Report empty listings separately from irrelevant analyzed pages.
    seen, empty = (s.get("listings") or [0, 0])[:2]
    if empty:
        lines.append(f"  listings:   {seen} read, {empty} held no items")
    # Surface degraded listing extraction even when it yielded candidates.
    stale = s.get("listings_stale", 0)
    if stale:
        lines.append(f"              {stale} read from markup alone; their newest posts are missing")

    if s.get("duration_sec") is not None:
        lines.append(f"  duration:   {s['duration_sec']}s")
    # After the numbers, which are what a reader came for.
    lines.extend(_refusal_advice(s))
    lines.extend(_own_seed_lines(s))
    lines.extend(_proposed_seed_lines(s))
    return "\n".join(lines)
