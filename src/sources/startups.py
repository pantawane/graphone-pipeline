"""Startups vertical: Y Combinator public directory via the yc-oss static API.
Structured data only, so no LLM and no hallucination risk. Every record links to its YC company page."""
import json
import logging
import os
import re

from pydantic import ValidationError

from src.db import Database, utc_now_iso
from src.fetcher import Fetcher
from src.resolver import EntityResolver, normalize, squash
from src.schemas import Source, StartupContent, StartupData, StartupRecord

log = logging.getLogger("startups")

BASE = "https://yc-oss.github.io/api"
SOURCE_NAME = "Y Combinator company directory (via yc-oss API)"
CACHE = "data/yc_ai_startups.json"
ALL_CACHE = "data/yc_all.json"
AI_TAG = re.compile(
    r"\bai\b|aiops|machine learning|generative|llm|deep learning|computer vision|nlp|natural language|chatbot",
    re.I,
)


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _cid(c: dict):
    return c.get("id") or c.get("slug")


async def fetch_yc_ai_startups(fetcher: Fetcher) -> list[dict]:
    res, meta = await fetcher.fetch_json(f"{BASE}/meta.json", robots=False)
    if not isinstance(meta, dict):
        raise RuntimeError(f"yc-oss meta.json failed: {res.error}")
    companies: dict = {}
    for key, val in (meta.get("tags") or {}).items():
        name = val.get("name", key) if isinstance(val, dict) else key
        if not AI_TAG.search(str(name).replace("-", " ")):
            continue
        url = val.get("api") if isinstance(val, dict) and val.get("api") else f"{BASE}/tags/{_slugify(key)}.json"
        r, data = await fetcher.fetch_json(url, robots=False)
        if not isinstance(data, list):
            log.warning("tag %s failed: %s", key, r.error)
            continue
        for c in data:
            companies.setdefault(_cid(c), c)
    return list(companies.values())


async def load_startups(fetcher: Fetcher, refresh: bool = False) -> list[dict]:
    if not refresh and os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    rows = await fetch_yc_ai_startups(fetcher)
    os.makedirs("data", exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    return rows


async def load_all_companies(fetcher: Fetcher, refresh: bool = False) -> list[dict]:
    if not refresh and os.path.exists(ALL_CACHE):
        with open(ALL_CACHE, encoding="utf-8") as f:
            return json.load(f)
    res, data = await fetcher.fetch_json(f"{BASE}/companies/all.json", robots=False)
    if not isinstance(data, list):
        raise RuntimeError(f"yc-oss companies/all.json failed: {res.error}")
    os.makedirs("data", exist_ok=True)
    with open(ALL_CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


async def run(db: Database, fetcher: Fetcher, resolver: EntityResolver, target: int = 10000,
              refresh: bool = False, include_all: bool = False) -> None:
    ai_rows = await load_startups(fetcher, refresh)
    ai_ids = {_cid(c) for c in ai_rows}
    rows = await load_all_companies(fetcher, refresh) if include_all else ai_rows
    pool = [c for c in rows if c.get("status") == "Active" and c.get("website") and c.get("url")]
    pool.sort(key=lambda c: (0 if _cid(c) in ai_ids else 1, c.get("id") or 0))   # AI first, deterministic
    log.info("companies loaded: %d, active with website and page: %d (AI-tagged overall: %d)",
             len(rows), len(pool), len(ai_ids))

    await db.conn.execute("DELETE FROM entity_mapping_log WHERE record_type='STARTUP'")   # idempotent rebuild
    await db.conn.commit()

    inserted = dups = invalid = 0
    for c in pool:
        if inserted >= target:
            break
        res = await resolver.resolve_and_log(db, c["name"], "STARTUP", domain=c["website"], allow_fuzzy=False)
        ts = c.get("team_size")
        try:
            rec = StartupRecord(
                source=Source(name=SOURCE_NAME, url=c["url"]),
                collectedAt=utc_now_iso(),
                content=StartupContent(
                    entityName=res.canonical,
                    data=StartupData(
                        employeeCount=ts if isinstance(ts, int) and ts >= 0 else None,
                        website=c["website"],
                        description=(c.get("one_liner") or None),
                        aiRelated=_cid(c) in ai_ids,
                    ),
                ),
            )
        except ValidationError as e:
            invalid += 1
            log.warning("invalid startup %s: %s", c.get("name"), str(e)[:120])
            continue
        key = squash(normalize(res.canonical))
        if await db.insert_record("STARTUP", key, SOURCE_NAME, c["url"], rec.model_dump()):
            inserted += 1
        else:
            dups += 1

    total = await db.count_records("STARTUP")
    cur = await db.conn.execute(
        "SELECT method, COUNT(*) FROM entity_mapping_log WHERE record_type='STARTUP' GROUP BY method ORDER BY 2 DESC")
    methods = await cur.fetchall()
    stored = await db.fetch_records("STARTUP")
    with_emp = sum(1 for r in stored if r["content"]["data"]["employeeCount"] is not None)
    ai_count = sum(1 for r in stored if r["content"]["data"].get("aiRelated"))
    print(f"\nnew startups inserted: {inserted}   duplicates skipped: {dups}   invalid: {invalid}")
    print(f"total STARTUP records: {total}   with employeeCount: {with_emp}   aiRelated: {ai_count}")
    print("entity resolution methods:", dict(methods))