
import asyncio
import random
from collections import Counter

from src.db import Database


async def main():
    async with Database("data/pipeline.db") as db:
        rows = await db.fetch_records("RESEARCH_PAPER")

    print("total:", len(rows))

    # Basic data-quality checks
    bad = [
        r for r in rows
        if not r["source_url"]
        or not r["content"]["authors"]
        or r["content"]["github_stars"] is None
    ]

    print("missing source_url / authors / stars:", len(bad))

    # Count missing repository links separately
    missing_repos = [
        r for r in rows
        if not r["content"].get("github_url")
    ]

    print("papers without GitHub repository:", len(missing_repos))

    # Check duplicate paper URLs
    urls = [r["source_url"] for r in rows]
    print("duplicate paper urls:", len(urls) - len(set(urls)))

    # Count shared repositories, safely ignoring missing URLs
    repos = Counter(
        r["content"]["github_url"].lower()
        for r in rows
        if r["content"].get("github_url")
    )

    shared = [
        (k, v)
        for k, v in repos.most_common(8)
        if v > 1
    ]

    print(
        "repos linked from several papers "
        "(possible platform repos):",
        shared
    )

    # Print 10 random records
    print("\n10 random records to verify by hand:")

    for r in random.sample(rows, min(10, len(rows))):
        c = r["content"]

        print(
            f"\n- {c['title']}"
            f"\n  paper : {c['paper_url']}"
            f"\n  repo  : {c.get('github_url')}"
            f"\n  stars : {c.get('github_stars')}"
        )


asyncio.run(main())
