import asyncio
import os

import aiohttp
from dotenv import load_dotenv

load_dotenv()
token = os.getenv("GITHUB_TOKEN")
print("token loaded:", bool(token))


async def main():
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession() as s:
        async with s.get("https://api.github.com/repos/pytorch/pytorch", headers=headers) as r:
            print("status:", r.status)
            print("rate limit:", r.headers.get("X-RateLimit-Limit"), "remaining:", r.headers.get("X-RateLimit-Remaining"))
            data = await r.json()
            print("pytorch stars:", data.get("stargazers_count"))


asyncio.run(main())