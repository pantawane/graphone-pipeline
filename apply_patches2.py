"""Round 2 patches. Backs up every changed file to backup_before_patch2/ and skips what is already applied."""
import shutil
import sys
from pathlib import Path

BACKUP = Path("backup_before_patch2")

NEW_CHECK_URLS = '''async def check_urls(urls: list[str]):
    """Patient on purpose: a flaky connection must not look like a data problem."""
    ua = os.getenv("USER_AGENT", "FrontierAtlasDemoBot/0.1")
    sem = asyncio.Semaphore(3)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45), headers={"User-Agent": ua}) as s:
        async def one(u):
            async with sem:
                last = "unknown"
                for attempt in range(3):
                    try:
                        async with s.get(u, allow_redirects=True) as r:
                            return u, r.status
                    except Exception as e:
                        last = type(e).__name__
                        await asyncio.sleep(2 * (attempt + 1))
                return u, last
        return await asyncio.gather(*[one(u) for u in urls])


'''

NEW_URL_BLOCK = '''    if "--check-urls" in sys.argv:
        print("\\n== 7. do the sampled URLs resolve?")
        for u, status in asyncio.run(check_urls(urls)):
            if isinstance(status, int) and status < 400:
                note = ""
            elif status in (403, 429, 999):
                note = "   <- blocked for bots: open it in a browser"
            elif status in (404, 410):
                note = "   <- PROBLEM (page not found)"
                problems.append(f"sampled URL not found: {u}")
            else:
                note = "   <- could not check from this network: open it in a browser"
            print(f"   {status!s:>6} {u}{note}")

'''

SCRIPTS_TABLE = '''## Diagnostic and maintenance scripts

| Script | Purpose |
|---|---|
| `status.py` | counts of records, product outcomes, news/job sources and LLM usage |
| `audit_final.py` | schema, URL and evidence checks plus a 30-row manual sample |
| `revalidate_products.py`, `revalidate_llm.py` | re-check stored products against their saved pages with the current rules |
| `clean_jobs.py`, `clean_news.py` | remove stored items that no longer pass the current filters |
| `run_jobs.py --peek / --boards`, `run_render.py --demo` | inspect real source fields and static vs rendered pages |
| `spike_*.py`, `compare_models.py`, `time_gemma.py`, `fix_*.py`, `check_*.py`, `inspect_pwc.py`, `filter_pwc.py` | one-off experiments that informed source and model choices (kept for transparency) |

'''

ENV_EXAMPLE = """GEMINI_API_KEY=
GROQ_API_KEY=
GITHUB_TOKEN=
USER_AGENT=FrontierAtlasDemoBot/0.1 (contact: your-email@example.com)
"""

PATCHES = [
    {"file": "audit_final.py", "name": "audit: patient URL checker", "kind": "between",
     "start": "async def check_urls(urls: list[str]):", "end": "def main() -> int:",
     "new": NEW_CHECK_URLS, "done": "sem = asyncio.Semaphore(3)"},
    {"file": "audit_final.py", "name": "audit: only 404/410 count as problems", "kind": "between",
     "start": '    if "--check-urls" in sys.argv:', "end": r'    print("\nPROBLEMS:" if problems',
     "new": NEW_URL_BLOCK, "done": "could not check from this network"},
    {"file": "make_docs.py", "name": "README: diagnostic scripts table", "kind": "insert_before",
     "anchor": "## Known limitations\n\nSee section 8", "new": SCRIPTS_TABLE,
     "done": "## Diagnostic and maintenance scripts"},
    {"file": "make_docs.py", "name": "README: honest name for the AI flag", "kind": "replace",
     "old": "STARTUP records (YC directory, `aiRelated` flag)",
     "new": "STARTUP records (YC directory; `yc_ai_tag` = tagged AI by YC)", "done": "yc_ai_tag"},
    {"file": "export_sheet.py", "name": "export: yc_ai_tag column", "kind": "replace",
     "old": '"aiRelated": data.get("aiRelated"),', "new": '"yc_ai_tag": data.get("aiRelated"),',
     "done": '"yc_ai_tag"'},
]


def apply(p: dict, text: str):
    if p["done"] in text:
        return text, "already applied"
    if p["kind"] == "replace":
        n = text.count(p["old"])
        return (text.replace(p["old"], p["new"]), "applied") if n == 1 else (text, f"NOT FOUND (matched {n} times)")
    if p["kind"] == "insert_before":
        n = text.count(p["anchor"])
        return (text.replace(p["anchor"], p["new"] + p["anchor"]), "applied") if n == 1 else (text, f"NOT FOUND (matched {n} times)")
    s = text.find(p["start"])
    e = text.find(p["end"], s + 1) if s != -1 else -1
    if s == -1 or e == -1:
        return text, "NOT FOUND (marker missing)"
    return text[:s] + p["new"] + text[e:], "applied"


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
    env = Path(".env.example")
    if not env.exists():
        env.write_text(ENV_EXAMPLE, encoding="utf-8")
        print("\n.env.example: created")
    print("\nall patches applied or already present" if not failed else f"\n{failed} patch(es) need a manual edit")
    return 1 if failed else 0


sys.exit(main())