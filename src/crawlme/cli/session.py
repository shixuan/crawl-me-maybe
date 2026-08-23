"""The session command: log in once, by hand, and keep the result.

A logged-in crawl needs a Playwright ``storage_state`` file.  Producing
one otherwise means writing a Playwright script or installing a cookie
exporting extension, and those extensions can read every cookie the
browser holds, for every site.  This opens a real browser at the
platform, waits while a person logs in, and saves what the session
became.

Credentials never come near this process: they are typed into the
platform's own page, in a browser window, and what lands on disk is the
session that login produced.

Deliberately its own command rather than something ``crawl run`` does on
demand.  A crawl that stops halfway to open a window and wait for a
human is a crawl that cannot run unattended, which is most of them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, cast

from crawlme.digest.feed import FEEDS


class SessionError(Exception):
    """The session could not be captured."""


def _login_url(feed: str) -> str:
    """Where to send the browser so the platform offers its login.

    Derived from the adapter's domain rather than declared per platform:
    a logged-out visitor to a login-walled front page gets the login
    wall, which is the whole point.  A platform that needs a specific
    address can declare one when it turns up; the second instance is
    what earns the field.
    """
    adapter = FEEDS.get(feed)
    if adapter is None or not adapter.NEEDS_SESSION:
        raise SessionError(f"{feed} needs no session, so there is nothing to log in to")
    return f"https://www.{adapter.DOMAIN}/"


async def _browser_state(feed: str, timeout_sec: int) -> dict[str, Any]:
    """Open a window, wait for a person, and hand back what login made.

    Everything untestable lives here: a real browser and a real human.
    What the caller does with the result -- judging whether that login
    took, and writing it down -- is kept outside so it can be checked.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:  # pragma: no cover - depends on the install
        raise SessionError("playwright is not installed: pip install playwright && playwright install chromium") from e

    url = _login_url(feed)
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=False)
        except Exception as e:  # pragma: no cover - depends on the desktop
            raise SessionError(
                f"could not open a browser window ({e}).\n"
                "A visible browser needs a desktop: on WSL that means WSLg, over SSH an X display."
            ) from e
        context = await browser.new_context()
        await (await context.new_page()).goto(url)

        print(f"\nA browser window is open at {url}")
        print("  1. log in there, the way you normally would")
        print("  2. come back here and press Enter")
        print("Nothing is read from the page: only the session your login produced.\n")
        try:
            await asyncio.wait_for(asyncio.to_thread(sys.stdin.readline), timeout=timeout_sec)
        except asyncio.TimeoutError as e:
            raise SessionError(f"nobody pressed Enter within {timeout_sec}s, so nothing was saved") from e
        finally:
            # Read before closing either: a closed context has no state.
            state = cast("dict[str, Any]", await context.storage_state())
            await context.close()
            await browser.close()
    return state


async def capture(feed: str, out: Path, *, timeout_sec: int = 600) -> dict[str, Any]:
    """Capture a logged-in session and write it to *out*."""
    state = await _browser_state(feed, timeout_sec)
    # The same thing the fetcher checks before a crawl: a state with no
    # cookies loads fine and crawls logged out, which on a walled
    # platform reads as an empty site rather than as a failed login.
    if not state.get("cookies"):
        raise SessionError("that browser was never logged in: the session it produced holds no cookies")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


async def cmd_session(args: argparse.Namespace) -> None:
    """The ``crawl session`` command: capture a logged-in session."""
    out = Path(args.path)
    if out.exists() and not args.force:
        print(f"Error: {out} already exists (use --force to replace it)", file=sys.stderr)
        sys.exit(1)
    try:
        state = await capture(args.feed, out, timeout_sec=args.timeout)
    except SessionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    hosts = sorted({c.get("domain", "") for c in state.get("cookies", [])})
    print(f"saved {out}  ({len(state['cookies'])} cookies from {', '.join(h for h in hosts if h)})")
    print(f'use it with:  crawl run "<prompt>" --feed {args.feed} --session {out}')


def add_arguments(sub: Any) -> None:
    """Register the session subcommand on the top-level parser."""
    p = sub.add_parser("session", help="Log in through a browser and save the session for later runs")
    p.add_argument("path", help="Where to write the session file, e.g. ./ig-session.json")
    walled = sorted(n for n, a in FEEDS.items() if a.NEEDS_SESSION)
    p.add_argument("--feed", choices=walled, default=(walled or ["instagram"])[0], help="Which platform")
    p.add_argument("--force", action="store_true", help="Replace an existing session file")
    p.add_argument("--timeout", type=int, default=600, help="Seconds to wait for the login (default: 600)")
