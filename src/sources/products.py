"""Products vertical: one PRODUCT record per startup, from its own pricing page.
Rule-based classification. Every record carries evidence snippets that must exist in the page text."""
import asyncio
import hashlib
import logging
import random
import re
from collections import Counter
from html import unescape
from urllib.parse import urljoin, urlsplit

import trafilatura
from pydantic import ValidationError

from src.db import Database, utc_now_iso
from src.fetcher import Fetcher
from src.resolver import normalize, squash
from src.schemas import ProductContent, ProductRecord, Source

log = logging.getLogger("products")
logging.getLogger("trafilatura").setLevel(logging.CRITICAL)

SOURCE_NAME = "Company pricing page"
LINK = re.compile(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
SKIP_LINK = re.compile(r"blog|news|press|docs|changelog|article|/post|careers|jobs", re.I)

UNIT = r"(?:mo(?:nth)?|yr|year|user|seat|member|credit|token|1k|1m|gb|request|call|employee|agent|project|app)"
FREE_RE = re.compile(
    r"\bfree (?:plan|tier|forever|version|account|for (?:individuals|personal|students|startups|small teams))\b"
    r"|\bforever free\b|\balways free\b|\bfreemium\b"
    r"|(?<![\w.])[$€£₹]\s?0(?![\d.,])",
    re.I,
)
PAID_RE = re.compile(
    rf"[$€£₹]\s?[1-9]\d*(?:[.,]\d+)*\s*(?:usd|eur)?\s*(?:/|per|a|each)\s*{UNIT}\b"
    r"|(?:starting at|starts at|from)\s*[$€£₹]\s?[1-9]\d*",
    re.I,
)
ENT_RE = re.compile(
    r"\bcontact (?:our )?sales\b|\btalk to (?:our |a )?sales\b|\bcustom pricing\b"
    r"|\brequest (?:a )?quote\b|\bcontact us for (?:a )?(?:pricing|quote)\b|\benterprise pricing\b",
    re.I,
)
UPSELL_RE = re.compile(r"\b(?:pro|premium|team|teams|business|enterprise|upgrade|paid|plus)\b", re.I)
NOT_A_PRICE_RE = re.compile(
    r"\b(?:wage|wages|salary|salaries|earn|earns|earning|earnings|per shift|paid out|payout|hourly rate|"
    r"compensation|job|jobs|raised|funding|valuation|revenue|arr)\b",
    re.I,
)
COMPARISON_RE = re.compile(r"\bvs\.?\b|\bversus\b|\bcompared to\b|\balternatives?\b|\bcompetitors?\b", re.I)
ALLOW_HOMEPAGE = False      # homepage text is weak evidence for a pricing model

def host(u: str) -> str:
    h = urlsplit(u).hostname or ""
    return h[4:] if h.startswith("www.") else h


def same_site(a: str, b: str) -> bool:
    ha, hb = host(a), host(b)
    return ha == hb or ha.endswith("." + hb)


def pricing_candidates(html: str, base: str) -> list[tuple[str, bool]]:
    """Up to 3 pages to try: (url, found_as_a_link_on_the_homepage)."""
    found: list[tuple[str, bool]] = []
    seen: set[str] = set()
    base_clean = base.split("#")[0].rstrip("/")
    for href, text in LINK.findall(html):
        h = href.strip()
        label = re.sub(r"<[^>]+>", " ", text).strip().lower()
        if h.startswith("#") or SKIP_LINK.search(h):
            continue
        if not (re.search(r"pricing|plans|price", h.lower()) or re.fullmatch(r"(?:plans? (?:&|and) )?(?:pricing|plans?)(?: (?:&|and) plans?)?", label)):
            continue
        full = urljoin(base, h).split("#")[0]
        if not full.startswith("http") or not same_site(full, base) or full.rstrip("/") == base_clean:
            continue
        if full not in seen:
            seen.add(full)
            found.append((full, True))
    for path in ("/pricing", "/plans"):
        guess = urljoin(base, path)
        if guess not in seen:
            seen.add(guess)
            found.append((guess, False))
    return found[:3]


def page_text(html: str) -> str:
    t = trafilatura.extract(html, favor_recall=True, include_tables=True) or ""
    if len(t) < 300:                                   # pricing tables are often dropped; strip tags instead
        s = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
        s = unescape(re.sub(r"(?s)<[^>]+>", " ", s))
        if len(s) > len(t):
            t = s
    return re.sub(r"\s+", " ", t).strip()


def snippet(text: str, m: re.Match, pad: int = 40) -> str:
    return text[max(0, m.start() - pad): min(len(text), m.end() + pad)].strip()[:200]


def first_price(text: str):
    for m in PAID_RE.finditer(text):
        window = text[max(0, m.start() - 80): m.end() + 80]
        if not NOT_A_PRICE_RE.search(window):
            return m
    return None


def classify(text: str, allow_enterprise: bool = True):
    """Returns (label, [evidence snippets]) or (None, reason)."""
    if len(COMPARISON_RE.findall(text)) >= 2:
        return None, "comparison_page"            # competitor prices must never be attributed to this company
    free = FREE_RE.search(text)
    paid = first_price(text)
    ent = ENT_RE.search(text) if allow_enterprise else None
    if free and (paid or ent):
        return "FREEMIUM", [snippet(text, free), snippet(text, paid or ent)]
    if free:
        if UPSELL_RE.search(text):
            return None, "free_only_with_upsell"      # ambiguous -> do not guess
        return "FREE", [snippet(text, free)]
    if paid:
        return "PAID", [snippet(text, paid)]
    if ent:
        return "ENTERPRISE", [snippet(text, ent)]
    return None, "no_signal"

GENERIC_NAME_WORDS = {"ai", "labs", "lab", "app", "inc", "technologies", "technology", "systems",
                      "software", "hq", "io", "group", "studio", "studios", "platform"}


def mentions_company(name: str, text: str, url: str) -> bool:
    """The page (or its domain) must mention the company, so redirects to unrelated sites are rejected."""
    squash_ = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    core = squash_("".join(t for t in normalize(name).split() if t not in GENERIC_NAME_WORDS))
    if len(core) < 3:
        return True                       # too short to test reliably
    return core in squash_(text) or core in squash_(urlsplit(url).hostname or "")

async def analyse_site(f: Fetcher, website: str):
    home = await f.fetch(website)
    if not home.ok:
        return "homepage_" + (home.error or "failed").split(":")[0], None
    base = home.final_url or website
    problems: set[str] = set()
    for url, linked in pricing_candidates(home.text, base):
        r = await f.fetch(url)
        if not r.ok:
            if linked:
                problems.add("fetch_failed")
            continue
        text = page_text(r.text)
        if len(text) < 200:
            problems.add("js_or_empty")
            continue
        label, ev = classify(text, allow_enterprise=False)
        if label:
            return "ok", {"label": label, "evidence": ev, "url": r.final_url or url, "text": text}
        problems.add("unclassified")
    text = page_text(home.text)
    if ALLOW_HOMEPAGE and len(text) >= 200 and re.search(r"\bpricing\b|\bplans?\b", text, re.I):
        label, ev = classify(text)
        if label:
            return "ok", {"label": label, "evidence": ev, "url": base, "text": text}
        problems.add("unclassified")
    if not problems:
        return "no_pricing_page", None
    if "unclassified" in problems:
        return "pricing_page_unclassified", None
    if "js_or_empty" in problems:
        return "pricing_page_js_or_empty", None
    return "pricing_fetch_failed", None


async def run(db: Database, fetcher: Fetcher, target: int = 1100, concurrency: int = 30,
              retry: bool = False) -> None:
    startups = await db.fetch_records("STARTUP")
    startups.sort(key=lambda r: (not r["content"]["data"].get("aiRelated"),
                                 hashlib.md5(r["content"]["entityName"].encode()).hexdigest()))
    existing = await db.existing_keys("PRODUCT")
    cur = await db.conn.execute("SELECT key, value FROM crawl_state WHERE source='products'")
    attempted = {k: v for k, v in await cur.fetchall()}

    pending = []
    for r in startups:
        key = squash(normalize(r["content"]["entityName"]))
        if key in existing or (key in attempted and not retry):
            continue
        pending.append((key, r))
    log.info("startups: %d, already products: %d, pending: %d", len(startups), len(existing), len(pending))

    sem = asyncio.Semaphore(concurrency)

    async def work(key, r):
        async with sem:
            outcome, res = await analyse_site(fetcher, r["content"]["data"]["website"])
            return key, r, outcome, res

    outcomes: Counter = Counter()
    inserted = processed = 0
    CHUNK = 120
    for i in range(0, len(pending), CHUNK):
        if len(existing) + inserted >= target:
            break
        results = await asyncio.gather(*[work(k, r) for k, r in pending[i:i + CHUNK]])
        for key, r, outcome, res in results:
            processed += 1
            if outcome == "ok":
                text = res["text"][:60000]
                if not all(e in text for e in res["evidence"]):
                    outcome = "evidence_check_failed"
                elif not mentions_company(r["content"]["entityName"], text, res["url"]):
                    outcome = "company_not_on_page"
                else:
                    try:
                        rec = ProductRecord(
                            source=Source(name=SOURCE_NAME, url=res["url"]),
                            collectedAt=utc_now_iso(),
                            content=ProductContent(
                                startupName=r["content"]["entityName"], pricingModel=res["label"],
                                description=r["content"]["data"].get("description"),
                                pricingUrl=res["url"], evidence=res["evidence"]),
                        )
                    except ValidationError as e:
                        outcome = "invalid_record"
                        log.warning("invalid product %s: %s", r["content"]["entityName"], str(e)[:120])
                    else:
                        if await db.insert_record("PRODUCT", key, SOURCE_NAME, res["url"], rec.model_dump()):
                            inserted += 1
                            await db.save_raw_page(res["url"], res["url"], 200, text)
            outcomes[outcome] += 1
            await db.set_state("products", key, outcome)
        print(f"  processed {processed}/{len(pending)}   new products {inserted}   total {len(existing) + inserted}")

    total = await db.count_records("PRODUCT")
    print(f"\nprocessed this run: {processed}   new products: {inserted}   total PRODUCT records: {total}")
    print("outcomes this run:")
    for k, v in outcomes.most_common():
        print(f"  {k:45} {v}")
    cur = await db.conn.execute(
        "SELECT json_extract(data_json,'$.content.pricingModel'), COUNT(*) FROM records "
        "WHERE record_type='PRODUCT' GROUP BY 1 ORDER BY 2 DESC")
    print("pricing models:", dict(await cur.fetchall()))
    rows = await db.fetch_records("PRODUCT")
    print("\n5 random products to verify by hand (open the pricing page and find the evidence!):")
    for r in random.sample(rows, min(5, len(rows))):
        c = r["content"]
        print(f"- {c['startupName']}: {c['pricingModel']}\n  {c['pricingUrl']}\n  evidence: {c['evidence']}")