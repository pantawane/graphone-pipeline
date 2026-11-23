"""Final audit: re-validates every stored record against its schema, runs integrity checks, verifies that
every product's evidence exists in the saved page, and writes a 30-row manual sample to output/audit_sample.md."""
import asyncio
import json
import os
import random
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from pydantic import ValidationError

from src.db import url_hash
from src.schemas import JobRecord, NewsRecord, ProductRecord, ResearchPaperRecord, StartupRecord

load_dotenv()
MODELS = {"STARTUP": StartupRecord, "PRODUCT": ProductRecord, "RESEARCH_PAPER": ResearchPaperRecord,
          "JOB": JobRecord, "NEWS": NewsRecord}
SAMPLE = {"STARTUP": 4, "PRODUCT": 8, "RESEARCH_PAPER": 6, "JOB": 6, "NEWS": 6}
NOW = datetime.now(timezone.utc)
CUT = NOW - timedelta(hours=24)
norm = lambda s: re.sub(r"\s+", " ", s).lower().strip()


def iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def describe(rtype: str, d: dict):
    c = d["content"]
    if rtype == "STARTUP":
        return (f"{c['entityName']} | team size {c['data'].get('employeeCount')} | "
                f"check the name and team size on the YC page"), d["source"]["url"]
    if rtype == "PRODUCT":
        return (f"{c['startupName']} -> {c['pricingModel']} ({c.get('method') or 'rules'}) | evidence: "
                f"{' | '.join(c['evidence'])[:300]} | open the page, Ctrl+F the evidence: is the label right?"), c["pricingUrl"]
    if rtype == "RESEARCH_PAPER":
        return (f"{c['title'][:100]} | repo {c.get('github_url')} | stars {c.get('github_stars')} | "
                f"arXiv title matches? repo exists? stars close?"), c["paper_url"]
    if rtype == "JOB":
        return (f"{c['company']}: {c['title']} | {c['date']} | remote={c['is_remote']} | {c['role_family']} | "
                f"company, date, remote flag and role family right?"), d["source"]["url"]
    return (f"{c['title'][:100]} | {c['published_date']} ({c['date_source']}, {c['date_confidence']}) | "
            f"date matches the page? full text is the article?"), d["source"]["url"]


async def check_urls(urls: list[str]):
    ua = os.getenv("USER_AGENT", "FrontierAtlasDemoBot/0.1")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers={"User-Agent": ua}) as s:
        async def one(u):
            try:
                async with s.get(u, allow_redirects=True) as r:
                    return u, r.status
            except Exception as e:
                return u, type(e).__name__
        return await asyncio.gather(*[one(u) for u in urls])


