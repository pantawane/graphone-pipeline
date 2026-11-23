import asyncio
import sys

from src.db import Database
from src.fetcher import Fetcher
from src.log import setup_logging
from src.resolver import EntityResolver, load_seeds
from src.sources import jobs


async def main():
    setup_logging("WARNING")
    overrides = {"boards-api.greenhouse.io": 0.3, "api.ashbyhq.com": 0.3, "himalayas.app": 1.0, "www.arbeitnow.com": 1.0}
    async with Database("data/pipeline.db") as db, Fetcher(
        max_concurrency=10, per_domain_interval=1.0, domain_overrides=overrides, max_bytes=20_000_000
    ) as f:
        if "--peek" in sys.argv:
            await jobs.peek(f)
        elif "--boards" in sys.argv:
            await jobs.board_check(f)
        else:
            await jobs.run(db, f, EntityResolver(load_seeds()))


asyncio.run(main())