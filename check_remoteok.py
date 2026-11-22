import asyncio

from src.fetcher import Fetcher
from src.log import setup_logging


async def main():
    setup_logging("ERROR")
    async with Fetcher(per_domain_interval=1.1) as f:
        r, data = await f.fetch_json("https://remoteok.com/api", robots=False)
    items = [i for i in data if isinstance(i, dict) and i.get("date")]
    items.sort(key=lambda i: i["date"], reverse=True)
    print("newest 5:")
    for i in items[:5]:
        print(" ", i.get("date"), i.get("epoch"), i.get("position"), "|", i.get("company"))


asyncio.run(main())