import asyncio
import sys

from src.db import Database
from src.fetcher import Fetcher
from src.log import setup_logging
from src.sources.products import run


async def main():
    setup_logging("WARNING")
    target = int(next((a for a in sys.argv[1:] if a.isdigit()), 1100))
    retry = "--retry" in sys.argv
    async with Database("data/pipeline.db") as db, Fetcher(max_concurrency=25, per_domain_interval=1.0) as f:
        await run(db, f, target=target, retry=retry)


asyncio.run(main())