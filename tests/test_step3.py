import asyncio

from aiohttp import web

from src.db import Database, canonical_url
from src.fetcher import Fetcher, backoff_delay


def test_canonical_url_merges_variants():
    a = canonical_url("https://www.Example.com/a/?utm_source=x&b=2&a=1#frag")
    b = canonical_url("http://example.com/a?a=1&b=2")
    assert a == b == "https://example.com/a?a=1&b=2"


def test_backoff_stays_within_cap():
    for attempt in range(10):
        assert 0 <= backoff_delay(attempt, base=1.0, cap=30.0) <= 30.0


def test_claim_url_dedupes(tmp_path):
    async def run():
        async with Database(str(tmp_path / "t.db")) as db:
            first = await db.claim_url("https://www.example.com/a/?utm_source=x&b=2&a=1#f", "news")
            second = await db.claim_url("http://example.com/a?a=1&b=2", "news")
            return first, second
    assert asyncio.run(run()) == (True, False)


def test_record_needs_source_url_and_dedupes(tmp_path):
    async def run():
        async with Database(str(tmp_path / "t.db")) as db:
            ok1 = await db.insert_record("STARTUP", "openai", "src", "https://example.com/x", {"a": 1})
            ok2 = await db.insert_record("STARTUP", "openai", "src", "https://example.com/x", {"a": 1})
            try:
                await db.insert_record("STARTUP", "nosrc", "src", "", {"a": 1})
                raised = False
            except ValueError:
                raised = True
            return ok1, ok2, raised
    assert asyncio.run(run()) == (True, False, True)


async def _serve(counter):
    async def flaky(request):
        counter["n"] += 1
        if counter["n"] <= 2:
            return web.Response(status=429, headers={"Retry-After": "0"})
        return web.Response(text="hello")

    async def missing(request):
        counter["m"] += 1
        return web.Response(status=404, text="nope")

    app = web.Application()
    app.router.add_get("/", flaky)
    app.router.add_get("/missing", missing)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


def test_fetcher_retries_429_then_succeeds():
    async def run():
        counter = {"n": 0, "m": 0}
        runner, port = await _serve(counter)
        try:
            async with Fetcher(per_domain_interval=0, backoff_base=0.05, respect_robots=False) as f:
                res = await f.fetch(f"http://127.0.0.1:{port}/")
        finally:
            await runner.cleanup()
        return res, counter
    res, counter = asyncio.run(run())
    assert res.ok and res.text == "hello" and res.attempts == 3 and counter["n"] == 3


def test_fetcher_does_not_retry_404():
    async def run():
        counter = {"n": 0, "m": 0}
        runner, port = await _serve(counter)
        try:
            async with Fetcher(per_domain_interval=0, backoff_base=0.05, respect_robots=False) as f:
                res = await f.fetch(f"http://127.0.0.1:{port}/missing")
        finally:
            await runner.cleanup()
        return res, counter
    res, counter = asyncio.run(run())
    assert not res.ok and res.error == "http_404" and res.attempts == 1 and counter["m"] == 1