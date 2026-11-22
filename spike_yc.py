import asyncio
import json
import re
from collections import Counter

from src.fetcher import Fetcher
from src.log import setup_logging

BASE = "https://yc-oss.github.io/api"
AI_TAG = re.compile(
    r"\bai\b|aiops|machine learning|generative|llm|deep learning|computer vision|nlp|natural language|chatbot",
    re.I,
)


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def parse_tags(tags) -> list[dict]:
    """meta['tags'] may be a dict (key -> info) or a list. Returns [{name, slug, count, api}]."""
    out = []
    items = tags.items() if isinstance(tags, dict) else [(None, t) for t in tags]
    for key, val in items:
        if isinstance(val, dict):
            name = val.get("name") or (key if isinstance(key, str) else "")
            slug = val.get("slug") or (slugify(key) if isinstance(key, str) else slugify(name))
            count = val.get("count")
            api = val.get("api")
        else:
            name = key if isinstance(key, str) else str(val)
            slug = slugify(name)
            count = val if isinstance(val, int) else None
            api = None
        if name:
            out.append({"name": name, "slug": slug, "count": count, "api": api})
    return out


async def main():
    setup_logging("WARNING")
    async with Fetcher(max_concurrency=4, per_domain_interval=0.5) as f:
        res, meta = await f.fetch_json(f"{BASE}/meta.json", robots=False)
        if not isinstance(meta, dict):
            print("meta.json failed:", res.error)
            return
        print("meta keys:", list(meta.keys()))
        raw_tags = meta.get("tags", {})
        print("tags type:", type(raw_tags).__name__, " size:", len(raw_tags))
        sample = list(raw_tags.items())[:3] if isinstance(raw_tags, dict) else raw_tags[:3]
        print("first 3 raw tag entries:", sample, "\n")

        tags = parse_tags(raw_tags)
        ai_tags = [t for t in tags if AI_TAG.search(t["name"].replace("-", " "))]
        print("AI-related tags found:")
        for t in ai_tags:
            print(f"  {t['name']!r:40} slug={t['slug']!r:35} count={t['count']}")

        companies: dict = {}
        failed = []
        for t in ai_tags:
            url = t["api"] if isinstance(t["api"], str) and t["api"].startswith("http") else f"{BASE}/tags/{t['slug']}.json"
            r, data = await f.fetch_json(url, robots=False)
            if not isinstance(data, list):
                failed.append((t["slug"], r.error))
                continue
            for c in data:
                key = c.get("id") or c.get("slug")
                companies.setdefault(key, c)
        print("\ntag files that failed:", failed)

    rows = list(companies.values())
    with_site = [c for c in rows if c.get("website")]
    with_size = [c for c in rows if isinstance(c.get("team_size"), int)]
    print(f"\nunique AI-related companies : {len(rows)}")
    print(f"  with a website            : {len(with_site)}")
    print(f"  with team_size            : {len(with_size)}")
    print(f"  status                    : {dict(Counter(c.get('status') for c in rows))}")
    if rows:
        print("\nexample record keys:", sorted(rows[0].keys()))
        print("example:", {k: rows[0].get(k) for k in ("name", "website", "team_size", "status", "url", "batch")})

    with open("data/yc_ai_startups.json", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False)
    print("\nsaved data/yc_ai_startups.json")


asyncio.run(main())