"""Async fetcher: concurrency cap, per-domain politeness, robots.txt, retries with backoff + jitter."""
import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger("fetcher")

DEFAULT_UA = os.getenv("USER_AGENT", "FrontierAtlasDemoBot/0.1 (contact: your-email@example.com)")
RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    status: int = 0
    final_url: str = ""
    text: str = ""
    headers: dict = field(default_factory=dict)
    error: str | None = None
    attempts: int = 0
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Exponential backoff with FULL jitter: random between 0 and min(cap, base * 2^attempt)."""
    return random.uniform(0, min(cap, base * (2 ** attempt)))


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return min(float(value), 60.0)
    except ValueError:
        return None          # HTTP-date form: ignore, fall back to backoff


def decode_body(data: bytes, charset: str | None) -> str:
    for enc in (charset, "utf-8"):
        if not enc:
            continue
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


class DomainLimiter:
    """Minimum gap between requests to one domain. Widens after a 429, narrows slowly after successes."""

    def __init__(self, default_interval: float = 1.0, overrides: dict | None = None):
        self.default = default_interval
        self.overrides = dict(overrides or {})
        self._base = dict(self.overrides)          # the configured intervals
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_penalty: dict[str, float] = {}

    def _base_interval(self, domain: str) -> float:
        return self._base.get(domain, self.default)

    def penalize(self, domain: str, factor: float = 1.5, cap: float = 20.0) -> None:
        now = time.monotonic()
        cur = self.overrides.get(domain, self.default)
        if now - self._last_penalty.get(domain, 0.0) < cur:   # one penalty per interval, not per queued request
            return
        self._last_penalty[domain] = now
        self.overrides[domain] = min(cap, max(cur, 0.5) * factor)
        log.warning("slowing down %s: interval now %.1fs", domain, self.overrides[domain])

    def reward(self, domain: str) -> None:
        cur = self.overrides.get(domain)
        if cur is not None:
            self.overrides[domain] = max(self._base_interval(domain), cur * 0.9)

    async def wait(self, domain: str) -> None:
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            interval = self.overrides.get(domain, self.default)
            now = time.monotonic()
            nxt = self._next.get(domain, 0.0)
            if nxt > now:
                await asyncio.sleep(nxt - now)
            self._next[domain] = time.monotonic() + interval


class Fetcher:
    def __init__(self, max_concurrency: int = 20, per_domain_interval: float = 1.0,
                 domain_overrides: dict | None = None, timeout_s: float = 25, max_retries: int = 3,
                 backoff_base: float = 1.0, max_bytes: int = 5_000_000,
                 user_agent: str = DEFAULT_UA, respect_robots: bool = True):
        self.max_concurrency = max_concurrency
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_bytes = max_bytes
        self.ua = user_agent
        self.respect_robots = respect_robots
        self.limiter = DomainLimiter(per_domain_interval, domain_overrides)
        self.sem = asyncio.Semaphore(max_concurrency)
        self.session: aiohttp.ClientSession | None = None
        self._robots: dict[str, robotparser.RobotFileParser] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> "Fetcher":
        connector = aiohttp.TCPConnector(limit=self.max_concurrency, limit_per_host=4)
        self.session = aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=self.timeout_s))
        return self

    async def __aexit__(self, *exc) -> None:
        if self.session:
            await self.session.close()

    async def _robots_for(self, scheme: str, netloc: str) -> robotparser.RobotFileParser:
        key = f"{scheme}://{netloc}"
        if key in self._robots:
            return self._robots[key]
        lock = self._robots_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._robots:
                return self._robots[key]
            rp = robotparser.RobotFileParser()
            try:
                async with self.session.get(
                        f"{key}/robots.txt", headers={"User-Agent": self.ua},
                        timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        rp.parse((await r.read()).decode("utf-8", "replace").splitlines())
                        rp.modified()
                    elif r.status in (401, 403) or r.status >= 500:
                        rp.disallow_all = True          # be conservative
                    else:
                        rp.allow_all = True             # no robots.txt = no restrictions
            except Exception as e:
                log.warning("robots.txt unreachable for %s (%s): assuming allowed", key, type(e).__name__)
                rp.allow_all = True
            self._robots[key] = rp
            return rp

    async def fetch(self, url: str, *, robots: bool = True, headers: dict | None = None) -> FetchResult:
        """Never raises. Check result.ok / result.error.

        Pass robots=False only for official public APIs that are meant for programmatic access
        (and document that choice)."""
        parts = urlsplit(url)
        domain = parts.hostname or ""
        res = FetchResult(url=url)

        if robots and self.respect_robots:
            rp = await self._robots_for(parts.scheme, parts.netloc)
            if not rp.can_fetch(self.ua, url):
                res.error = "blocked_by_robots"
                log.info("skip (robots.txt) %s", url)
                return res

        hdrs = {"User-Agent": self.ua, "Accept": "*/*", "Accept-Language": "en"}
        hdrs.update(headers or {})
        t0 = time.monotonic()

        for attempt in range(self.max_retries + 1):
            res.attempts = attempt + 1
            await self.limiter.wait(domain)          # politeness first, outside the global semaphore
            retry_delay: float | None = None
            try:
                async with self.sem:
                    async with self.session.get(url, headers=hdrs) as r:
                        data = await r.read()
                        status = r.status
                        if status == 429:
                            self.limiter.penalize(domain)
                        if status in RETRY_STATUS and attempt < self.max_retries:
                            ra = parse_retry_after(r.headers.get("Retry-After"))
                            retry_delay = ra if ra is not None else backoff_delay(attempt, self.backoff_base)
                            log.warning("HTTP %s from %s, retry %d/%d in %.1fs",
                                        status, domain, attempt + 1, self.max_retries, retry_delay)
                        else:
                            res.status = status
                            res.final_url = str(r.url)
                            res.headers = dict(r.headers)
                            res.text = decode_body(data[: self.max_bytes], r.charset)
                            res.error = None if 200 <= status < 300 else f"http_{status}"
                            if 200 <= status < 300:
                                self.limiter.reward(domain)
                            break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < self.max_retries:
                    retry_delay = backoff_delay(attempt, self.backoff_base)
                    log.warning("%s fetching %s, retry %d/%d in %.1fs",
                                type(e).__name__, domain, attempt + 1, self.max_retries, retry_delay)
                else:
                    res.error = f"{type(e).__name__}: {str(e)[:120]}"
                    break
            if retry_delay is not None:
                await asyncio.sleep(retry_delay)     # sleep OUTSIDE the semaphore

        res.elapsed = time.monotonic() - t0
        if res.error:
            log.info("FAILED %s -> %s (attempts=%d)", url, res.error, res.attempts)
        return res

    async def fetch_json(self, url: str, **kw) -> tuple[FetchResult, object | None]:
        res = await self.fetch(url, **kw)
        if not res.ok:
            return res, None
        try:
            return res, json.loads(res.text)
        except json.JSONDecodeError:
            res.error = "invalid_json"
            return res, None