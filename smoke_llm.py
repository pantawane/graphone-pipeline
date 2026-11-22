import asyncio
import re

from src.db import Database
from src.llm import LLMOrchestrator
from src.log import setup_logging

PROMPT = """Classify the pricing model of the product described on this pricing page.
Use ONLY the text between <page> tags. If unclear, use null. Never guess.
Return ONLY one JSON object: {"pricing_model": "FREE" | "FREEMIUM" | "PAID" | "ENTERPRISE" | null, "evidence": ["exact quote copied from the page", ...]}
<page>
{text}
</page>"""
FOCUS = re.compile(r"[$€£₹]\s?\d|\bfree\b|\bplan\b|\bpricing\b|\bper (?:month|user|seat|year)\b|\bcontact sales\b", re.I)
SMALL = "Pricing. Free plan: $0 per month, 1 project. Pro plan: $29 per month, unlimited projects. Enterprise: contact sales."
BIG = ("Welcome to our blog about many things. " * 4000) + SMALL + (" Footer links and legal. " * 4000)


def make_validator(text):
    norm = lambda s: re.sub(r"\s+", " ", s).lower().strip()

    def validate(d):
        if d.get("pricing_model") not in ("FREE", "FREEMIUM", "PAID", "ENTERPRISE", None):
            return "bad_enum"
        if d.get("pricing_model") is not None:
            ev = d.get("evidence")
            if not isinstance(ev, list) or not ev:
                return "no_evidence"
            if any(not isinstance(q, str) or norm(q) not in norm(text) for q in ev):
                return "evidence_not_in_page"
        return None

    return validate


async def main():
    setup_logging("INFO")
    async with Database("data/pipeline.db") as db:
        orch = await LLMOrchestrator.create("config/llm.json", db)
        for label, text in (("small page", SMALL), ("huge page (forces truncation)", BIG)):
            res = await orch.extract(PROMPT, text, make_validator(text), focus=FOCUS)
            print(f"\n[{label}] answered by: {res.tier}\n  data   : {res.data}\n  events : {res.events}")
        print("\n" + orch.summary())


asyncio.run(main())