from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .engines import ENGINES
from .pipeline import NoResultsError, ScrapeConfig, scrape

log = logging.getLogger("scraper")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scraper",
        description="Search one or more engines, read the top results (and relevant links inside them), "
                    "and save all raw text to a Markdown + JSON file for summarising.",
    )
    p.add_argument("query", nargs="?", help="what to search for (omit to be prompted interactively)")
    p.add_argument("-e", "--engines", default="google,bing,duckduckgo",
                   help=f"comma-separated list or 'all'. Available: {', '.join(ENGINES)} (default: %(default)s)")
    p.add_argument("-n", "--top", type=int, default=5, help="top results to read per engine (default: %(default)s)")
    p.add_argument("-d", "--depth", type=int, default=1,
                   help="how many levels of links to follow from each result; 0 = results only (default: %(default)s)")
    p.add_argument("-l", "--links", type=int, default=3,
                   help="max links to follow from each page (default: %(default)s)")
    p.add_argument("--any-domain", action="store_true", help="also follow links to other websites")
    p.add_argument("--max-pages", type=int, default=100, help="hard cap on total pages fetched (default: %(default)s)")
    p.add_argument("--max-chars", type=int, default=0,
                   help="truncate each page's text in the Markdown file to N characters; 0 = no limit")
    p.add_argument("--concurrency", type=int, default=6, help="parallel page downloads (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=20, help="per-request timeout in seconds (default: %(default)s)")
    p.add_argument("--browser", action="store_true", help="render every page in headless Chromium (slower, handles JS sites)")
    p.add_argument("--show-browser", action="store_true", help="run Chromium visibly (lets you solve CAPTCHAs)")
    p.add_argument("--no-js-fallback", action="store_true",
                   help="don't retry blocked/near-empty pages in the browser")
    p.add_argument("--ignore-robots", action="store_true", help="don't check robots.txt before fetching")
    p.add_argument("-o", "--out-dir", default="output", help="where to save results (default: %(default)s)")
    p.add_argument("-q", "--quiet", action="store_true", help="only print warnings and the final summary")
    return p


def _ask(prompt: str, default: str) -> str:
    val = input(f"{prompt} [{default}]: ").strip()
    return val or default


def interactive(args: argparse.Namespace) -> None:
    print("Interactive mode (press Enter to accept the default)\n")
    while not args.query:
        args.query = input("Search query: ").strip()
    names = list(ENGINES)
    print("Engines: " + "  ".join(f"{i}={n}" for i, n in enumerate(names, 1)) + "  (or 'all')")
    choice = _ask("Pick engines (numbers or names, comma-separated)", args.engines)
    picked = []
    for tok in choice.split(","):
        tok = tok.strip()
        picked.append(names[int(tok) - 1] if tok.isdigit() and 0 < int(tok) <= len(names) else tok)
    args.engines = ",".join(picked)
    args.top = int(_ask("Top results per engine", str(args.top)))
    args.depth = int(_ask("Link depth (0 = only the results)", str(args.depth)))
    if args.depth > 0:
        args.links = int(_ask("Links to follow per page", str(args.links)))
    print()


async def run(args: argparse.Namespace) -> int:
    cfg = ScrapeConfig(
        query=args.query, engines=args.engines, top=args.top, depth=args.depth, links=args.links,
        any_domain=args.any_domain, max_pages=args.max_pages, max_chars=args.max_chars,
        concurrency=args.concurrency, timeout=args.timeout, browser=args.browser,
        show_browser=args.show_browser, js_fallback=not args.no_js_fallback,
        respect_robots=not args.ignore_robots, out_dir=args.out_dir,
    )
    try:
        r = await scrape(cfg)
    except NoResultsError as e:
        log.error("%s", e)
        return 1
    print(f"\nDone in {r.elapsed:.0f}s: read {r.pages_ok}/{len(r.pages)} pages "
          f"({r.duplicates} duplicates skipped), {r.total_chars:,} characters of text.")
    print(f"  Markdown (for summarising): {r.paths['markdown']}")
    print(f"  JSON (structured):          {r.paths['json']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO, format="%(message)s")
    for noisy in ("httpx", "httpcore", "trafilatura", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not args.query:
        try:
            interactive(args)
        except (EOFError, KeyboardInterrupt):
            return 130
    try:
        return asyncio.run(run(args))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
