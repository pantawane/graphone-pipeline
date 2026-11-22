"""Multi-tier LLM orchestrator.

- fallback chain across tiers, each with its own limits
- quota tracking: per-minute sliding window + per-day counters persisted in SQLite
- 413 protection: smart truncation to each tier's input budget, halved again after a real 413
- 429 handling: server retry hints or exponential backoff with FULL jitter, then fall to the next tier
- the answer must parse as JSON and pass a caller-supplied validator, otherwise the next tier is tried
"""
import asyncio
import json
import logging
import os
import random
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("llm")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def full_jitter(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    return random.uniform(0, min(cap, base * (2 ** attempt)))


def text_of(msg) -> str:
    c = getattr(msg, "content", msg)
    if isinstance(c, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return str(c)


def parse_json_object(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def parse_retry_delay(s: str) -> float | None:
    m = re.search(r"(?:retry|try again)(?: in| after)?:?\s*(?:(\d+)m)?\s*([\d.]+)\s*(ms|s)\b", s)
    if not m:
        return None
    secs = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1) + int(m.group(1) or 0) * 60
    return min(secs, 120.0)


def classify_error(exc: BaseException) -> tuple[str, float | None]:
    """Returns (kind, retry_after_seconds). kinds: timeout, too_large, rate_limited, quota_day, auth, server, other."""
    s = f"{type(exc).__name__}: {exc}".lower()
    hint = parse_retry_delay(s)
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in s or "timed out" in s:
        return "timeout", None
    if (re.search(r"\b413\b", s) or "too large" in s or "context length" in s or "context_length" in s
            or "maximum context" in s or "too many tokens" in s):
        return "too_large", None
    if re.search(r"\b429\b", s) or "resource_exhausted" in s or "rate limit" in s or "rate_limit" in s \
            or "too many requests" in s or "quota" in s:
        if "per day" in s or "daily" in s or "perday" in s:
            return "quota_day", None
        return "rate_limited", hint
    if re.search(r"\b(401|403)\b", s) or "api key" in s or "permission" in s or "unauthenticated" in s:
        return "auth", None
    if re.search(r"\b50[0-4]\b", s) or "unavailable" in s or "overloaded" in s or "internal" in s:
        return "server", None
    return "other", None


def smart_truncate(text: str, max_tokens: int, focus: "re.Pattern | None" = None) -> str:
    """Fit text into a token budget (~4 chars per token) while keeping the beginning plus the
    most informative chunks (highest number of `focus` matches), in their original order."""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    pieces: list[str] = []
    for p in re.split(r"(?<=[.!?|])\s+", text):
        while len(p) > 800:
            pieces.append(p[:800])
            p = p[800:]
        if p:
            pieces.append(p)
    chunks, cur = [], ""
    for p in pieces:
        if cur and len(cur) + len(p) + 1 > 500:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)

    head_budget = int(max_chars * 0.2)
    chunks[0] = chunks[0][:head_budget]
    keep = {0}
    used = len(chunks[0])
    for i in range(1, len(chunks)):
        if used + len(chunks[i]) + 1 > head_budget:
            break
        keep.add(i)
        used += len(chunks[i]) + 1

    def score(c: str) -> int:
        return len(focus.findall(c)) if focus else 0

    for i in sorted((i for i in range(len(chunks)) if i not in keep), key=lambda i: (-score(chunks[i]), i)):
        if used + len(chunks[i]) + 1 <= max_chars:
            keep.add(i)
            used += len(chunks[i]) + 1

    out, prev = [], None
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            out.append("…")
        out.append(chunks[i])
        prev = i
    return " ".join(out)


@dataclass
class Tier:
    name: str
    model: object                       # anything with: async ainvoke(prompt) -> object with .content
    max_input_tokens: int
    timeout_s: float = 30
    rpm: int | None = None
    tpm: int | None = None
    rpd: int | None = None
    tpd: int | None = None
    max_attempts: int = 3
    # runtime state
    window: deque = field(default_factory=deque)       # (timestamp, tokens) of requests in the last 60s
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    req_today: int = 0
    tok_today: int = 0
    consecutive_failures: int = 0
    open_until: float = 0.0
    exhausted_day: str | None = None
    disabled: bool = False


@dataclass
class LLMResult:
    data: dict | None
    tier: str | None
    events: list[str] = field(default_factory=list)
    reason: str = ""


class LLMOrchestrator:
    def __init__(self, tiers: list[Tier], db=None, backoff_base: float = 1.0, max_sleep: float = 30.0,
                 breaker_threshold: int = 4, breaker_cooldown: float = 60.0):
        self.tiers = tiers
        self.db = db
        self.backoff_base = backoff_base
        self.max_sleep = max_sleep
        self.breaker_threshold = breaker_threshold
        self.breaker_cooldown = breaker_cooldown
        self.stats: Counter = Counter()

    @classmethod
    async def create(cls, path: str = "config/llm.json", db=None) -> "LLMOrchestrator":
        from dotenv import load_dotenv
        load_dotenv()
        cfg = json.load(open(path, encoding="utf-8"))
        tiers = []
        for t in cfg["tiers"]:
            try:
                model = _build_model(t)
            except Exception as exc:
                log.error("tier %s unavailable: %s", t["name"], exc)
                continue
            tiers.append(Tier(name=t["name"], model=model, max_input_tokens=t["max_input_tokens"],
                              timeout_s=t.get("timeout_s", 30), rpm=t.get("rpm"), tpm=t.get("tpm"),
                              rpd=t.get("rpd"), tpd=t.get("tpd"), max_attempts=t.get("max_attempts", 3)))
        orch = cls(tiers, db=db, **cfg.get("settings", {}))
        await orch.start()
        return orch

    async def start(self) -> None:
        if self.db:
            for t in self.tiers:
                t.req_today, t.tok_today = await self.db.get_llm_usage(t.name)

    # ---------------------------------------------------------------- availability and quotas
    def _unavailable(self, t: Tier) -> str | None:
        if t.disabled:
            return "disabled"
        if t.exhausted_day == today():
            return "quota_day"
        if t.rpd is not None and t.req_today >= t.rpd:
            return "quota_day"
        if t.tpd is not None and t.tok_today >= t.tpd:
            return "quota_day"
        if t.open_until > time.monotonic():
            return "cooldown"
        return None

    def all_unavailable(self) -> bool:
        return all(self._unavailable(t) in ("disabled", "quota_day") for t in self.tiers)

    def _note_failure(self, t: Tier) -> None:
        t.consecutive_failures += 1
        if t.consecutive_failures >= self.breaker_threshold:
            t.open_until = time.monotonic() + self.breaker_cooldown
            t.consecutive_failures = 0
            log.warning("circuit open for %s: skipping it for %.0fs", t.name, self.breaker_cooldown)

    async def _reserve(self, t: Tier, est_tokens: int, max_wait: float = 15.0) -> bool:
        """Wait (up to max_wait) for room in the per-minute request/token window. False = tier is too busy."""
        async with t.lock:
            waited = 0.0
            while True:
                now = time.monotonic()
                while t.window and now - t.window[0][0] >= 60:
                    t.window.popleft()
                reqs = len(t.window)
                toks = sum(tok for _, tok in t.window)
                if (t.rpm is None or reqs < t.rpm) and (t.tpm is None or toks + est_tokens <= t.tpm):
                    t.window.append((now, est_tokens))
                    return True
                if not t.window:
                    return False                      # a single request larger than the per-minute budget
                wait = 60 - (now - t.window[0][0]) + 0.05
                if waited + wait > max_wait:
                    return False
                await asyncio.sleep(wait)
                waited += wait

    async def _persist(self, t: Tier, tokens: int) -> None:
        t.req_today += 1
        t.tok_today += tokens
        if self.db:
            await self.db.add_llm_usage(t.name, 1, tokens)

    # ---------------------------------------------------------------- main entry point
    async def extract(self, prompt_template: str, text: str, validate: Callable[[dict], "str | None"],
                      focus: "re.Pattern | None" = None, overhead_tokens: int = 250) -> LLMResult:
        """prompt_template contains the placeholder {text}. validate(data) returns None if OK, else a reason."""
        events: list[str] = []
        for t in self.tiers:
            why = self._unavailable(t)
            if why:
                events.append(f"{t.name}:skipped_{why}")
                continue
            budget = max(t.max_input_tokens - overhead_tokens, 200)
            for attempt in range(t.max_attempts):
                prompt = prompt_template.replace("{text}", smart_truncate(text, budget, focus))
                est = estimate_tokens(prompt) + 300
                if not await self._reserve(t, est):
                    events.append(f"{t.name}:busy")
                    break
                try:
                    msg = await asyncio.wait_for(t.model.ainvoke(prompt), timeout=t.timeout_s)
                except Exception as exc:
                    kind, hint = classify_error(exc)
                    self.stats[f"{t.name}:{kind}"] += 1
                    events.append(f"{t.name}:{kind}")
                    log.warning("%s %s: %s", t.name, kind, str(exc)[:160])
                    await self._persist(t, est)
                    if kind == "too_large":                       # 413: halve the input and retry here
                        budget = int(budget * 0.5)
                        if budget < 200:
                            break
                        continue
                    if kind == "rate_limited":                    # 429: server hint, else full-jitter backoff
                        self._note_failure(t)
                        delay = hint if hint is not None else full_jitter(attempt, self.backoff_base)
                        if delay > self.max_sleep:
                            break                                 # too long to wait: use the next tier
                        await asyncio.sleep(delay)
                        continue
                    if kind == "quota_day":
                        t.exhausted_day = today()
                        break
                    if kind == "auth":
                        t.disabled = True
                        log.error("tier %s disabled for this run (auth error)", t.name)
                        break
                    if kind in ("timeout", "server"):
                        self._note_failure(t)
                        if attempt + 1 < t.max_attempts:
                            await asyncio.sleep(full_jitter(attempt, self.backoff_base))
                            continue
                        break
                    break                                         # unknown error: next tier
                out = text_of(msg)
                await self._persist(t, est + estimate_tokens(out))
                try:
                    data = parse_json_object(out)
                except Exception:
                    self.stats[f"{t.name}:bad_json"] += 1
                    events.append(f"{t.name}:bad_json")
                    break
                reason = validate(data)
                if reason:
                    self.stats[f"{t.name}:invalid"] += 1
                    events.append(f"{t.name}:invalid({reason})")
                    break
                t.consecutive_failures = 0
                self.stats[f"{t.name}:ok"] += 1
                events.append(f"{t.name}:ok")
                return LLMResult(data, t.name, events)
        return LLMResult(None, None, events, "all_tiers_failed")

    def summary(self) -> str:
        lines = ["LLM usage this run:"]
        for k in sorted(self.stats):
            lines.append(f"  {k:30} {self.stats[k]}")
        for t in self.tiers:
            lines.append(f"  today {t.name}: {t.req_today} requests, {t.tok_today} tokens "
                         f"(limits: rpd={t.rpd}, tpd={t.tpd})")
        return "\n".join(lines)


def _build_model(cfg: dict):
    if cfg["provider"] == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        args = dict(model=cfg["model_id"], google_api_key=os.environ["GEMINI_API_KEY"], temperature=0,
                    max_output_tokens=cfg.get("max_output_tokens", 1000))
        try:
            return ChatGoogleGenerativeAI(max_retries=0, **args)     # we do our own retries
        except Exception:
            return ChatGoogleGenerativeAI(**args)
    if cfg["provider"] == "groq":
        from langchain_groq import ChatGroq
        args = dict(model=cfg["model_id"], temperature=0, max_tokens=cfg.get("max_output_tokens", 1000))
        try:
            return ChatGroq(max_retries=0, **args)
        except Exception:
            return ChatGroq(**args)
    raise ValueError(f"unknown provider {cfg['provider']}")