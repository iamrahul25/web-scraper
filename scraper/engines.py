from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .extract import query_terms, relevance
from .fetchers import BrowserFetcher, HttpFetcher

log = logging.getLogger("scraper")


@dataclass
class SearchResult:
    engine: str
    rank: int
    title: str
    url: str
    snippet: str


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


class Engine:
    name = ""
    base = ""
    requires_browser = False

    def page_url(self, query: str, page: int, offset: int) -> str:
        raise NotImplementedError

    def parse(self, html: str) -> list[tuple[str, str, str]]:
        """Return (title, url, snippet) tuples for organic results on one page."""
        raise NotImplementedError

    async def resolve(self, url: str, browser: BrowserFetcher) -> str:
        return url


class DuckDuckGo(Engine):
    name = "duckduckgo"
    base = "https://html.duckduckgo.com"

    def page_url(self, query, page, offset):
        url = f"{self.base}/html/?q={quote_plus(query)}"
        return url + (f"&s={offset}&dc={offset + 1}" if offset else "")

    def parse(self, html):
        soup = BeautifulSoup(html, "lxml")
        out = []
        for block in soup.select("div.result:not(.result--ad)"):
            a = block.select_one("a.result__a")
            if not a or not a.get("href"):
                continue
            href = a["href"]
            if "duckduckgo.com/y.js" in href:  # ad
                continue
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
            out.append((_text(a), urljoin("https:", href), _text(block.select_one(".result__snippet"))))
        return out


class Bing(Engine):
    name = "bing"
    base = "https://www.bing.com"

    def page_url(self, query, page, offset):
        return f"{self.base}/search?q={quote_plus(query)}&setlang=en&cc=US&first={offset + 1}"

    @staticmethod
    def _decode(href: str) -> str:
        if "bing.com/ck/a" not in href:
            return href
        u = parse_qs(urlparse(href).query).get("u", [""])[0]
        if u.startswith("a1"):
            s = u[2:] + "=" * (-len(u[2:]) % 4)
            try:
                return base64.urlsafe_b64decode(s).decode("utf-8")
            except Exception:
                pass
        return href

    def parse(self, html):
        soup = BeautifulSoup(html, "lxml")
        out = []
        for block in soup.select("li.b_algo"):
            a = block.select_one("h2 a")
            if not a or not a.get("href"):
                continue
            snippet = block.select_one(".b_caption p, p.b_lineclamp2, p.b_lineclamp3, p")
            out.append((_text(a), self._decode(a["href"]), _text(snippet)))
        return out


class Yahoo(Engine):
    name = "yahoo"
    base = "https://search.yahoo.com"
    _ru = re.compile(r"/RU=([^/]+)/R[KS]=")

    def page_url(self, query, page, offset):
        return f"{self.base}/search?p={quote_plus(query)}&b={offset + 1}"

    def parse(self, html):
        soup = BeautifulSoup(html, "lxml")
        out = []
        for block in soup.select("div.algo"):
            a = block.select_one(".compTitle a")
            if not a or not a.get("href"):
                continue
            href = a["href"]
            m = self._ru.search(href)
            if m:
                href = unquote(m.group(1))
            title = _text(block.select_one(".compTitle h3")) or _text(a)
            out.append((title, href, _text(block.select_one(".compText"))))
        return out


class Brave(Engine):
    name = "brave"
    base = "https://search.brave.com"
    requires_browser = True  # plain HTTP gets 429

    def page_url(self, query, page, offset):
        return f"{self.base}/search?q={quote_plus(query)}&source=web" + (f"&offset={page}" if page else "")

    def parse(self, html):
        soup = BeautifulSoup(html, "lxml")
        out = []
        for block in soup.select('div.snippet[data-type="web"]'):
            a = block.select_one("a[href^='http']")
            if not a:
                continue
            title_el = block.select_one(".title")
            title = (title_el.get("title") or _text(title_el)) if title_el else _text(a)
            snippet = _text(block.select_one(".generic-snippet .content, .snippet-description, .content"))
            out.append((title, a["href"], snippet))
        return out


