import asyncio
import sys

from src.db import Database
from src.fetcher import Fetcher
from src.log import setup_logging
from src.resolver import EntityResolver, load_seeds
from src.sources.startups import run


async def main():
    setup_logging()
    refresh = "--refresh" in sys.argv
    include_all = "--all" in sys.argv
    async with Database("data/pipeline.db") as db, Fetcher(
        max_concurrency=4, per_domain_interval=0.5, max_bytes=80_000_000, timeout_s=90
    ) as f:
        await run(db, f, EntityResolver(load_seeds()), refresh=refresh, include_all=include_all)


asyncio.run(main())