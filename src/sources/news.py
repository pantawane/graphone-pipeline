"""News vertical (Step 10): fresh (<24h) AI articles with full text, normalized dates, cross-run dedupe,
and optional LLM company extraction that is verified against the text."""
import asyncio
import calendar
import logging
import random
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin

import feedparser
import trafilatura
from pydantic import ValidationError

from src.dates import ParsedDate, is_fresh, meta_dates, parse_datetime
from src.db import Database, url_hash, utc_now_iso
from src.fetcher import Fetcher
from src.llm import LLMOrchestrator
from src.resolver import EntityResolver
from src.schemas import NewsContent, NewsRecord, Source

log = logging.getLogger("news")
logging.getLogger("trafilatura").setLevel(logging.CRITICAL)

SOURCES = [
    {"name": "TechCrunch AI", "kind": "rss", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "The Verge AI", "kind": "rss", "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"},
    {"name": "Wired AI", "kind": "rss", "url": "https://www.wired.com/feed/tag/ai/latest/rss"},
    {"name": "Ars Technica AI", "kind": "rss", "url": "https://arstechnica.com/ai/feed/"},
    {"name": "Hacker News (AI)", "kind": "hn_api", "ai_filter": True,
     "url": "https://hn.algolia.com/api/v1/search_by_date?tags=story&query=AI&hitsPerPage=100"},
    # Same site as above, but read from HTML: demonstrates parsing the visible relative dates ("3 hours ago").
    {"name": "Hacker News /newest (relative dates)", "kind": "hn_html",  "ai_filter": True, "url": "https://news.ycombinator.com/newest"},
]

HN_BLOCK = re.compile(r'<tr class="athing[^"]*" id="(\d+)">')
HN_TITLE = re.compile(r'<span class="titleline"><a href="([^"]+)"[^>]*>(.*?)</a>', re.S)
HN_AGE = re.compile(r'<span class="age"[^>]*>\s*<a [^>]*>([^<]+)</a>', re.S)

NEWS_PROMPT = """Extract the names of AI companies, startups and labs that the article below is about or mentions prominently.
Use ONLY the text between <article> tags. Never add a company that is not written in the text.
Return ONLY one JSON object: {"companies": ["Name exactly as written in the text", ...]} (at most 8 names, empty list if none)
<article>
{text}
</article>"""


def html_to_text(html: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html or "")
    return re.sub(r"\s+", " ", unescape(re.sub(r"(?s)<[^>]+>", " ", s))).strip()


def make_company_validator(text: str):
    low = text.lower()

    def validate(d: dict):
        names = d.get("companies")
        if not isinstance(names, list) or len(names) > 8:
            return "bad_companies"
        for n in names:
            if not isinstance(n, str) or not 2 <= len(n) <= 60 or n.lower() not in low:
                return "company_not_in_text"
        return None

    return validate

AI_NEWS_RE = re.compile(
    r"\b(?:ai|a\.i\.|artificial intelligence|llms?|gpt|chatgpt|openai|anthropic|claude|gemini|machine learning|"
    r"deep learning|neural|generative|genai|agents?|agentic|diffusion|transformers?|copilot|mistral|deepseek)\b",
    re.I)


def is_ai_article(title: str, text: str) -> bool:
    return bool(AI_NEWS_RE.search(title)) or len(AI_NEWS_RE.findall(text[:3000])) >= 3


def parse_hn_html(html: str, base: str, now: datetime) -> list[dict]:
    parts = HN_BLOCK.split(html)                                  # [pre, id, chunk, id, chunk, ...]
    items = []
    for i in range(1, len(parts), 2):
        chunk = parts[i + 1]
        t = HN_TITLE.search(chunk)
        if not t:
            continue
        url = urljoin(base, unescape(t.group(1)))
        if "news.ycombinator.com" in url:
            continue                                              # Ask HN / internal links have no article
        a = HN_AGE.search(chunk)
        p = parse_datetime(a.group(1), now) if a else None
        # the visible relative text is when it was SUBMITTED to HN: only a flagged fallback, never the article's date
        fallback = ParsedDate(p.dt, "inferred" if p.precision == "exact" else p.precision, "relative_text") if p else None
        items.append({"title": html_to_text(t.group(2)), "url": url, "date": None,
                      "fallback_date": fallback, "feed_text": ""})
    return items


