from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

import trafilatura
from bs4 import BeautifulSoup

SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".mp4", ".mp3", ".wav",
    ".avi", ".mov", ".webm", ".zip", ".rar", ".7z", ".gz", ".tar", ".exe", ".msi", ".dmg",
    ".apk", ".css", ".js", ".json", ".xml", ".rss", ".woff", ".woff2", ".ttf",
)
SKIP_WORDS = re.compile(
    r"(login|log-in|signin|sign-in|signup|sign-up|register|logout|account|cart|checkout|"
    r"privacy|terms|cookie|legal|careers|jobs|advertis|subscribe|newsletter|contact|"
    r"share|facebook\.com|twitter\.com|x\.com/intent|linkedin\.com/share|pinterest|whatsapp|mailto)",
    re.I,
)
STOPWORDS = set(
    "a an and are as at be by for from how i in is it of on or that the this to was what when where "
    "which who why will with you your vs best top does do can should could would about into much many "
    "get make use using".split()
)
_SUFFIXES = (("ies", "y"), ("ing", ""), ("es", ""), ("ed", ""), ("s", ""))


@dataclass
class Link:
    url: str
    anchor: str
    in_content: bool
    index: int


@dataclass
class Extracted:
    title: str = ""
    text: str = ""
    author: str = ""
    published: str = ""
    description: str = ""
    links: list[Link] = field(default_factory=list)


def stem(word: str) -> str:
    for suffix, repl in _SUFFIXES:
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + repl
    return word


def tokens(text: str) -> set[str]:
    return {stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())}


def query_terms(query: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2 and w not in STOPWORDS]
    return list(dict.fromkeys(stem(w) for w in words))


def relevance(text: str, terms: list[str]) -> int:
    toks = tokens(text)
    return sum(1 for t in terms if t in toks)


def normalize_url(url: str) -> str:
    url, _ = urldefrag(url.strip())
    return url


def _fallback_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg"]):
        tag.decompose()
    body = soup.body or soup
    lines = [ln.strip() for ln in body.get_text("\n").splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _collect_links(soup: BeautifulSoup, base_url: str) -> list[Link]:
    base_tag = soup.find("base", href=True)
    base = urljoin(base_url, base_tag["href"]) if base_tag else base_url
    content_roots = soup.select("article, main, [role=main], #content, .content, .post, .entry-content")
    content_ids = {id(a) for root in content_roots for a in root.find_all("a", href=True)}

    links, seen = [], set()
    for i, a in enumerate(soup.find_all("a", href=True)):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        if a.find_parent(["nav", "header", "footer"]):
            continue
        url = normalize_url(urljoin(base, href))
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or url in seen:
            continue
        if parsed.path.lower().endswith(SKIP_EXTENSIONS):
            continue
        anchor = a.get_text(" ", strip=True)[:200]
        if SKIP_WORDS.search(url) or SKIP_WORDS.search(anchor):
            continue
        seen.add(url)
        links.append(Link(url=url, anchor=anchor, in_content=id(a) in content_ids, index=i))
    return links


def extract_html(html: str, url: str) -> Extracted:
    out = Extracted()
    soup = BeautifulSoup(html, "lxml")
    out.links = _collect_links(soup, url)

    meta = trafilatura.extract_metadata(html, default_url=url)
    if meta:
        out.title = meta.title or ""
        out.author = meta.author or ""
        out.published = meta.date or ""
        out.description = meta.description or ""
    if not out.title and soup.title:
        out.title = soup.title.get_text(strip=True)

    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        include_links=False,
        favor_recall=True,
        deduplicate=True,
    )
    if not text or len(text) < 200:
        fallback = _fallback_text(soup)
        if len(fallback) > len(text or ""):
            text = fallback
    out.text = (text or "").strip()
    return out


def same_site(a: str, b: str) -> bool:
    ha = urlparse(a).netloc.lower().removeprefix("www.")
    hb = urlparse(b).netloc.lower().removeprefix("www.")
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def pick_links(
    links: list[Link],
    page_url: str,
    terms: list[str],
    limit: int,
    same_domain: bool,
    exclude: set[str],
) -> list[Link]:
    """Rank a page's links by relevance to the query and return the best `limit`."""
    if limit <= 0:
        return []
    page_norm = normalize_url(page_url).rstrip("/")
    scored = []
    for link in links:
        norm = link.url.rstrip("/")
        if norm == page_norm or norm in exclude:
            continue
        if same_domain and not same_site(link.url, page_url):
            continue
        if len(link.anchor) < 3:
            continue
        score = 2 * relevance(link.anchor, terms) + relevance(urlparse(link.url).path, terms)
        if score == 0:
            continue
        score += 1 if link.in_content else 0
        scored.append((-score, link.index, link))
    scored.sort(key=lambda s: (s[0], s[1]))
    return [s[2] for s in scored[:limit]]
