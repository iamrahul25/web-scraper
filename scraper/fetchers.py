from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger("scraper")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_BYTES = 10 * 1024 * 1024
_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.I)


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    html: str | None = None
    text: str | None = None
    error: str | None = None
    via: str = "http"

    @property
    def ok(self) -> bool:
        return self.error is None and (self.html is not None or self.text is not None)


def _decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type)
    charset = m.group(1) if m else None
    if not charset:
        mm = _META_CHARSET.search(body[:4096])
        charset = mm.group(1).decode("ascii", "ignore") if mm else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _pdf_to_text(body: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()


class HttpFetcher:
    def __init__(self, timeout: float = 20.0):
        self.client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout),
            http2=True,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )

    async def get_text(self, url: str) -> tuple[int, str, str]:
        """Plain GET for search pages and robots.txt. Returns (status, final_url, text)."""
        r = await self.client.get(url)
        return r.status_code, str(r.url), r.text

    async def fetch(self, url: str) -> FetchResult:
        res = FetchResult(url=url)
        try:
            async with self.client.stream("GET", url) as r:
                res.status = r.status_code
                res.final_url = str(r.url)
                res.content_type = r.headers.get("content-type", "").lower()
                if r.status_code >= 400:
                    res.error = f"HTTP {r.status_code}"
                    return res
                ct = res.content_type
                is_pdf = "pdf" in ct or (not ct and url.lower().endswith(".pdf"))
                is_text = not ct or any(t in ct for t in ("html", "xml", "text/plain"))
                if not (is_pdf or is_text):
                    res.error = f"unsupported content-type: {ct}"
                    return res
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        break
                body = b"".join(chunks)
            if is_pdf:
                res.text = await asyncio.to_thread(_pdf_to_text, body)
            elif "text/plain" in ct:
                res.text = _decode(body, ct)
            else:
                res.html = _decode(body, ct)
        except Exception as e:  # network errors, TLS, decode failures, bad PDFs
            res.error = f"{type(e).__name__}: {e}"[:300]
        return res

    async def close(self) -> None:
        await self.client.aclose()


class BrowserFetcher:
    """Lazily-started headless Chromium (Playwright). Needed for Google/Brave and JS-heavy pages."""

    def __init__(self, headless: bool = True, max_pages: int = 4, timeout: float = 30.0):
        self.headless = headless
        self.timeout_ms = int(timeout * 1000)
        self._sem = asyncio.Semaphore(max_pages)
        self._lock = asyncio.Lock()
        self._pw = self._browser = self._ctx = None
        self.available: bool | None = None  # unknown until first start attempt
        self.unavailable_reason = ""

    async def _ensure(self) -> bool:
        if self.available is not None:
            return self.available
        async with self._lock:
            if self.available is not None:
                return self.available
            try:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    headless=self.headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                self._ctx = await self._browser.new_context(
                    user_agent=USER_AGENT, locale="en-US", viewport={"width": 1366, "height": 900}
                )
                await self._ctx.route("**/*", self._block_heavy)
                self.available = True
            except Exception as e:
                self.available = False
                self.unavailable_reason = str(e).splitlines()[0][:200]
                log.warning(
                    "Headless browser unavailable (%s). Run: python -m playwright install chromium",
                    self.unavailable_reason,
                )
        return self.available

    @staticmethod
    async def _block_heavy(route):
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()

    async def fetch(self, url: str, settle_ms: int = 1500) -> FetchResult:
        res = FetchResult(url=url, via="browser")
        if not await self._ensure():
            res.error = f"browser unavailable: {self.unavailable_reason}"
            return res
        async with self._sem:
            page = await self._ctx.new_page()
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                try:
                    await page.wait_for_load_state("networkidle", timeout=settle_ms + 3000)
                except Exception:
                    pass
                await page.wait_for_timeout(settle_ms)
                res.status = resp.status if resp else 0
                res.final_url = page.url
                res.content_type = (resp.headers.get("content-type", "") if resp else "").lower()
                if res.status >= 400:
                    res.error = f"HTTP {res.status}"
                res.html = await page.content()
            except Exception as e:
                res.error = f"{type(e).__name__}: {e}".splitlines()[0][:300]
            finally:
                await page.close()
        return res

    async def resolve_redirect(self, url: str) -> str:
        """Follow one redirect hop using the browser's cookies (e.g. Google's /goto links)."""
        if not await self._ensure():
            return url
        try:
            r = await self._ctx.request.get(url, max_redirects=0, timeout=self.timeout_ms)
            return r.headers.get("location") or url
        except Exception:
            return url

    async def close(self) -> None:
        for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    await getattr(obj, meth)()
                except Exception:
                    pass
