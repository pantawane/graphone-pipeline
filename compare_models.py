import os, re, json, sys, time
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq

load_dotenv()
MAX_CHARS = 8000            # about 2K tokens of page text, keeps us under the TPM limits
N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
OUT_FILE = "data/compare_results.jsonl"      # CHANGE 1

PROMPT = """You extract facts from a company web page.
Use ONLY the text between <page> tags. If a fact is not explicitly stated, use null. Never guess.
Return ONLY one JSON object, no markdown, with these keys:
  company_name: string
  description: string, max 25 words
  pricing_model: one of FREE, FREEMIUM, PAID, ENTERPRISE, or null
  employee_count: integer or null
  evidence: object with keys company_name, pricing_model, employee_count. Each value is an exact
            short quote (max 12 words) copied from the page that supports that field, or null if that field is null.
<page>
PAGE_HERE
</page>"""

RETRIES = {"n": 0}
# CHANGE 3: errors worth retrying (checked against the lowercase error text)
RETRYABLE = ("429", "503", "resource_exhausted", "unavailable", "overloaded",
             "rate limit", "rate_limit", "too many requests")


def text_of(msg):
    c = msg.content
    if isinstance(c, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return c


def norm(s):
    return re.sub(r"\s+", " ", s).lower().strip()


def parse_json(raw):
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    return json.loads(re.search(r"\{.*\}", raw, re.S).group(0))


def check(d, text):
    t = norm(text)
    if d.get("company_name") is None:                  # CHANGE 4: model abstained, not a hallucination
        return "abstain_no_name"
    if not isinstance(d["company_name"], str) or norm(d["company_name"]) not in t:
        return "name_not_in_text"
    if d.get("pricing_model") not in (None, "FREE", "FREEMIUM", "PAID", "ENTERPRISE"):
        return "bad_enum"
    ev = d.get("evidence") or {}
    for k in ("company_name", "pricing_model", "employee_count"):
        if d.get(k) is not None:
            q = ev.get(k)
            if not q or norm(str(q)) not in t:
                return f"evidence_missing_{k}"
    ec = d.get("employee_count")
    if ec is not None and not (isinstance(ec, int) and str(ec) in text.replace(",", "")):
        return "employee_count_not_in_text"
    return "pass"


def call(model, prompt):
    for attempt in range(3):
        try:
            t0 = time.time()
            out = text_of(model.invoke(prompt))
            return out, None, time.time() - t0
        except Exception as e:
            s = str(e)
            if any(k in s.lower() for k in RETRYABLE):          # CHANGE 3
                RETRIES["n"] += 1
                time.sleep(10 * 2 ** attempt)                    # 10s, 20s, 40s
                continue
            return None, f"{type(e).__name__}: {s[:120]}", 0.0
    return None, "retryable_error_after_3_tries", 0.0


def load_pages():
    # CHANGE 2: works whether pages.json was saved as UTF-8 or Windows cp1252
    data = open("data/test_pages/pages.json", "rb").read()
    try:
        txt = data.decode("utf-8")
    except UnicodeDecodeError:
        txt = data.decode("cp1252", errors="replace")
    return json.loads(txt)


models = []
for mid in os.environ["GEMINI_MODEL_IDS"].split(","):
    mid = mid.strip()
    gap = 12 if "gemma" in mid.lower() else 5
    models.append((mid, ChatGoogleGenerativeAI(model=mid, temperature=0,
                   google_api_key=os.environ["GEMINI_API_KEY"]), gap))
for mid in os.environ["GROQ_MODEL_IDS"].split(","):
    mid = mid.strip()
    models.append((mid, ChatGroq(model=mid, temperature=0, max_tokens=1500), 25))

pages = load_pages()[:N]
open(OUT_FILE, "w", encoding="utf-8").close()      # CHANGE 1: start with an empty results file
report = {}

for label, model, gap in models:
    stats = {"valid_json": 0, "pass": 0, "abstain": 0, "errors": 0,
             "secs": 0.0, "ok_calls": 0, "retries": 0}
    RETRIES["n"] = 0
    print(f"\n=== {label}")
    for p in pages:
        text = p["text"][:MAX_CHARS]
        raw, err, call_secs = call(model, PROMPT.replace("PAGE_HERE", text))
        if err:
            stats["errors"] += 1
            verdict = f"ERROR {err}"
        else:
            stats["secs"] += call_secs
            stats["ok_calls"] += 1
            try:
                d = parse_json(raw)
                stats["valid_json"] += 1
                verdict = check(d, text)
                if verdict == "pass":
                    stats["pass"] += 1
                elif verdict.startswith("abstain"):
                    stats["abstain"] += 1
            except Exception as e:
                verdict = f"bad_json {type(e).__name__}"
        print(f"  {verdict:32} {call_secs:5.1f}s  {p['url']}")
        rec = {"model": label, "url": p["url"], "verdict": verdict, "secs": round(call_secs, 1), "raw": raw}
        with open(OUT_FILE, "a", encoding="utf-8") as f:                      # CHANGE 1
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        time.sleep(gap)
    stats["retries"] = RETRIES["n"]
    print("  retries:", RETRIES["n"])
    report[label] = stats

print("\n=== SUMMARY (pages tested: %d)" % len(pages))
print(f"{'model':45} {'json':>5} {'pass':>5} {'abstain':>8} {'err':>4} {'retries':>8} {'avg s':>6}")
for m, s in report.items():
    avg = s["secs"] / s["ok_calls"] if s["ok_calls"] else 0.0
    print(f"{m:45} {s['valid_json']:>5} {s['pass']:>5} {s['abstain']:>8} {s['errors']:>4} {s['retries']:>8} {avg:>6.1f}")