async def collect_rss(f: Fetcher, src: dict, now: datetime):
    r = await f.fetch(src["url"])
    if not r.ok:
        return [], r.error or f"http_{r.status}"
    items = []
    for e in feedparser.parse(r.text).entries:
        url, title = e.get("link"), " ".join((e.get("title") or "").split())
        if not url or not title:
            continue
        st = e.get("published_parsed") or e.get("updated_parsed")
        if st:
            pd = ParsedDate(datetime.fromtimestamp(calendar.timegm(st), timezone.utc), "exact", "rss:published")
        else:
            p = parse_datetime(e.get("published") or e.get("updated"), now)
            pd = ParsedDate(p.dt, p.precision, "rss:" + p.source) if p else None
        content = e.get("content")
        feed_html = content[0].get("value", "") if content else (e.get("summary") or "")
        items.append({"title": title, "url": url, "date": pd, "feed_text": html_to_text(feed_html)})
    return items, None


async def collect_hn_api(f: Fetcher, src: dict, now: datetime):
    r, data = await f.fetch_json(src["url"], robots=False)
    if not isinstance(data, dict):
        return [], r.error or "no_json"
    items = []
    for h in data.get("hits", []):
        url, title = h.get("url"), h.get("title")
        if not url or not title:
            continue
        sub = parse_datetime(h.get("created_at"), now)
        # HN gives the SUBMISSION time, not the article's own date: only a flagged fallback
        fallback = ParsedDate(sub.dt, "inferred", "hn_submission_time") if sub else None
        items.append({"title": title, "url": url, "date": None, "fallback_date": fallback, "feed_text": ""})
    return items, None


async def collect_hn_html(f: Fetcher, src: dict, now: datetime):
    r = await f.fetch(src["url"])
    if not r.ok:
        return [], r.error or f"http_{r.status}"
    return parse_hn_html(r.text, src["url"], now), None


COLLECTORS = {"rss": collect_rss, "hn_api": collect_hn_api, "hn_html": collect_hn_html}


async def fetch_article(f: Fetcher, url: str):
    r = await f.fetch(url)
    if not r.ok:
        return None, ""
    text = trafilatura.extract(r.text, include_comments=False, favor_recall=True) or ""
    return r.text, re.sub(r"\s+", " ", text).strip()


@dataclass
class Ctx:
    f: Fetcher
    db: Database
    now: datetime
    resolver: EntityResolver
    orch: "LLMOrchestrator | None"
    stats: Counter
    baseline: bool


async def extract_companies(ctx: Ctx, text: str) -> list[str]:
    res = await ctx.orch.extract(NEWS_PROMPT, text, make_company_validator(text))
    if res.data is None:
        ctx.stats["llm_unavailable"] += 1
        return []
    out: list[str] = []
    for name in res.data["companies"]:
        r = await ctx.resolver.resolve_and_log(ctx.db, name, "NEWS", allow_fuzzy=True)
        if r.canonical and r.canonical not in out:
            out.append(r.canonical)
    return out


