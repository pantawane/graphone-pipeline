import asyncio
import os
import sys

from dotenv import load_dotenv

from src.db import Database
from src.fetcher import Fetcher
from src.log import setup_logging
from src.sources.papers import run


async def main():
    load_dotenv()
    setup_logging()
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
    async with Database("data/pipeline.db") as db, Fetcher(
        max_concurrency=10,
        per_domain_interval=0.5,
        domain_overrides={"export.arxiv.org": 3.0, "api.github.com": 0.15},
    ) as f:
        await run(db, f, target, os.getenv("GITHUB_TOKEN"))


asyncio.run(main())