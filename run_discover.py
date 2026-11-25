import asyncio
import sys

from src.db import Database
from src.fetcher import Fetcher
from src.llm import LLMOrchestrator
from src.log import setup_logging
from src.sources import products_discover


async def main():
    setup_logging("WARNING")
    limit = int(next((a for a in sys.argv[1:] if a.isdigit()), 500))
    async with Database("data/pipeline.db") as db, Fetcher(max_concurrency=12, per_domain_interval=1.0, timeout_s=30) as f:
        orch = await LLMOrchestrator.create("config/llm.json", db) if "--llm" in sys.argv else None
        await products_discover.run(db, f, orch, limit)


asyncio.run(main())