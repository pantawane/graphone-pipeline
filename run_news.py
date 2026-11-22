import asyncio
import sys

from src.db import Database
from src.fetcher import Fetcher
from src.llm import LLMOrchestrator
from src.log import setup_logging
from src.resolver import EntityResolver, load_seeds
from src.sources.news import run


async def main():
    setup_logging("WARNING")
    use_llm = "--llm" in sys.argv
    async with Database("data/pipeline.db") as db, Fetcher(
        max_concurrency=10, per_domain_interval=1.0, domain_overrides={"hn.algolia.com": 0.5}
    ) as f:
        orch = await LLMOrchestrator.create("config/llm.json", db) if use_llm else None
        await run(db, f, EntityResolver(load_seeds()), orch)


asyncio.run(main())