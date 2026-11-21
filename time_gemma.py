import os, time
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
load_dotenv()
for mid in os.environ["GEMINI_MODEL_IDS"].split(",")[:2]:
    m = ChatGoogleGenerativeAI(model=mid.strip(), google_api_key=os.environ["GEMINI_API_KEY"])
    for i in range(3):
        t = time.time()
        try:
            m.invoke("Reply with the single word: ok")
            print(mid, f"call {i+1}: {time.time()-t:.1f}s")
        except Exception as e:
            print(mid, f"call {i+1}: FAIL {type(e).__name__} {time.time()-t:.1f}s", str(e)[:100])