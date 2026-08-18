"""Phase 0 probe: can we see a merchant's posts at all?

Not part of the pipeline and not a test. It fetches one profile through
the browser fetcher and reports what actually came back, because the
three questions that decide Phase 0 are all answered by looking at one
rendered page:

  1. is the caption in the HTML, or only the image?
  2. how many posts does one page yield, and is there a cursor?
  3. did the platform serve content, a login wall, or a challenge?

Usage:
    python benchmark/phase0_probe.py https://www.instagram.com/<account>/ \
        --cookies ./ig-session.json

Writes the rendered HTML next to the report so the extraction fields in
Phase 2 can be designed against real content rather than guesses.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from crawlme.digest.fetcher import PlaywrightFetcher
from crawlme.schemas import URL, FrontierItem

#: Signals that we got something other than the content we asked for.
_WALL_MARKERS = {
    "login": ("loginForm", "Log in to Instagram", "/accounts/login"),
    "challenge": ("challenge_required", "Suspicious Login", "checkpoint_required"),
    "rate_limit": ("Please wait a few minutes", "rate limited", "Try Again Later"),
    "age_gate": ("age_verification",),
    # A wrong handle renders a full, healthy-looking 950KB shell, so
    # without this the probe happily reports success on a 404.
    "not_found": ("Sorry, this page", "isn't available", "Page Not Found"),
}


def _classify(html: str) -> list[str]:
    hits = []
    for label, markers in _WALL_MARKERS.items():
        if any(m.lower() in html.lower() for m in markers):
            hits.append(label)
    return hits


def _captions(html: str) -> list[str]:
    """Pull anything that looks like post text out of the rendered page.

    Deliberately several heuristics rather than one selector: the point
    is to find out *whether* the text is reachable, not to build the
    parser. Phase 1 writes the real adapter against whatever wins here.
    """
    found: list[str] = []
    # 1. JSON blobs the app ships inline
    for m in re.finditer(r'"caption"\s*:\s*"((?:[^"\\]|\\.){10,})"', html):
        found.append(m.group(1)[:200])
    for m in re.finditer(r'"(?:edge_media_to_caption|text)"\s*:\s*"((?:[^"\\]|\\.){20,})"', html):
        found.append(m.group(1)[:200])
    # 2. alt text, which IG fills with a description of the post.
    #    Skip avatars: the viewer's own "X's profile picture" is always
    #    present and is not content.
    for m in re.finditer(r'<img[^>]+alt="([^"]{25,})"', html):
        alt = m.group(1)
        if "profile picture" in alt.lower():
            continue
        found.append(alt[:200])
    # 3. og:description, at least present on profile pages
    for m in re.finditer(r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', html):
        found.append(m.group(1)[:200])
    seen, out = set(), []
    for f in found:
        k = f[:60]
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


#: Post permalinks come in two shapes: the bare /p/<code>/ and the
#: profile-scoped /<user>/p/<code>/ the grid actually renders. Matching
#: only the bare form reports zero posts on a page full of them.
_POST_HREF = re.compile(r'href="((?:/[A-Za-z0-9_.]+)?/(?:p|reel)/[A-Za-z0-9_-]+/?)"')


def _post_links(html: str) -> list[str]:
    return sorted(set(_POST_HREF.findall(html)))


def _is_post_page(url: str) -> bool:
    return bool(re.search(r"/(?:p|reel)/[A-Za-z0-9_-]+", url))


def _post_body(html: str) -> dict[str, str]:
    """What a post detail page carries beyond the grid's alt text.

    The caption is the thing the whole product depends on: it is where
    "what is on offer, until when, and how to claim it" lives. The grid
    only exposes an auto-generated description, so this is the check
    that decides whether Phase 1 needs one request per post.
    """
    out: dict[str, str] = {}
    for key, pat in (
        ("og:description", r'property="og:description"[^>]+content="([^"]+)"'),
        ("og:title", r'property="og:title"[^>]+content="([^"]+)"'),
        ("json caption", r'"caption"\s*:\s*"((?:[^"\\]|\\.){10,})"'),
        ("json text", r'"text"\s*:\s*"((?:[^"\\]|\\.){30,})"'),
        ("time datetime", r'<time[^>]+datetime="([^"]+)"'),
    ):
        m = re.search(pat, html)
        if m:
            out[key] = m.group(1)[:400]
    return out


async def probe(url: str, cookies: str, out_dir: Path, wait_until: str) -> int:
    fetcher = PlaywrightFetcher(storage_state=cookies, wait_until=wait_until)  # type: ignore[arg-type]
    try:
        item = FrontierItem(url=URL(raw=url, canonical=url, url_key="probe"), url_key="probe")
        result = await fetcher.fetch(item)
    finally:
        await fetcher.aclose()

    html = result.raw.decode("utf-8", "replace")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    page_file = out_dir / f"probe_{stamp}.html"
    page_file.write_text(html, encoding="utf-8")

    walls = _classify(html)
    captions = _captions(html)
    posts = _post_links(html)
    body = _post_body(html) if _is_post_page(url) else {}

    print(f"url            {url}")
    print(f"status         {result.status_code}   {result.fetch_duration_ms} ms")
    print(f"html           {len(html) // 1024} KB  -> {page_file}")
    print(f"wall signals   {walls or 'none'}")
    print(f"post links     {len(posts)}   {posts[:3]}")
    print(f"caption-ish    {len(captions)}")
    for c in captions[:5]:
        print(f"  - {c[:110]}")
    if _is_post_page(url):
        print("post body")
        for k, v in body.items():
            print(f"  {k:16} {v[:200]}")
        if not body:
            print("  (nothing found)")

    # Posts are the thing being verified.  Captions alone can come from
    # page chrome, so they do not carry a PASS on their own.
    if _is_post_page(url):
        verdict = "PASS" if not walls and body else "FAIL"
    else:
        verdict = "PASS" if not walls and posts else "FAIL"
    print(f"\nverdict        {verdict}")
    if "not_found" in walls:
        print("  that handle does not exist; check the account name")
    elif walls:
        print("  the platform served a wall, not content; see the saved HTML")
    elif not captions and not posts:
        print("  page rendered but held no posts; the selector or wait_until may be wrong")
    return 0 if verdict == "PASS" else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 0 probe: one profile, one page")
    ap.add_argument("url")
    ap.add_argument("--cookies", required=True)
    ap.add_argument("--out", default="results/phase0")
    ap.add_argument("--wait-until", default="networkidle", choices=["load", "domcontentloaded", "networkidle"])
    args = ap.parse_args()
    if not Path(args.cookies).is_file():
        print(f"no session file at {args.cookies}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(probe(args.url, args.cookies, Path(args.out), args.wait_until)))


if __name__ == "__main__":
    main()
