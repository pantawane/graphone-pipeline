import asyncio, aiohttp, pathlib, json, trafilatura

UA = "FrontierAtlasDemoBot/0.1 (contact: your-email@example.com)"
OUT = pathlib.Path("data/test_pages")
OUT.mkdir(parents=True, exist_ok=True)

async def grab(session, url, sem):
    async with sem:
        try:
            async with session.get(url, headers={"User-Agent": UA}) as r:
                if r.status != 200:
                    return url, None, f"status {r.status}"
                html = await r.text(errors="ignore")
        except Exception as e:
            return url, None, type(e).__name__
    text = await asyncio.to_thread(trafilatura.extract, html, favor_recall=True)
    if not text or len(text) < 500:
        return url, None, "too little text"
    return url, text, "ok"

async def main():
    urls = [u.strip() for u in open("urls.txt") if u.strip() and not u.startswith("#")]
    sem = asyncio.Semaphore(5)
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        results = await asyncio.gather(*[grab(s, u, sem) for u in urls])
    pages = []
    for url, text, status in results:
        print(f"{status:18} {url}")
        if text:
            pages.append({"url": url, "text": text})
    # json.dump(pages, open(OUT / "pages.json", "w"), ensure_ascii=False, indent=1)
    with open(OUT / "pages.json", "w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=1)
    print(f"\nsaved {len(pages)} usable pages")

asyncio.run(main())