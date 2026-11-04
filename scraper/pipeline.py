from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .crawler import CrawlOptions, Crawler, Page
from .engines import ENGINES, SearchResult, resolve_engine_names, run_search
from .fetchers import BrowserFetcher, HttpFetcher
from .output import write_outputs

log = logging.getLogger("scraper")


class NoResultsError(RuntimeError):
    pass


@dataclass
class ScrapeConfig:
    query: str
    engines: str = "google,bing,duckduckgo"
    top: int = 5
    depth: int = 1
    links: int = 3
    any_domain: bool = False
    max_pages: int = 100
    max_chars: int = 0
    concurrency: int = 6
    timeout: float = 20
    browser: bool = False
    show_browser: bool = False
    js_fallback: bool = True
    respect_robots: bool = True
    out_dir: str = "output"


@dataclass
class ScrapeRun:
    results_by_engine: dict[str, list[SearchResult]]
    pages: list[Page]
    paths: dict[str, Path]
    elapsed: float
    engines: list[str] = field(default_factory=list)

    @property
    def pages_ok(self) -> int:
        return sum(1 for p in self.pages if p.text)

    @property
    def duplicates(self) -> int:
        return sum(1 for p in self.pages if p.duplicate_of is not None)

    @property
    def total_chars(self) -> int:
        return sum(p.chars for p in self.pages)


async def scrape(cfg: ScrapeConfig) -> ScrapeRun:
    """Search the chosen engines, crawl the results, and write raw_data.md + data.json."""
    query = cfg.query.strip()
    if not query:
        raise ValueError("Query is empty.")
    engine_names = resolve_engine_names(cfg.engines)
    if not engine_names:
        raise ValueError("Pick at least one search engine.")

    http = HttpFetcher(timeout=cfg.timeout)
    browser = BrowserFetcher(headless=not cfg.show_browser, timeout=max(cfg.timeout, 30))
    started = time.monotonic()
    try:
        log.info('Searching %s for "%s" (top %d each)', ", ".join(engine_names), query, cfg.top)
        found = await asyncio.gather(
            *(run_search(ENGINES[n], query, cfg.top, http, browser, force_browser=cfg.browser) for n in engine_names),
            return_exceptions=True,
        )
        results_by_engine: dict[str, list[SearchResult]] = {}
        for name, res in zip(engine_names, found):
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, Exception):
                log.warning("[%s] search failed: %s", name, res)
                res = []
            log.info("[%s] %d result(s)", name, len(res))
            results_by_engine[name] = res
        if not any(results_by_engine.values()):
            raise NoResultsError("No search results from any engine.")

        opts = CrawlOptions(
            depth=max(0, cfg.depth),
            links_per_page=max(0, cfg.links),
            same_domain=not cfg.any_domain,
            max_pages=max(1, cfg.max_pages),
            concurrency=max(1, cfg.concurrency),
            force_browser=cfg.browser,
            js_fallback=cfg.js_fallback,
            respect_robots=cfg.respect_robots,
        )
        pages = await Crawler(query, opts, http, browser).run(results_by_engine)

        settings = {
            "engines": engine_names, "top": cfg.top, "depth": opts.depth, "links_per_page": opts.links_per_page,
            "same_domain": opts.same_domain, "max_pages": opts.max_pages, "browser": cfg.browser,
        }
        paths = write_outputs(Path(cfg.out_dir), query, settings, results_by_engine, pages, cfg.max_chars)
    finally:
        await http.close()
        await browser.close()

    return ScrapeRun(results_by_engine, pages, paths, time.monotonic() - started, engine_names)
