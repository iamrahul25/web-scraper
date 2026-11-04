from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from .engines import SearchResult
from .extract import Extracted, extract_html, normalize_url, pick_links, query_terms
from .fetchers import USER_AGENT, BrowserFetcher, FetchResult, HttpFetcher

log = logging.getLogger("scraper")


@dataclass
class CrawlOptions:
    depth: int = 1
    links_per_page: int = 3
    same_domain: bool = True
    max_pages: int = 100
    concurrency: int = 6
    per_host: int = 2
    min_chars: int = 300
    force_browser: bool = False
    js_fallback: bool = True
    respect_robots: bool = True


@dataclass
class Page:
    id: int
    url: str
    depth: int
    found_via: list[dict] = field(default_factory=list)
    parent_id: int | None = None
    parent_url: str | None = None
    link_anchor: str = ""
    final_url: str = ""
    status: int = 0
    fetched_with: str = ""
    content_type: str = ""
    title: str = ""
    author: str = ""
    published: str = ""
    description: str = ""
    text: str = ""
    chars: int = 0
    followed_links: list[str] = field(default_factory=list)
    duplicate_of: int | None = None
    error: str | None = None


class Robots:
    def __init__(self, http: HttpFetcher):
        self.http = http
        self._cache: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def allowed(self, url: str) -> bool:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        async with self._locks[origin]:
            if origin not in self._cache:
                parser = None
                try:
                    status, _, body = await asyncio.wait_for(self.http.get_text(origin + "/robots.txt"), 10)
                    if status == 200:
                        parser = RobotFileParser()
                        parser.parse(body.splitlines())
                except Exception:
                    pass
                self._cache[origin] = parser
        parser = self._cache[origin]
        return parser is None or parser.can_fetch(USER_AGENT, url)


class Crawler:
    def __init__(self, query: str, opts: CrawlOptions, http: HttpFetcher, browser: BrowserFetcher):
        self.query = query
        self.terms = query_terms(query)
        self.opts = opts
        self.http = http
        self.browser = browser
        self.robots = Robots(http)
        self.pages: list[Page] = []
        self.visited: set[str] = set()
        self._seen_content: dict[str, int] = {}  # final URL or text hash -> page id
        self._sem = asyncio.Semaphore(opts.concurrency)
        self._host_sems: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(opts.per_host))

    def _claim(self, url: str) -> bool:
        key = normalize_url(url).rstrip("/")
        if key in self.visited or len(self.pages) >= self.opts.max_pages:
            return False
        self.visited.add(key)
        return True

    @staticmethod
    async def _extract(res: FetchResult) -> Extracted | None:
        if res.text is not None:
            return Extracted(text=res.text)
        if res.html is not None:
            return await asyncio.to_thread(extract_html, res.html, res.final_url or res.url)
        return None

    async def _fetch(self, url: str) -> tuple[FetchResult, Extracted | None]:
        if self.opts.force_browser:
            res = await self.browser.fetch(url)
            return res, await self._extract(res)
        res = await self.http.fetch(url)
        ex = await self._extract(res)
        blocked = res.status in (401, 403, 429, 503) or (res.error or "").startswith(("ConnectError", "ReadTimeout"))
        too_short = res.html is not None and (ex is None or len(ex.text) < self.opts.min_chars)
        if self.opts.js_fallback and (blocked or too_short):
            b = await self.browser.fetch(url)
            bex = await self._extract(b)
            if bex and len(bex.text) > (len(ex.text) if ex else 0):
                return b, bex
        return res, ex

    async def _visit(self, page: Page) -> None:
        host = urlparse(page.url).netloc
        async with self._sem, self._host_sems[host]:
            if self.opts.respect_robots and not await self.robots.allowed(page.url):
                page.error = "disallowed by robots.txt"
                log.info("  skip (robots.txt) %s", page.url)
                return
            res, ex = await self._fetch(page.url)
        page.status, page.final_url = res.status, res.final_url or page.url
        page.fetched_with, page.content_type = res.via, res.content_type
        if ex:
            page.title, page.author, page.published, page.description = ex.title, ex.author, ex.published, ex.description
            page.text = ex.text
            page._links = ex.links  # type: ignore[attr-defined]
        if res.error and not page.text:
            page.error = res.error
        if page.text:
            keys = ("url:" + normalize_url(page.final_url).rstrip("/"),
                    "sha1:" + hashlib.sha1(page.text.encode("utf-8", "replace")).hexdigest())
            original = next((self._seen_content[k] for k in keys if k in self._seen_content), None)
            if original is not None:
                page.duplicate_of, page.text = original, ""
                page.__dict__.pop("_links", None)
                log.info("  [d%d #%d] %s  ->  duplicate of #%d", page.depth, page.id, page.url[:100], original)
                return
            for k in keys:
                self._seen_content[k] = page.id
        page.chars = len(page.text)
        state = f"{page.chars:,} chars" if page.text else f"FAILED ({page.error})"
        log.info("  [d%d #%d] %s  ->  %s", page.depth, page.id, page.url[:100], state)

    def _new_page(self, url: str, depth: int, **kw) -> Page:
        page = Page(id=len(self.pages) + 1, url=url, depth=depth, **kw)
        self.pages.append(page)
        return page

    async def run(self, results_by_engine: dict[str, list[SearchResult]]) -> list[Page]:
        # Interleave by rank so the best result of every engine is fetched first.
        level: list[Page] = []
        by_url: dict[str, Page] = {}
        max_rank = max((len(r) for r in results_by_engine.values()), default=0)
        for rank in range(max_rank):
            for engine, results in results_by_engine.items():
                if rank >= len(results):
                    continue
                r = results[rank]
                via = {"engine": engine, "rank": r.rank, "title": r.title, "snippet": r.snippet}
                key = normalize_url(r.url).rstrip("/")
                if key in by_url:
                    by_url[key].found_via.append(via)
                elif self._claim(r.url):
                    by_url[key] = self._new_page(r.url, 0, found_via=[via])
                    level.append(by_url[key])

        for depth in range(self.opts.depth + 1):
            if not level:
                break
            log.info("Depth %d: fetching %d page(s)", depth, len(level))
            await asyncio.gather(*(self._visit(p) for p in level))
            if depth == self.opts.depth:
                break
            next_level: list[Page] = []
            for parent in level:
                links = getattr(parent, "_links", [])
                chosen = pick_links(
                    links, parent.final_url or parent.url, self.terms,
                    self.opts.links_per_page, self.opts.same_domain, self.visited,
                )
                for link in chosen:
                    if self._claim(link.url):
                        parent.followed_links.append(link.url)
                        next_level.append(self._new_page(
                            link.url, depth + 1, parent_id=parent.id,
                            parent_url=parent.url, link_anchor=link.anchor,
                        ))
            level = next_level

        for p in self.pages:
            p.__dict__.pop("_links", None)
        return self.pages
