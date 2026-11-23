import asyncio
import sys

from src.db import Database
from src.fetcher import Fetcher
from src.llm import LLMOrchestrator
from src.log import setup_logging
from src.sources import products_render
from src.sources.render import Renderer


async def main():
    setup_logging("WARNING")
    limit = int(next((a for a in sys.argv[1:] if a.isdigit()), 150))
    async with Database("data/pipeline.db") as db, Fetcher(max_concurrency=10, per_domain_interval=1.0) as f:
        async with Renderer(f) as renderer:
            if "--demo" in sys.argv:
                await products_render.demo(f, renderer)
                return
            orch = await LLMOrchestrator.create("config/llm.json", db) if "--llm" in sys.argv else None
            await products_render.run(db, f, renderer, orch, limit)


asyncio.run(main())