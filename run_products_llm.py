import asyncio
import sys

from src.db import Database
from src.fetcher import Fetcher
from src.llm import LLMOrchestrator
from src.log import setup_logging
from src.sources.products_llm import run


async def main():
    setup_logging("WARNING")
    limit = int(next((a for a in sys.argv[1:] if a.isdigit()), 300))
    async with Database("data/pipeline.db") as db, Fetcher(max_concurrency=10, per_domain_interval=1.0) as f:
        orch = await LLMOrchestrator.create("config/llm.json", db)
        await run(db, f, orch, limit=limit)


asyncio.run(main())