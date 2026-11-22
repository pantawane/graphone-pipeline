"""Step 8B: recover 'pricing_page_unclassified' startups with the LLM chain.
A record is created only if the quotes exist in the page and the label is consistent with them."""
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
from src.sources.products import PAID_RE, SOURCE_NAME, mentions_company, page_text, pricing_candidates

log = logging.getLogger("products_llm")

PROMPT = """You classify the pricing model of a software product from its pricing page text.
Use ONLY the text between <page> tags. Never guess; if the page does not make it clear, answer null.

Definitions:
- FREE: the product is entirely free to use (no paid plans shown).
- FREEMIUM: a free plan or free tier exists AND paid plans or a contact-sales tier also exist.
- PAID: only paid plans exist (a free trial alone does not make it free).
- ENTERPRISE: no public prices at all; access is by contacting sales or requesting a quote.

Return ONLY one JSON object:
{"pricing_model": "FREE" | "FREEMIUM" | "PAID" | "ENTERPRISE" | null, "evidence": ["exact quote copied from the page", "..."]}
Rules for evidence: 1 to 3 quotes, each copied EXACTLY, character for character, from the page (8 to 25 words), no ellipses, no paraphrasing. Use an empty list when the answer is null.

<page>
{text}
</page>"""

FOCUS = re.compile(
    r"[$€£₹]\s?\d|\bfree\b|\bplans?\b|\bpricing\b|\bper (?:month|user|seat|year)\b|\bcontact sales\b|\bcredits?\b|\benterprise\b",
    re.I,
)
PRICE_WORDS = re.compile(r"pricing|plans?|per (?:month|user|seat|year)|/mo\b|\bfree\b|contact sales|credits?", re.I)
FREEISH = re.compile(r"\bfree\b|(?<![\w.])[$€£₹]\s?0(?![\d.,])|open[- ]source", re.I)
PAIDISH = re.compile(r"[$€£₹]\s?[1-9]|\bcontact\b|\bcustom\b|\benterprise\b|\bupgrade\b|\bpaid\b|\bpro\b|\bplus\b|\bteam\b|\bbusiness\b", re.I)
PRICEISH = re.compile(r"[$€£₹]\s?\d|\b\d[\d,.]*\s?(?:usd|eur|inr)\b|\bcredits?\b|\bper (?:month|user|seat|year)\b", re.I)
SALESISH = re.compile(
    r"\bcontact (?:our )?sales\b|\btalk to (?:our |a )?sales\b|\bcustom (?:pricing|quote|proposal|plans?)\b"
    r"|\b(?:get|request) (?:a )?quote\b|\bwe(?:'|’)?ll (?:scope|quote)\b|\bwe quote\b"
    r"|\bcontact us (?:for|to get) (?:a )?(?:pricing|quote)\b|\benterprise pricing\b",
    re.I,
)

def _norm_quote(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip())


def make_validator(text: str):
    """Returns validate(data) -> None if acceptable, else a reason. null (unclear) is acceptable."""
    has_price_on_page = PAID_RE.search(text) is not None

    def validate(d: dict):
        model = d.get("pricing_model")
        if model is None:
            return None
        if model not in ("FREE", "FREEMIUM", "PAID", "ENTERPRISE"):
            return "bad_enum"
        ev = d.get("evidence")
        if not isinstance(ev, list) or not 1 <= len(ev) <= 4:
            return "bad_evidence_list"
        for q in ev:
            if not isinstance(q, str) or len(q.strip()) < 8 or len(q) > 300:
                return "bad_quote"
            if not re.search(re.escape(_norm_quote(q)), text, re.I):
                return "evidence_not_in_page"
        joined = " ".join(ev)
        if model == "PAID" and not PRICEISH.search(joined):
            return "paid_without_price_evidence"
        if model == "FREEMIUM" and not (FREEISH.search(joined) and PAIDISH.search(joined)):
            return "freemium_evidence_incomplete"
        if model == "FREE" and (not FREEISH.search(joined) or has_price_on_page):
            return "free_inconsistent"
        if model == "ENTERPRISE" and (not SALESISH.search(joined) or has_price_on_page):
            return "enterprise_inconsistent"
        return None

    return validate


def exact_slices(evidence: list[str], text: str) -> list[str]:
    """Replace the model's wording by the exact text found on the page."""
    return [re.search(re.escape(_norm_quote(q)), text, re.I).group(0) for q in evidence]


async def find_pricing_text(f: Fetcher, website: str):
    home = await f.fetch(website)
    if not home.ok:
        return None
    base = home.final_url or website
    fallback = None
    for url, _linked in pricing_candidates(home.text, base):
        r = await f.fetch(url)
        if not r.ok:
            continue
        final = r.final_url or url
        if urlsplit(final).path.strip("/") == "":          # redirected to the homepage: not a pricing page
            continue
        text = page_text(r.text)
        if len(text) < 200:
            continue
        if PRICE_WORDS.search(text):
            return final, text
        fallback = fallback or (final, text)
    return fallback

