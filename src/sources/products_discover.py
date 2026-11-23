"""Step 9b: find pricing pages that normal link discovery missed (sitemap.xml + extra common paths)."""
import asyncio
import logging
import random
import re
from collections import Counter
from urllib.parse import urljoin, urlsplit

from pydantic import ValidationError

from src.db import Database, utc_now_iso
from src.fetcher import Fetcher
from src.llm import LLMOrchestrator
from src.resolver import normalize, squash
from src.schemas import ProductContent, ProductRecord, Source
from src.sources.products import SKIP_LINK, SOURCE_NAME, classify, mentions_company, page_text, same_site
from src.sources.products_llm import FOCUS, PROMPT, exact_slices, make_validator

log = logging.getLogger("products_discover")

LOC_RE = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s]+?)\s*(?:\]\]>)?\s*</loc>", re.I)
PRICING_PATH_RE = re.compile(
    r"(?:^|/)(?:pricing|plans?|prices?|pricing-plans|plans-pricing|plans-and-pricing|pricing-and-plans|subscriptions?)/?$",
    re.I)
EXTRA_PATHS = ["/pricing-plans", "/plans-and-pricing", "/price", "/subscribe", "/en/pricing", "/product/pricing"]


async def sitemap_urls(f: Fetcher, website: str) -> list[str]:
    r = await f.fetch(urljoin(website, "/sitemap.xml"))
    if not r.ok or "<loc" not in r.text.lower():
        return []
    locs = LOC_RE.findall(r.text)
    if "<sitemapindex" not in r.text.lower():
        return locs[:5000]
    children = [u for u in locs if u.lower().endswith(".xml")]
    children.sort(key=lambda u: 0 if re.search(r"page|static|main|site", u, re.I) else 1)
    out: list[str] = []
    for child in children[:3]:
        cr = await f.fetch(child)
        if cr.ok:
            out += LOC_RE.findall(cr.text)
    return out[:5000]


def pick_candidates(locs: list[str], website: str) -> list[str]:
    hits = []
    for u in locs:
        if not u.startswith("http") or not same_site(u, website):
            continue
        path = urlsplit(u).path
        if SKIP_LINK.search(path) or not PRICING_PATH_RE.search(path):
            continue
        hits.append(u)
    hits.sort(key=lambda u: (urlsplit(u).path.count("/"), len(u)))
    return hits[:2]


async def process(f: Fetcher, orch, key: str, st: dict):
    website = st["content"]["data"]["website"]
    cands = pick_candidates(await sitemap_urls(f, website), website)
    cands += [urljoin(website, p) for p in EXTRA_PATHS][: max(0, 3 - len(cands))]
    for url in cands:
        r = await f.fetch(url)
        if not r.ok:
            continue
        final = r.final_url or url
        if not re.search(r"pricing|plans?|price|subscri", urlsplit(final).path, re.I):
            continue                                              # redirected away from a pricing page
        body = page_text(r.text)[:60000]
        if len(body) < 200:
            continue
        label, evidence = classify(body)
        if label:
            return key, st, "ok", {"url": final, "text": body, "label": label, "evidence": evidence,
                                   "method": "rules+discover"}
        if orch is None:
            return key, st, "discover_unclassified", None
        res = await orch.extract(PROMPT, body, make_validator(body), focus=FOCUS)
        if res.data is None:
            return key, st, "discover_deferred", None             # every tier busy: retry later
        if res.data.get("pricing_model"):
            return key, st, "ok", {"url": final, "text": body, "label": res.data["pricing_model"],
                                   "evidence": exact_slices(res.data["evidence"], body),
                                   "method": f"llm+discover:{res.tier}"}
        return key, st, "discover_unclear", None
    return key, st, "discover_none", None


async def safe_process(f, orch, key, st):
    try:
        return await process(f, orch, key, st)
    except Exception as exc:
        log.warning("discover failed for %s: %s", key, str(exc)[:120])
        return key, st, "discover_failed", None


async def run(db: Database, fetcher: Fetcher, orch: "LLMOrchestrator | None" = None, limit: int = 500) -> None:
    startups = {squash(normalize(r["content"]["entityName"])): r for r in await db.fetch_records("STARTUP")}
    cur = await db.conn.execute("SELECT key FROM crawl_state WHERE source='products' AND value='no_pricing_page'")
    keys = [k for (k,) in await cur.fetchall() if k in startups]
    keys.sort(key=lambda k: (not startups[k]["content"]["data"].get("aiRelated"), k))
    keys = keys[:limit]
    print(f"startups with no pricing page found so far: to retry with sitemap/extra paths: {len(keys)} (limit {limit})")

    outcomes: Counter = Counter()
    new_rows: list[dict] = []
    for i in range(0, len(keys), 8):
        results = await asyncio.gather(*[safe_process(fetcher, orch, k, startups[k]) for k in keys[i:i + 8]])
        for key, st, outcome, cand in results:
            if outcome == "ok":
                body, url, name = cand["text"], cand["url"], st["content"]["entityName"]
                if not all(e in body for e in cand["evidence"]):
                    outcome = "discover_evidence_check_failed"
                elif not mentions_company(name, body, url):
                    outcome = "discover_company_not_on_page"
                else:
                    try:
                        rec = ProductRecord(
                            source=Source(name=SOURCE_NAME, url=url), collectedAt=utc_now_iso(),
                            content=ProductContent(
                                startupName=name, pricingModel=cand["label"],
                                description=st["content"]["data"].get("description"), pricingUrl=url,
                                evidence=cand["evidence"], method=cand["method"]))
                    except ValidationError:
                        outcome = "discover_invalid_record"
                    else:
                        if await db.insert_record("PRODUCT", key, SOURCE_NAME, url, rec.model_dump()):
                            outcome = "discover_ok"
                            await db.save_raw_page(url, url, 200, body)
                            new_rows.append({"name": name, "model": cand["label"], "url": url,
                                             "method": cand["method"], "evidence": cand["evidence"]})
                        else:
                            outcome = "discover_duplicate"
            outcomes[outcome] += 1
            if outcome != "discover_deferred":                    # deferred: leave the state alone, retry later
                await db.set_state("products", key, outcome)
        print(f"  processed {min(i + 8, len(keys))}/{len(keys)}   recovered so far {outcomes['discover_ok']}")

    print(f"\nrecovered by discovery: {outcomes['discover_ok']}   total PRODUCT records: {await db.count_records('PRODUCT')}")
    print("outcomes:")
    for k, v in outcomes.most_common():
        print(f"  {k:40} {v}")
    print("\n5 recovered products to verify by hand:")
    for r in random.sample(new_rows, min(5, len(new_rows))):
        print(f"- {r['name']}: {r['model']} ({r['method']})\n  {r['url']}\n  evidence: {r['evidence']}")