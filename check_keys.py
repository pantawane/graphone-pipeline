import os
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq
load_dotenv()

def text_of(msg):
    c = msg.content
    if isinstance(c, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return c

for mid in os.environ["GEMINI_MODEL_IDS"].split(","):
    mid = mid.strip()
    try:
        m = ChatGoogleGenerativeAI(model=mid, temperature=0, google_api_key=os.environ["GEMINI_API_KEY"])
        print("OK  ", mid, "->", text_of(m.invoke("Reply with the single word: ok")).strip()[:40])
    except Exception as e:
        print("FAIL", mid, type(e).__name__, str(e)[:200])

for mid in os.environ["GROQ_MODEL_IDS"].split(","):
    mid = mid.strip()
    try:
        m = ChatGroq(model=mid, temperature=0, max_tokens=500)
        print("OK  ", mid, "->", text_of(m.invoke("Reply with the single word: ok")).strip()[:40])
    except Exception as e:
        print("FAIL", mid, type(e).__name__, str(e)[:200])