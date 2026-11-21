import asyncio
import os

from src.db import Database
from src.fetcher import Fetcher
from src.log import setup_logging

DB_PATH = "data/step3_demo.db"
URLS = [
    "https://mistral.ai",
    "https://cohere.com",
    "https://www.pinecone.io",
    "https://mistral.ai/?utm_source=test",                  # same page as the first URL -> duplicate
    "https://mistral.ai/definitely-not-a-real-page-xyz",    # should return 404
    "https://this-domain-does-not-exist-12345.com",         # network error
]


async def main():
    setup_logging()
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)                                  # start clean on every demo run
    async with Database(DB_PATH) as db, Fetcher(max_concurrency=10, per_domain_interval=1.0) as f:
        fresh = []
        for u in URLS:
            if await db.claim_url(u, "demo"):
                fresh.append(u)
            else:
                print(f"DUPLICATE skipped: {u}")

        results = await asyncio.gather(*[f.fetch(u) for u in fresh])
        for r in results:
            print(f"{'OK ' if r.ok else 'ERR'} status={r.status:<3} attempts={r.attempts} "
                  f"{r.elapsed:5.1f}s bytes={len(r.text):<7} {r.error or ''}  {r.url}")
            if r.ok:
                await db.save_raw_page(r.url, r.final_url, r.status, r.text)

        print("\nraw pages stored:", await db.count_raw_pages())


asyncio.run(main())