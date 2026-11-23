"""Phase V: render JavaScript-heavy pages politely and DETECT bot challenges instead of fighting them.

Policy: robots.txt is honoured, one request per domain every few seconds, an honest User-Agent with a contact,
images/fonts/media are not downloaded, and a challenge page (Cloudflare, DataDome, CAPTCHA) is logged and skipped.
No CAPTCHA solving, no proxy rotation, no fingerprint spoofing."""
import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.fetcher import DomainLimiter, Fetcher

log = logging.getLogger("render")

STRONG_MARKERS = (
    "just a moment...", "cf-chl", "cf_chl_opt", "attention required! | cloudflare",
    "checking your browser before accessing", "enable javascript and cookies to continue",
    "geo.captcha-delivery.com", "px-captcha",
)


def detect_block(status: int, headers: dict, html: str) -> "str | None":
    """Returns a reason string if the response is a bot challenge or refusal, else None."""
    h = {str(k).lower(): str(v).lower() for k, v in (headers or {}).items()}
    if h.get("cf-mitigated") == "challenge":
        return "cloudflare_challenge"
    low = (html or "").lower()
    visible = len(re.sub(r"<[^>]+>", " ", low))
    marker = next((m for m in STRONG_MARKERS if m in low), None)
    if marker and (status in (403, 429, 503) or visible < 3000):
        return "challenge_page"
    if status in (401, 403, 429, 503):
        return f"http_{status}"
    return None


@dataclass
class RenderResult:
    url: str
    final_url: str = ""
    status: int = 0
    html: str = ""
    blocked: "str | None" = None
    error: "str | None" = None

    @property
    def ok(self) -> bool:
        return self.blocked is None and self.error is None and 200 <= self.status < 300


class Renderer:
    def __init__(self, fetcher: Fetcher, concurrency: int = 3, nav_timeout_ms: int = 30000,
                 per_domain_interval: float = 2.0):
        self.fetcher = fetcher
        self.sem = asyncio.Semaphore(concurrency)
        self.limiter = DomainLimiter(per_domain_interval)
        self.nav_timeout_ms = nav_timeout_ms
        self._pw = self._browser = self._context = None

    async def __aenter__(self) -> "Renderer":
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent=self.fetcher.ua, locale="en-US", viewport={"width": 1280, "height": 900})
        await self._context.route("**/*", self._route)
        return self

    async def __aexit__(self, *exc) -> None:
        await self._context.close()
        await self._browser.close()
        await self._pw.stop()

    async def _route(self, route) -> None:
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()                                   # text is all we need
        else:
            await route.continue_()

    async def render(self, url: str) -> RenderResult:
        parts = urlsplit(url)
        if self.fetcher.respect_robots:
            rp = await self.fetcher._robots_for(parts.scheme, parts.netloc)
            if not rp.can_fetch(self.fetcher.ua, url):
                return RenderResult(url, blocked="blocked_by_robots")
        async with self.sem:
            await self.limiter.wait(parts.hostname or "")
            page = await self._context.new_page()
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass                                          # some pages never go idle: use what we have
                await page.wait_for_timeout(500)
                html = await page.content()
                status = resp.status if resp else 0
                headers = await resp.all_headers() if resp else {}
                return RenderResult(url, page.url, status, html, detect_block(status, headers, html), None)
            except Exception as exc:
                return RenderResult(url, error=f"{type(exc).__name__}: {str(exc)[:120]}")
            finally:
                await page.close()