import asyncio
import json
import random
import re
from collections import Counter
from urllib.parse import urljoin, urlsplit

import trafilatura

from src.fetcher import Fetcher
from src.log import setup_logging

PRICE = re.compile(r"[$€£₹]\s?\d[\d,.]*|\d\s?(?:usd\b|/mo\b|/month|per month|per user|/year|per year)", re.I)
FREE = re.compile(r"free plan|free tier|free forever|forever free|always free|\$\s?0\b|freemium", re.I)
ENT = re.compile(r"contact sales|talk to sales|custom pricing|book a demo|request a demo|enterprise", re.I)
LINK = re.compile(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
N = 60


def host(u: str) -> str:
    h = urlsplit(u).hostname or ""
    return h[4:] if h.startswith("www.") else h


def same_site(a: str, b: str) -> bool:
    ha, hb = host(a), host(b)
    return ha == hb or ha.endswith("." + hb)


def find_pricing_link(html: str, base: str) -> str | None:
    for href, text in LINK.findall(html):
        if "pricing" in href.lower() or "pricing" in re.sub(r"<[^>]+>", "", text).lower():
            full = urljoin(base, href.strip())
            if full.startswith("http") and same_site(full, base):
                return full
    return None


def classify(text: str):
    price, free, ent = PRICE.search(text), FREE.search(text), ENT.search(text)
    if free and (price or ent):
        return "FREEMIUM", free.group(0)
    if free:
        return "FREE", free.group(0)
    if price:
        return "PAID", price.group(0)
    if ent:
        return "ENTERPRISE", ent.group(0)
    return None, None


async def probe(f: Fetcher, s: dict, stats: Counter, samples: list):
    site = s["website"]
    r = await f.fetch(site)
    if not r.ok:
        stats[f"homepage_failed:{r.error}"] += 1
        return
    base = r.final_url or site
    link = find_pricing_link(r.text, base)
    if link:
        p = await f.fetch(link)
    else:
        link = urljoin(base, "/pricing")
        p = await f.fetch(link)
    if not p.ok:
        stats["no_pricing_page" if not find_pricing_link(r.text, base) else "pricing_fetch_failed"] += 1
        return
    text = trafilatura.extract(p.text, favor_recall=True) or ""
    if len(text) < 200:
        stats["pricing_page_empty_or_js"] += 1
        return
    label, evidence = classify(text)
    if not label:
        stats["unclassified"] += 1
        return
    stats[label] += 1
    samples.append((s["name"], link, label, evidence))


async def main():
    setup_logging("ERROR")
    startups = json.load(open("data/yc_ai_startups.json", encoding="utf-8"))
    pool = [c for c in startups if c.get("website") and c.get("status") == "Active"]
    sample = random.Random(1).sample(pool, min(N, len(pool)))
    stats: Counter = Counter()
    samples: list = []
    async with Fetcher(max_concurrency=10, per_domain_interval=1.0) as f:
        await asyncio.gather(*[probe(f, s, stats, samples) for s in sample])

    usable = sum(stats[k] for k in ("FREE", "FREEMIUM", "PAID", "ENTERPRISE"))
    print(f"\nsites probed: {len(sample)}   usable pricing results: {usable} ({100 * usable / len(sample):.0f}%)")
    for k, v in stats.most_common():
        print(f"  {k:45} {v}")
    print("\nsamples (check these by opening the page!):")
    for name, link, label, ev in samples[:10]:
        print(f"- {name}: {label}  evidence={ev!r}\n  {link}")


asyncio.run(main())