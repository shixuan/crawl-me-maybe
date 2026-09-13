"""Open a browser for manual login and save Playwright storage state."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import select
import sys
import time
from pathlib import Path
from typing import Any, cast

from crawlme.digest.feed import FEEDS

# How often to ask whether the login window is still there.
_CLOSE_POLL_SECONDS = 0.5


class SessionError(Exception):
    """The session could not be captured."""


def _login_url(feed: str) -> str:
    """Return the platform homepage used for manual login."""
    adapter = FEEDS.get(feed)
    if adapter is None or not adapter.NEEDS_SESSION:
        raise SessionError(f"{feed} needs no session, so there is nothing to log in to")
    return f"https://www.{adapter.DOMAIN}/"


def _typed_enter() -> bool:
    """Whether a line is waiting on stdin, without blocking for one.

    Not a thread: cancelling a task does not stop the read inside it,
    and the interpreter then hangs at exit waiting to join it.
    """
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
    except (OSError, ValueError):
        # No selectable stdin (a pipe on some platforms, or none at
        # all). The window is still a way out, so this is not fatal.
        return False
    if not ready:
        return False
    # An empty read is end of input, not a keypress.
    return bool(sys.stdin.readline())


async def _wait_for_a_person(browser: Any, page: Any, timeout_sec: float) -> bool:
    """Wait for Enter or tab/browser closure; return False on timeout.

    This does not verify login. Poll because closing a tab need not close the browser.
    Avoid reading storage state during login because it can open tabs.
    """
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if _typed_enter():
            return True
        try:
            if page.is_closed() or not browser.is_connected():
                return True
        except Exception:
            return True
        await asyncio.sleep(_CLOSE_POLL_SECONDS)
    return False


async def _read_state(context: Any) -> dict[str, Any] | None:
    """The session as it stands, or None if it cannot be read."""
    try:
        return cast("dict[str, Any]", await context.storage_state())
    except Exception:
        return None


async def _browser_state(feed: str, timeout_sec: int) -> dict[str, Any]:
    """Open the platform homepage and collect state when the manual login wait ends."""
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
        page = await context.new_page()
        await page.goto(url)

        print(f"\nA browser window is open at {url}")
        print("  1. log in there, the way you normally would")
        print("  2. close the window, or come back here and press Enter")
        print("Nothing is read from the page: only the session your login produced.\n")
        try:
            came = await _wait_for_a_person(browser, page, timeout_sec)
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Try to preserve any session established before the interruption.
            print("\ninterrupted; keeping the session the login produced", file=sys.stderr)
            came = True
        try:
            if not came:
                raise SessionError(f"nobody finished logging in within {timeout_sec}s, so nothing was saved")
            state = await _read_state(context)
            if state is None:
                raise SessionError(
                    "the browser was closed before its session could be read.\n"
                    "  Close the tab, or press Enter here, rather than quitting the browser:\n"
                    "  a session lives in the browser and goes with it."
                )
        finally:
            with contextlib.suppress(Exception):
                await context.close()
            with contextlib.suppress(Exception):
                await browser.close()
    return state


async def capture(feed: str, out: Path, *, timeout_sec: int = 600) -> dict[str, Any]:
    """Write browser storage state to *out*, rejecting states without cookies."""
    state = await _browser_state(feed, timeout_sec)
    # Cookie presence alone does not verify authentication.
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
    print(f'use it with:  crawl run "<prompt>" --seeds <url> --session {out}')


def add_arguments(sub: Any) -> None:
    """Register the session subcommand on the top-level parser."""
    p = sub.add_parser("session", help="Log in through a browser and save the session for later runs")
    p.add_argument("path", help="Where to write the session file, e.g. ./session.json")
    walled = sorted(n for n, a in FEEDS.items() if a.NEEDS_SESSION)
    p.add_argument("--feed", choices=walled, default=walled[0], help="Which platform")
    p.add_argument("--force", action="store_true", help="Replace an existing session file")
    p.add_argument("--timeout", type=int, default=600, help="Seconds to wait for the login (default: 600)")