def main() -> int:
    con = sqlite3.connect("data/pipeline.db")
    problems: list[str] = []
    rows_by_type = {}

    print("== 1. schema validation of every stored record")
    for rtype, model in MODELS.items():
        rows = con.execute("SELECT id, source_url, data_json FROM records WHERE record_type=? ORDER BY id",
                           (rtype,)).fetchall()
        rows_by_type[rtype] = rows
        bad = 0
        for rid, url, dj in rows:
            try:
                model.model_validate(json.loads(dj))
            except ValidationError as e:
                bad += 1
                if bad <= 3:
                    print(f"   INVALID {rtype} id={rid} {url}: {str(e)[:150]}")
        print(f"   {rtype:15} {len(rows):6} records, {bad} invalid")
        if bad:
            problems.append(f"{rtype}: {bad} records fail schema validation")

    print("\n== 2. source URLs")
    for rtype, rows in rows_by_type.items():
        urls = [r[1] for r in rows]
        missing = sum(1 for u in urls if not u or not u.startswith(("http://", "https://")))
        dups = len(urls) - len(set(urls))
        note = "  (allowed: products may share a pricing page)" if rtype == "PRODUCT" and dups else ""
        print(f"   {rtype:15} missing/invalid URL: {missing}   duplicate URLs: {dups}{note}")
        if missing:
            problems.append(f"{rtype}: {missing} records without a valid source URL")
        if dups and rtype != "PRODUCT":
            problems.append(f"{rtype}: {dups} duplicate source URLs")

    print("\n== 3. product evidence exists in the saved page text")
    ok = nopage = bad = 0
    for rid, url, dj in rows_by_type["PRODUCT"]:
        c = json.loads(dj)["content"]
        body = con.execute("SELECT body FROM raw_pages WHERE url_hash=? ORDER BY id DESC LIMIT 1",
                           (url_hash(c["pricingUrl"]),)).fetchone()
        if not body or not body[0]:
            nopage += 1
        elif all(norm(e) in norm(body[0]) for e in c["evidence"]):
            ok += 1
        else:
            bad += 1
            if bad <= 5:
                print(f"   EVIDENCE NOT FOUND: {c['startupName']} {c['pricingUrl']}")
    print(f"   verified: {ok}   no saved page: {nopage}   evidence missing: {bad}")
    if bad:
        problems.append(f"{bad} products whose evidence is not in the saved page")
    if nopage:
        problems.append(f"{nopage} products without a saved page (cannot verify)")

    print("\n== 4. research papers")
    papers = [json.loads(r[2])["content"] for r in rows_by_type["RESEARCH_PAPER"]]
    with_stars = sum(1 for c in papers if c.get("github_stars") is not None)
    inconsistent = sum(1 for c in papers if (c.get("github_url") is None) != (c.get("github_stars") is None))
    print(f"   with GitHub stars: {with_stars}   repo/stars inconsistent: {inconsistent}")
    if with_stars < 1000:
        problems.append(f"only {with_stars} papers with GitHub stars (minimum 1,000)")
    if inconsistent:
        problems.append(f"{inconsistent} papers where repo and stars disagree")

    print("\n== 5. freshness (records still inside the 24-hour window now)")
    for rtype, key in (("NEWS", "published_date"), ("JOB", "date")):
        cs = [json.loads(r[2])["content"] for r in rows_by_type[rtype]]
        fresh = sum(1 for c in cs if iso(c[key]) >= CUT)
        inferred = sum(1 for c in cs if c.get("date_confidence") == "inferred")
        print(f"   {rtype:5} stored: {len(cs)}   fresh now: {fresh}   inferred dates: {inferred}")
    print("   (export_sheet.py drops anything older than 24h and leaves out inferred news dates)")

    # ---- manual sample
    rng = random.Random()
    picks = []
    for rtype, n in SAMPLE.items():
        pool = rows_by_type.get(rtype, [])
        if rtype == "PRODUCT":
            llm = [r for r in pool if (json.loads(r[2])["content"].get("method") or "rules").startswith(("llm", "rules+render"))]
            rules = [r for r in pool if r not in llm]
            chosen = rng.sample(llm, min(n // 2, len(llm))) + rng.sample(rules, min(n - n // 2, len(rules)))
        else:
            chosen = rng.sample(pool, min(n, len(pool)))
        picks += [(rtype, r) for r in chosen]
    lines = [f"# Manual audit sample ({NOW:%Y-%m-%d %H:%M} UTC)", "",
             "Open each URL and tick the box only if the record matches the page.", ""]
    urls = []
    for rtype, (rid, url, dj) in picks:
        text, link = describe(rtype, json.loads(dj))
        urls.append(link)
        lines += [f"- [ ] **{rtype}** {text}", f"  {link}", ""]
    Path("output").mkdir(exist_ok=True)
    Path("output/audit_sample.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n== 6. manual sample written: output/audit_sample.md ({len(picks)} rows). Open and tick each one.")

    if "--check-urls" in sys.argv:
        print("\n== 7. do the sampled URLs resolve?")
        for u, status in asyncio.run(check_urls(urls)):
            flag = "" if isinstance(status, int) and status < 400 else (
                "   <- blocked for bots, check in a browser" if status in (403, 429) else "   <- PROBLEM")
            print(f"   {status!s:>6} {u}{flag}")
            if flag.endswith("PROBLEM"):
                problems.append(f"sampled URL does not resolve: {u}")

    print("\nPROBLEMS:" if problems else "\nno automated problems found")
    for p in problems:
        print("  -", p)
    return 1 if problems else 0


sys.exit(main())