"""Source spike for news (Step 10) and jobs (Step 11):
which sources work, which have real dates, and how many items are fresh (last 24h)."""
import asyncio
import calendar
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser

from src.fetcher import Fetcher
from src.log import setup_logging

NOW = datetime.now(timezone.utc)
CUT = NOW - timedelta(hours=24)

NEWS_RSS = {
    "techcrunch_ai": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "venturebeat_ai": "https://venturebeat.com/category/ai/feed/",
    "verge_ai": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "mit_tech_review_ai": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "arstechnica_ai": "https://arstechnica.com/ai/feed/",
    "wired_ai": "https://www.wired.com/feed/tag/ai/latest/rss",
    "huggingface_blog": "https://huggingface.co/blog/feed.xml",
    "google_ai_blog": "https://blog.google/technology/ai/rss/",
    "openai_news": "https://openai.com/news/rss.xml",
}
NEWS_JSON = {"hackernews_ai": "https://hn.algolia.com/api/v1/search_by_date?tags=story&query=AI&hitsPerPage=100"}
JOB_RSS = {"weworkremotely": "https://weworkremotely.com/remote-jobs.rss"}
JOB_JSON = {
    "remoteok": "https://remoteok.com/api",
    "himalayas": "https://himalayas.app/jobs/api?limit=100",
    "arbeitnow": "https://www.arbeitnow.com/api/job-board-api",
}
RELATIVE_HTML = {"hn_newest_html": "https://news.ycombinator.com/newest"}

ATS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
    "lever": "https://api.lever.co/v0/postings/{t}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{t}",
}
TOKENS = [
    "openai", "anthropic", "mistral", "cohere", "huggingface", "perplexity", "stabilityai", "scaleai",
    "databricks", "runwayml", "elevenlabs", "replit", "cognition", "togetherai", "together", "groq",
    "cerebras", "sambanova", "wandb", "langchain", "llamaindex", "pinecone", "weaviate", "anyscale", "modal",
    "replicate", "lightningai", "ai21", "writer", "harvey", "lumalabs", "pika", "ideogram", "suno",
    "descript", "snorkelai", "labelbox", "fireworksai", "baseten", "character", "adept", "sierra", "glean",
    "jasper", "synthesia", "xai", "anysphere", "windsurf", "poolside", "magic", "reka", "essentialai",
    "inflectionai", "blackforestlabs", "deepgram", "assemblyai", "gradient", "brightai", "contextual",
]
DATE_KEYS = ("first_published", "publishedAt", "published_at", "createdAt", "created_at", "pubDate",
             "date", "epoch", "updated_at")
RELATIVE_RE = re.compile(r"\b\d+\s+(?:minute|hour|day)s?\s+ago\b", re.I)


def to_dt(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        v = float(v)
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, timezone.utc)
    s = str(v).strip()
    if s.isdigit():
        return to_dt(int(s))
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def find_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("jobs", "data", "hits", "results"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def date_stats(items):
    items = [i for i in items if isinstance(i, dict)]
    best = None
    for key in DATE_KEYS:
        n = sum(1 for i in items if to_dt(i.get(key)))
        if n and (best is None or n > best[1]):
            best = (key, n)
    if not best:
        return len(items), 0, 0, None
    key = best[0]
    fresh = sum(1 for i in items if (d := to_dt(i.get(key))) and d >= CUT)
    return len(items), best[1], fresh, key


async def probe_rss(f, name, url):
    r = await f.fetch(url)
    if not r.ok:
        return name, r.error or f"http_{r.status}", 0, 0, 0, None, ""
    feed = feedparser.parse(r.text)
    total, dated, fresh, full = len(feed.entries), 0, 0, 0
    for e in feed.entries:
        st = e.get("published_parsed") or e.get("updated_parsed")
        if st:
            dated += 1
            if datetime.fromtimestamp(calendar.timegm(st), timezone.utc) >= CUT:
                fresh += 1
        c = e.get("content")
        if c and len(c[0].get("value", "")) > 1500:
            full += 1
    return name, "ok", total, dated, fresh, "published", f"full_text_in_feed={full}/{total}"


async def probe_json(f, name, url, robots=True):
    r, data = await f.fetch_json(url, robots=robots)
    if data is None:
        return name, r.error or "no_json", 0, 0, 0, None, ""
    total, dated, fresh, key = date_stats(find_items(data))
    return name, "ok", total, dated, fresh, key, ""


async def probe_relative(f, name, url):
    r = await f.fetch(url)
    if not r.ok:
        return name, r.error or f"http_{r.status}", 0, 0, 0, None, ""
    n = len(RELATIVE_RE.findall(r.text))
    return name, "ok", n, n, 0, "relative text", "relative dates found in HTML"


async def probe_ats(f, ats, token):
    r, data = await f.fetch_json(ATS[ats].format(t=token), robots=False)
    if data is None:
        return ats, token, r.error or "no_json", 0, 0, 0, None
    total, dated, fresh, key = date_stats(find_items(data))
    return ats, token, "ok", total, dated, fresh, key


def line(row):
    name, status, total, dated, fresh, key, extra = row
    print(f"  {name:22} {status:26} items={total:<5} dated={dated:<5} fresh24h={fresh:<4} "
          f"{('key=' + key) if key else ''} {extra}")


async def main():
    setup_logging("ERROR")
    overrides = {"boards-api.greenhouse.io": 0.3, "api.lever.co": 0.3, "api.ashbyhq.com": 0.3,
                 "remoteok.com": 1.1, "hn.algolia.com": 0.5}
    async with Fetcher(max_concurrency=10, per_domain_interval=1.0, domain_overrides=overrides) as f:
        news = await asyncio.gather(
            *[probe_rss(f, n, u) for n, u in NEWS_RSS.items()],
            *[probe_json(f, n, u, robots=False) for n, u in NEWS_JSON.items()],
            *[probe_relative(f, n, u) for n, u in RELATIVE_HTML.items()])
        jobs = await asyncio.gather(
            *[probe_rss(f, n, u) for n, u in JOB_RSS.items()],
            *[probe_json(f, n, u, robots=(n != "remoteok")) for n, u in JOB_JSON.items()])
        ats_rows = await asyncio.gather(*[probe_ats(f, a, t) for t in TOKENS for a in ATS])

    print(f"\nrun time (UTC): {NOW:%Y-%m-%d %H:%M}   fresh = published after {CUT:%Y-%m-%d %H:%M}")
    print("\n== NEWS")
    for row in news:
        line(row)
    print("\n== JOB BOARDS")
    for row in jobs:
        line(row)

    print("\n== ATS BOARDS THAT ANSWERED (greenhouse / lever / ashby)")
    hits = [r for r in ats_rows if r[2] == "ok" and r[3] > 0]
    for ats, token, _s, total, dated, fresh, key in sorted(hits, key=lambda r: (r[0], -r[5])):
        print(f"  {ats:11} {token:18} jobs={total:<5} dated={dated:<5} fresh24h={fresh:<4} key={key}")
    for a in ATS:
        mine = [r for r in hits if r[0] == a]
        print(f"  -> {a}: {len(mine)} boards answered, {sum(r[3] for r in mine)} jobs, "
              f"{sum(r[5] for r in mine)} fresh in 24h, date keys: {sorted({r[6] for r in mine if r[6]})}")


asyncio.run(main())