async def process(fetcher: Fetcher, orch: LLMOrchestrator, key: str, st: dict):
    found = await find_pricing_text(fetcher, st["content"]["data"]["website"])
    if not found:
        return key, st, "llm_no_text", None, None
    url, text = found
    text = text[:60000]
    res = await orch.extract(PROMPT, text, make_validator(text), focus=FOCUS)
    if res.data is None:
        bad = sum(1 for e in res.events if ":invalid" in e or ":bad_json" in e)
        return key, st, ("llm_rejected" if bad >= 2 else "llm_deferred"), None, res
    if res.data.get("pricing_model") is None:
        return key, st, "llm_unclear", None, res
    return key, st, "candidate", {"url": url, "text": text, "data": res.data, "tier": res.tier}, res


async def run(db: Database, fetcher: Fetcher, orch: LLMOrchestrator, limit: int = 300, chunk: int = 30) -> None:
    startups = {squash(normalize(r["content"]["entityName"])): r for r in await db.fetch_records("STARTUP")}
    cur = await db.conn.execute(
        "SELECT key FROM crawl_state WHERE source='products' AND value='pricing_page_unclassified'")
    keys = [k for (k,) in await cur.fetchall() if k in startups]
    keys.sort(key=lambda k: (not startups[k]["content"]["data"].get("aiRelated"), k))
    keys = keys[:limit]
    print(f"unclassified startups to process: {len(keys)} (limit {limit})")

    outcomes: Counter = Counter()
    new_rows: list[dict] = []

    async def handle(batch: list[str]) -> list[str]:
        """Processes a batch. Returns the keys that were deferred (every tier busy or failing)."""
        deferred: list[str] = []
        results = await asyncio.gather(*[process(fetcher, orch, k, startups[k]) for k in batch])
        for key, st, outcome, cand, _res in results:
            if outcome == "candidate":
                text, url, data = cand["text"], cand["url"], cand["data"]
                name = st["content"]["entityName"]
                if not mentions_company(name, text, url):
                    outcome = "llm_company_not_on_page"
                else:
                    try:
                        rec = ProductRecord(
                            source=Source(name=SOURCE_NAME, url=url),
                            collectedAt=utc_now_iso(),
                            content=ProductContent(
                                startupName=name, pricingModel=data["pricing_model"],
                                description=st["content"]["data"].get("description"),
                                pricingUrl=url, evidence=exact_slices(data["evidence"], text),
                                method=f"llm:{cand['tier']}"),
                        )
                    except ValidationError as e:
                        outcome = "llm_invalid_record"
                        log.warning("invalid LLM product %s: %s", name, str(e)[:120])
                    else:
                        if await db.insert_record("PRODUCT", key, SOURCE_NAME, url, rec.model_dump()):
                            outcome = "llm_ok"
                            new_rows.append({"name": name, "model": data["pricing_model"], "url": url,
                                             "tier": cand["tier"], "evidence": rec.content.evidence})
                            await db.save_raw_page(url, url, 200, text)
                        else:
                            outcome = "llm_duplicate"
            if outcome == "llm_deferred":
                deferred.append(key)                              # try again later, state unchanged
                continue
            outcomes[outcome] += 1
            await db.set_state("products", key, outcome)
        return deferred

    pending = keys
    done = 0
    for pass_no in (1, 2):
        deferred_all: list[str] = []
        for i in range(0, len(pending), chunk):
            if orch.all_unavailable():
                print("all LLM tiers are out of quota or disabled: stopping, resume later")
                break
            deferred_all += await handle(pending[i:i + chunk])
            done += len(pending[i:i + chunk])
            print(f"  pass {pass_no}: processed {min(i + chunk, len(pending))}/{len(pending)}   "
                  f"recovered so far {outcomes['llm_ok']}")
        if not deferred_all or pass_no == 2 or orch.all_unavailable():
            outcomes["llm_deferred"] += len(deferred_all)
            break
        print(f"{len(deferred_all)} deferred (rate limits or capacity): waiting 70s, then one retry pass")
        await asyncio.sleep(70)
        pending = deferred_all

    total = await db.count_records("PRODUCT")
    print(f"\nrecovered by LLM this run: {outcomes['llm_ok']}   total PRODUCT records: {total}")
    print("outcomes:")
    for k, v in outcomes.most_common():
        print(f"  {k:30} {v}")
    cur = await db.conn.execute(
        "SELECT COALESCE(json_extract(data_json,'$.content.method'),'rules'), COUNT(*) FROM records "
        "WHERE record_type='PRODUCT' GROUP BY 1 ORDER BY 2 DESC")
    print("products by method:", dict(await cur.fetchall()))
    print("\n" + orch.summary())
    print("\n5 recovered products to verify by hand:")
    for r in random.sample(new_rows, min(5, len(new_rows))):
        print(f"- {r['name']}: {r['model']} (by {r['tier']})\n  {r['url']}\n  evidence: {r['evidence']}")