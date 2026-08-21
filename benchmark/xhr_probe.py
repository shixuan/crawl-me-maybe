"""Does a feed listing page download the text we cannot see in its DOM?

The grid renders thumbnails: one link and one generated image description
per post, and no caption. A human browsing it cannot read captions
either. But the page has to get the posts from somewhere, and that
somewhere is a request the page makes itself.

This probe answers one question before any design rests on it: does that
response carry the post text? It records every response the page makes,
then reports which ones mention the posts by code and which of those
carry text long enough to be a caption rather than a label.

Nothing here forges a request. It saves what the page already downloaded
and would otherwise be thrown away when only the rendered DOM is kept, so
it costs exactly the same one page load a normal fetch costs.

    python benchmark/xhr_probe.py \
        --url https://www.instagram.com/mollytea_canada/ \
        --session ./ig-session.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_POST_CODE = re.compile(r'"(?:code|shortcode)"\s*:\s*"([A-Za-z0-9_-]{8,})"')
_CAPTION_KEYS = ("caption", "edge_media_to_caption", "accessibility_caption", "text")
_OUT = Path("results/xhr")


async def probe(url: str, session: str, wait_ms: int) -> int:
    from playwright.async_api import async_playwright

    captured: list[dict[str, Any]] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=session)
        page = await context.new_page()

        async def _on_response(resp: Any) -> None:
            ctype = (resp.headers or {}).get("content-type", "")
            if "json" not in ctype and "javascript" not in ctype:
                return
            try:
                body = await resp.text()
            except Exception:
                return  # a body can be gone by the time we ask for it
            captured.append({"url": resp.url, "status": resp.status, "body": body})

        # Before navigating: a listener attached afterwards misses the
        # very requests that fill the first screen.
        page.on("response", _on_response)
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        # The grid fills lazily; a small scroll asks for the next page of
        # posts, which is the same request shape as the first.
        await page.mouse.wheel(0, 4000)
        await page.wait_for_timeout(wait_ms)
        await context.close()
        await browser.close()

    return _report(url, captured)


def _report(url: str, captured: list[dict[str, Any]]) -> int:
    print(f"\nurl:       {url}")
    print(f"responses: {len(captured)} json/js bodies recorded\n")

    interesting = []
    for item in captured:
        codes = set(_POST_CODE.findall(item["body"]))
        if not codes:
            continue
        longest = _longest_text(item["body"])
        interesting.append((item, codes, longest))

    if not interesting:
        print("VERDICT: FAIL — no response mentioned any post code.")
        print("The page may deliver posts inside the document itself, or the")
        print("selectors used here do not match this platform's shape.")
        return 1

    interesting.sort(key=lambda t: -len(t[2]))
    _OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    print(f"{'posts':>6}  {'longest text':>12}  url")
    for i, (item, codes, longest) in enumerate(interesting[:10]):
        short = item["url"].split("?")[0][-64:]
        print(f"{len(codes):>6}  {len(longest):>12}  {short}")
        if i == 0:
            path = _OUT / f"xhr_{stamp}.json"
            path.write_text(item["body"], encoding="utf-8")
            print(f"        saved -> {path}")

    best_codes, best_text = interesting[0][1], interesting[0][2]
    print()
    print(f"post codes in the best response: {len(best_codes)}")
    print(f"longest text found:              {len(best_text)} chars")
    if best_text:
        print(f"  {best_text[:220]!r}")

    # A caption is prose; a label is a word or two. The line between them
    # is what decides whether the funnel gets something to read.
    if len(best_text) >= 80:
        print("\nVERDICT: PASS — the page downloads post text it never renders.")
        return 0
    print("\nVERDICT: FAIL — responses carry the posts but no text worth ranking on.")
    return 1


def _longest_text(body: str) -> str:
    """The longest string under any caption-ish key in the body."""
    best = ""
    for key in _CAPTION_KEYS:
        for m in re.finditer(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', body):
            value = m.group(1)
            if len(value) > len(best):
                best = value
    # Unescape the way JSON means it. Decoding as unicode_escape would
    # read the utf-8 bytes as latin-1 and turn every emoji and CJK
    # character into mojibake, which also miscounts the length the
    # verdict rests on.
    try:
        return str(json.loads(f'"{best}"'))
    except json.JSONDecodeError:
        return best


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True)
    ap.add_argument("--session", required=True, help="Playwright storage_state JSON")
    ap.add_argument("--wait-ms", type=int, default=3000)
    args = ap.parse_args()
    if not Path(args.session).is_file():
        print(f"session file not found: {args.session}", file=sys.stderr)
        return 2
    return asyncio.run(probe(args.url, args.session, args.wait_ms))


if __name__ == "__main__":
    raise SystemExit(main())
