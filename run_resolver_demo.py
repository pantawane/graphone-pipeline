import asyncio

from src.db import Database
from src.resolver import EntityResolver, load_seeds

MESSY = [
    ("OpenAI", None), ("OpenAI, Inc.", None), ("Open AI", None), ("OPEN AI", None),
    ("HuggingFace Inc.", None), ("W&B", None), ("Weights and Biases", None),
    ("Mistral", None), ("Mistral AI SAS", None), ("Anthropik", None), ("Cohere AI", None),
    ("Together", None), ("Scale", None), ("Grok", None), ("Coherent", None),
    ("Foo Labs Inc", None), ("Foo Labs", None), ("Acme Robotics", None),
    ("Unknown Co", "https://careers.openai.com/jobs/1"),
]


async def main():
    resolver = EntityResolver(load_seeds())
    async with Database("data/pipeline.db") as db:
        for raw, domain in MESSY:
            r = await resolver.resolve_and_log(db, raw, "DEMO", domain)
            print(f"{raw!r:28} -> {r.canonical!r:20} [{r.method}{' NEW' if r.is_new else ''}]")


asyncio.run(main())