async def process_item(ctx: Ctx, src: dict, item: dict) -> None:
    url, now, stats = item["url"], ctx.now, ctx.stats
    pd, fallback = item.get("date"), item.get("fallback_date")
    early = pd or fallback
    if early is not None and not is_fresh(early, now):
        stats["old"] += 1                                          # cheap rejection before any download
        return
    if not await ctx.db.claim_url(url, src["name"]):               # never process the same URL twice
        stats["seen_before"] += 1
        return
    item["_claimed"] = True

    text = item.get("feed_text") or ""
    html = None
    if len(text) < 1500:
        html, page = await fetch_article(ctx.f, url)
        if len(page) > len(text):
            text = page
    if len(text) < 500:
        await ctx.db.release_url(url)                              # paywall / blocked: allow a retry later
        stats["no_full_text"] += 1
        return

    if src.get("ai_filter") and not is_ai_article(item["title"], text):
        stats["not_ai"] += 1
        return

    if pd is None and html:
        cands = meta_dates(html, url)                              # JSON-LD, meta tags, <time>, URL pattern
        if cands:
            pd = cands[0]
    if pd is None:
        pd = fallback
    if pd is None:                                                 # no date anywhere: "new since last run" heuristic
        if not ctx.baseline:
            stats["baseline_skipped"] += 1                         # first run: remember it, do not emit
            return
        pd = ParsedDate(now, "inferred", "first_seen_heuristic")
    if not is_fresh(pd, now):
        stats["old_by_page_date"] += 1
        return

    companies = await extract_companies(ctx, text) if ctx.orch is not None else []
    try:
        rec = NewsRecord(
            source=Source(name=src["name"], url=url),
            collectedAt=utc_now_iso(),
            content=NewsContent(
                title=item["title"], url=url,
                published_date=pd.dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                date_source=pd.source, date_confidence=pd.precision,
                full_text=text[:45000], text_length=len(text), companies=companies),
        )
    except ValidationError as e:
        stats["invalid_record"] += 1
        log.warning("invalid news record %s: %s", url, str(e)[:120])
        return
    if await ctx.db.insert_record("NEWS", url_hash(url), src["name"], url, rec.model_dump()):
        stats["stored"] += 1
    else:
        stats["duplicate_record"] += 1


async def safe_process(ctx: Ctx, src: dict, item: dict) -> None:
    try:
        await process_item(ctx, src, item)
    except Exception as exc:
        ctx.stats["item_error"] += 1
        log.warning("item failed %s: %s", item.get("url"), str(exc)[:120])
        if item.get("_claimed"):
            await ctx.db.release_url(item["url"])


async def run(db: Database, fetcher: Fetcher, resolver: EntityResolver,
              orch: "LLMOrchestrator | None" = None, max_items: int = 60) -> None:
    now = datetime.now(timezone.utc)
    report: dict[str, Counter] = {}
    for src in SOURCES:
        stats: Counter = Counter()
        report[src["name"]] = stats
        try:
            items, err = await COLLECTORS[src["kind"]](fetcher, src, now)
        except Exception as exc:
            log.warning("collector crashed for %s: %s", src["name"], exc)
            stats["collector_crashed"] = 1
            continue
        if err:
            stats["source_error"] = 1
            log.warning("%s: %s", src["name"], err)
            continue
        stats["collected"] = len(items)
        baseline = await db.get_state("news", f"baseline:{src['name']}") is not None
        ctx = Ctx(fetcher, db, now, resolver, orch, stats, baseline)
        batch = items[:max_items]
        for i in range(0, len(batch), 8):
            await asyncio.gather(*[safe_process(ctx, src, it) for it in batch[i:i + 8]])
        await db.set_state("news", f"baseline:{src['name']}", utc_now_iso())

    print(f"\nrun time (UTC): {now:%Y-%m-%d %H:%M}")
    for name, st in report.items():
        print(f"  {name:40} " + "  ".join(f"{k}={v}" for k, v in sorted(st.items())))
    rows = await db.fetch_records("NEWS")
    print(f"\ntotal NEWS records in database: {len(rows)}")
    print("\n3 random articles to verify by hand:")
    for r in random.sample(rows, min(3, len(rows))):
        c = r["content"]
        print(f"- [{r['source_name']}] {c['title'][:90]}\n  published {c['published_date']} "
              f"({c['date_source']}, {c['date_confidence']})  text={c['text_length']} chars  "
              f"companies={c['companies']}\n  {c['url']}")