"""Applies the pending code updates safely: backs up every changed file into backup_before_patch/,
skips patches that are already applied, and reports the ones that need a manual edit."""
import shutil
import sys
from pathlib import Path

BACKUP = Path("backup_before_patch")
JOBS, NEWS, FETCHER = "src/sources/jobs.py", "src/sources/news.py", "src/fetcher.py"

AI_NEWS_BLOCK = r'''AI_NEWS_RE = re.compile(
    r"\b(?:ai|a\.i\.|artificial intelligence|llms?|gpt|chatgpt|openai|anthropic|claude|gemini|machine learning|"
    r"deep learning|neural|generative|genai|agents?|agentic|diffusion|transformers?|copilot|mistral|deepseek)\b",
    re.I)


def is_ai_article(title: str, text: str) -> bool:
    return bool(AI_NEWS_RE.search(title)) or len(AI_NEWS_RE.findall(text[:3000])) >= 3


'''

NEW_PARSE_HN = r'''def parse_hn_html(html: str, base: str, now: datetime) -> list[dict]:
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


'''

AI_FILTER_STEP = '''    if src.get("ai_filter") and not is_ai_article(item["title"], text):
        stats["not_ai"] += 1
        return

'''

PATCHES = [
    # ---- jobs
    {"file": JOBS, "name": "jobs: remote flag from Greenhouse 'Location Type' metadata", "kind": "replace",
     "old": '    return _item(company, title, url, pd, loc, detail=("greenhouse", token, j.get("id")))',
     "new": ('    meta = j.get("metadata") or []\n'
             '    loc_type = next((m.get("value") for m in meta if isinstance(m, dict) and m.get("name") == "Location Type"), None)\n'
             '    remote = None if loc_type is None else ("remote" in str(loc_type).lower())\n'
             '    return _item(company, title, url, pd, loc, remote, detail=("greenhouse", token, j.get("id")))'),
     "done": "loc_type"},
    {"file": JOBS, "name": "jobs: AI filter on the title only", "kind": "replace",
     "old": 'src["ai_filter"] and not is_ai_job(item["title"], item["tags"])',
     "new": 'src["ai_filter"] and not is_ai_job(item["title"])',
     "done": 'is_ai_job(item["title"]):'},
    {"file": JOBS, "name": "jobs: German engineering titles", "kind": "replace",
     "old": 'machine learning|platform"),', "new": 'machine learning|platform|entwickler|ingenieur|programmierer"),',
     "done": "entwickler"},
    {"file": JOBS, "name": "jobs: remove unverified boards (magic, gradient)", "kind": "replace",
     "old": '"magic": "Magic", "gradient": "Gradient",', "new": "", "absent": '"magic": "Magic"'},
    # ---- news
    {"file": NEWS, "name": "news: AI relevance filter function", "kind": "insert_before",
     "anchor": "def parse_hn_html(", "new": AI_NEWS_BLOCK, "done": "def is_ai_article"},
    {"file": NEWS, "name": "news: ai_filter flag on Hacker News API source", "kind": "replace",
     "old": '{"name": "Hacker News (AI)", "kind": "hn_api",',
     "new": '{"name": "Hacker News (AI)", "kind": "hn_api", "ai_filter": True,',
     "done": '"kind": "hn_api", "ai_filter": True'},
    {"file": NEWS, "name": "news: ai_filter flag on Hacker News HTML source", "kind": "replace",
     "old": '"kind": "hn_html", "url":', "new": '"kind": "hn_html", "ai_filter": True, "url":',
     "done": '"kind": "hn_html", "ai_filter": True'},
    {"file": NEWS, "name": "news: HN relative time is an 'inferred' fallback date", "kind": "between",
     "start": "def parse_hn_html(", "end": "async def collect_rss(", "new": NEW_PARSE_HN,
     "done": '"inferred" if p.precision == "exact" else p.precision'},
    {"file": NEWS, "name": "news: apply the AI filter before dates are resolved", "kind": "insert_before",
     "anchor": "    if pd is None and html:", "new": AI_FILTER_STEP,
     "done": 'if src.get("ai_filter") and not is_ai_article'},
    # ---- fetcher
    {"file": FETCHER, "name": "fetcher: NO_RETRY constant", "kind": "replace",
     "old": "RETRY_STATUS = {429, 500, 502, 503, 504}",
     "new": ("RETRY_STATUS = {429, 500, 502, 503, 504}\n"
             "NO_RETRY = (aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError,\n"
             "            aiohttp.ClientConnectorDNSError)"),
     "done": "NO_RETRY = ("},
    {"file": FETCHER, "name": "fetcher: do not retry certificate / TLS / DNS failures", "kind": "replace",
     "old": "                if attempt < self.max_retries:",
     "new": "                if attempt < self.max_retries and not isinstance(e, NO_RETRY):",
     "done": "not isinstance(e, NO_RETRY)"},
]


def apply(p: dict, text: str):
    if "done" in p and p["done"] in text:
        return text, "already applied"
    if "absent" in p and p["absent"] not in text:
        return text, "already applied"
    if p["kind"] == "replace":
        n = text.count(p["old"])
        return (text.replace(p["old"], p["new"]), "applied") if n == 1 else (text, f"NOT FOUND (matched {n} times)")
    if p["kind"] == "insert_before":
        n = text.count(p["anchor"])
        return (text.replace(p["anchor"], p["new"] + p["anchor"]), "applied") if n == 1 else (text, f"NOT FOUND (matched {n} times)")
    if p["kind"] == "between":
        s, e = text.find(p["start"]), text.find(p["end"])
        if s == -1 or e == -1 or e < s:
            return text, "NOT FOUND (start or end marker missing)"
        return text[:s] + p["new"] + text[e:], "applied"
    return text, "unknown patch kind"


def main() -> int:
    failed = 0
    for file in sorted({p["file"] for p in PATCHES}):
        path = Path(file)
        if not path.exists():
            print(f"{file}: FILE MISSING")
            failed += 1
            continue
        original = text = path.read_text(encoding="utf-8")
        print(f"\n{file}")
        for p in [p for p in PATCHES if p["file"] == file]:
            text, status = apply(p, text)
            print(f"  [{status:>15}] {p['name']}")
            failed += status.startswith("NOT FOUND")
        if text != original:
            BACKUP.mkdir(exist_ok=True)
            shutil.copy(path, BACKUP / path.name)
            path.write_text(text, encoding="utf-8")
    print("\nall patches applied or already present" if not failed else f"\n{failed} patch(es) need a manual edit (see above)")
    return 1 if failed else 0


sys.exit(main())