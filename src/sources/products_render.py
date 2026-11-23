"""Step 9 (Phase V): recover pricing pages that only exist after JavaScript runs ('pricing_page_js_or_empty')."""
import asyncio
import logging
import random
import re
from collections import Counter
from urllib.parse import urlsplit

from pydantic import ValidationError

from src.db import Database, utc_now_iso
from src.fetcher import Fetcher
from src.llm import LLMOrchestrator
from src.resolver import normalize, squash
from src.schemas import ProductContent, ProductRecord, Source
from src.sources.products import SOURCE_NAME, classify, mentions_company, page_text, pricing_candidates
from src.sources.products_llm import FOCUS, PROMPT, exact_slices, make_validator
from src.sources.render import Renderer

log = logging.getLogger("products_render")
PRICING_PATH = re.compile(r"pricing|plans?|price", re.I)
DEMO_URLS = ["https://www.langchain.com", "https://www.perplexity.ai", "https://weaviate.io", "https://modal.com"]


async def demo(fetcher: Fetcher, renderer: Renderer) -> None:
    """Phase V demonstration: the same pages fetched statically and rendered with a browser."""
    print(f"{'url':34} {'static':>22} {'text':>6}   rendered                    text  blocked")
    for url in DEMO_URLS:
        s = await fetcher.fetch(url)
        static_len = len(page_text(s.text)) if s.ok else 0
        r = await renderer.render(url)
        rendered_len = len(page_text(r.html)) if r.ok else 0
        print(f"{url:34} {str(s.status or s.error)[:22]:>22} {static_len:>6}   "
              f"status={r.status:<4} {rendered_len:>6}  {r.blocked or r.error or '-'}")


async def process(fetcher: Fetcher, renderer: Renderer, orch, key: str, st: dict):
    website = st["content"]["data"]["website"]
    home = await fetcher.fetch(website)
    if not home.ok:
        return key, st, "render_homepage_failed", None
    base = home.final_url or website
    cands = pricing_candidates(home.text, base)
    ordered = [u for u, linked in cands if linked] + [u for u, linked in cands if not linked]
    for url in ordered[:2]:
        r = await renderer.render(url)
        if r.blocked:
            return key, st, f"render_blocked:{r.blocked}", None
        if not r.ok:
            continue
        final = r.final_url or url
        if not PRICING_PATH.search(urlsplit(final).path):
            continue                                              # redirected to the homepage
        body = page_text(r.html)[:60000]
        if len(body) < 200:
            continue
        label, evidence = classify(body)
        if label:
            return key, st, "ok", {"url": final, "text": body, "label": label, "evidence": evidence,
                                   "method": "rules+render"}
        if orch is not None:
            res = await orch.extract(PROMPT, body, make_validator(body), focus=FOCUS)
            if res.data and res.data.get("pricing_model"):
                return key, st, "ok", {"url": final, "text": body, "label": res.data["pricing_model"],
                                       "evidence": exact_slices(res.data["evidence"], body),
                                       "method": f"llm+render:{res.tier}"}
            return key, st, "render_unclear", None
        return key, st, "render_unclassified", None
    return key, st, "render_empty", None


async def safe_process(fetcher, renderer, orch, key, st):
    try:
        return await process(fetcher, renderer, orch, key, st)
    except Exception as exc:
        log.warning("render failed for %s: %s", key, str(exc)[:120])
        return key, st, "render_failed", None


async def run(db: Database, fetcher: Fetcher, renderer: Renderer, orch: "LLMOrchestrator | None" = None,
              limit: int = 150) -> None:
    startups = {squash(normalize(r["content"]["entityName"])): r for r in await db.fetch_records("STARTUP")}
    cur = await db.conn.execute(
        "SELECT key FROM crawl_state WHERE source='products' AND value='pricing_page_js_or_empty'")
    keys = [k for (k,) in await cur.fetchall() if k in startups][:limit]
    print(f"JavaScript-only pricing pages to render: {len(keys)} (limit {limit})")

    outcomes: Counter = Counter()
    new_rows: list[dict] = []
    for i in range(0, len(keys), 6):
        results = await asyncio.gather(*[safe_process(fetcher, renderer, orch, k, startups[k])
                                         for k in keys[i:i + 6]])
        for key, st, outcome, cand in results:
            if outcome == "ok":
                body, url, name = cand["text"], cand["url"], st["content"]["entityName"]
                if not all(e in body for e in cand["evidence"]):
                    outcome = "render_evidence_check_failed"
                elif not mentions_company(name, body, url):
                    outcome = "render_company_not_on_page"
                else:
                    try:
                        rec = ProductRecord(
                            source=Source(name=SOURCE_NAME, url=url), collectedAt=utc_now_iso(),
                            content=ProductContent(
                                startupName=name, pricingModel=cand["label"],
                                description=st["content"]["data"].get("description"), pricingUrl=url,
                                evidence=cand["evidence"], method=cand["method"]))
                    except ValidationError:
                        outcome = "render_invalid_record"
                    else:
                        if await db.insert_record("PRODUCT", key, SOURCE_NAME, url, rec.model_dump()):
                            outcome = "render_ok"
                            await db.save_raw_page(url, url, 200, body)
                            new_rows.append({"name": name, "model": cand["label"], "url": url,
                                             "method": cand["method"], "evidence": cand["evidence"]})
                        else:
                            outcome = "render_duplicate"
            outcomes[outcome] += 1
            await db.set_state("products", key, outcome)
        print(f"  processed {min(i + 6, len(keys))}/{len(keys)}   recovered so far {outcomes['render_ok']}")

    print(f"\nrecovered by rendering: {outcomes['render_ok']}   total PRODUCT records: {await db.count_records('PRODUCT')}")
    print("outcomes:")
    for k, v in outcomes.most_common():
        print(f"  {k:40} {v}")
    print("\n5 recovered products to verify by hand:")
    for r in random.sample(new_rows, min(5, len(new_rows))):
        print(f"- {r['name']}: {r['model']} ({r['method']})\n  {r['url']}\n  evidence: {r['evidence']}")