class Google(Engine):
    name = "google"
    base = "https://www.google.com"
    requires_browser = True  # Google serves a JS-only page to plain HTTP clients

    def page_url(self, query, page, offset):
        return f"{self.base}/search?q={quote_plus(query)}&hl=en&gl=us" + (f"&start={offset}" if offset else "")

    def parse(self, html):
        soup = BeautifulSoup(html, "lxml")
        out = []
        for h3 in soup.select("#rso a h3, #search a h3"):
            a = h3.find_parent("a")
            if not a or not a.get("href"):
                continue
            block = a.find_parent("div", class_="tF2Cxc") or a.find_parent(attrs={"data-hveid": True})
            snippet = block.select_one(".VwiC3b, [data-sncf], .ITZIwc") if block else None
            out.append((_text(h3), urljoin(self.base, a["href"]), _text(snippet)))
        return out

    async def resolve(self, url, browser):
        parsed = urlparse(url)
        if parsed.netloc.endswith("google.com") and parsed.path == "/url":
            q = parse_qs(parsed.query)
            return (q.get("q") or q.get("url") or [url])[0]
        if parsed.netloc.endswith("google.com") and parsed.path == "/goto":
            return await browser.resolve_redirect(url)
        return url


ENGINES: dict[str, Engine] = {e.name: e for e in (Google(), Bing(), DuckDuckGo(), Brave(), Yahoo())}
ALIASES = {"ddg": "duckduckgo", "duck": "duckduckgo", "g": "google", "b": "bing", "y": "yahoo"}
_SEARCH_HOSTS = ("google.", "bing.com", "duckduckgo.com", "yahoo.com", "brave.com", "msn.com/en-us/search")


def resolve_engine_names(spec: str) -> list[str]:
    if spec.strip().lower() == "all":
        return list(ENGINES)
    names = []
    for raw in spec.split(","):
        n = ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if not n:
            continue
        if n not in ENGINES:
            raise ValueError(f"Unknown engine '{raw}'. Choose from: {', '.join(ENGINES)} or 'all'")
        if n not in names:
            names.append(n)
    return names


def _is_search_engine_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(h in host for h in _SEARCH_HOSTS)


async def run_search(
    engine: Engine,
    query: str,
    limit: int,
    http: HttpFetcher,
    browser: BrowserFetcher,
    force_browser: bool = False,
    max_pages: int = 6,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen: set[str] = set()
    offset = 0
    use_browser = engine.requires_browser or force_browser
    terms = query_terms(query)

    def looks_relevant(items: list[tuple[str, str, str]]) -> bool:
        # Engines sometimes "poison" bot traffic with unrelated (often spam) results.
        if not terms or not items:
            return bool(items)
        hits = sum(1 for t, u, s in items if relevance(f"{t} {s} {u}", terms) > 0)
        return hits / len(items) >= 0.3

    for page in range(max_pages):
        url = engine.page_url(query, page, offset)
        items: list[tuple[str, str, str]] = []
        if not use_browser:
            try:
                status, _, html = await http.get_text(url)
                items = engine.parse(html) if status == 200 else []
                if not items:
                    log.info("[%s] plain HTTP returned no results (HTTP %s); retrying in browser", engine.name, status)
                elif not looks_relevant(items):
                    log.info("[%s] plain HTTP results look unrelated to the query; retrying in browser", engine.name)
                    items = []
            except Exception as e:
                log.info("[%s] HTTP error %s; retrying in browser", engine.name, e)
            if not items:
                use_browser = True
        if use_browser and not items:
            res = await browser.fetch(url, settle_ms=1200)
            if res.html and ("/sorry/" in res.final_url or 'id="captcha-form"' in res.html):
                log.warning("[%s] blocked by a CAPTCHA. Try --show-browser to solve it manually, or use other engines.", engine.name)
                break
            items = engine.parse(res.html) if res.html else []
            if not items:
                hint = " - the engine is probably rate-limiting you; wait a few minutes" if res.status in (202, 403, 429) else ""
                log.warning("[%s] no results on page %d (%s)%s", engine.name, page + 1, res.error or "empty page", hint)
                break
            if not looks_relevant(items):
                log.warning("[%s] results look unrelated to the query (engine may be blocking bots); skipping", engine.name)
                break

        offset += len(items)
        before = len(results)
        for title, link, snippet in items:
            link = await engine.resolve(link, browser)
            if not link.startswith("http") or _is_search_engine_url(link):
                continue
            key = link.rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            results.append(SearchResult(engine.name, len(results) + 1, title, link, snippet))
            if len(results) >= limit:
                return results
        if len(results) == before:
            # Page added nothing new (e.g. pagination ignored over plain HTTP).
            if use_browser:
                log.info("[%s] no further results available (stopped at %d)", engine.name, len(results))
                break
            use_browser = True
        await asyncio.sleep(random.uniform(1.0, 2.0))
    return results
