"""Deterministic entity resolution: no LLM, every decision is explainable and logged."""
import json
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "gmbh", "ag", "plc", "lp", "llp", "pbc", "sa", "sas", "bv", "pte", "pvt", "oy", "ab", "spa", "srl",
}
GENERIC_SUFFIXES = {"ai", "labs", "lab", "technologies", "technology", "systems", "software",
                    "hq", "io", "group", "studio", "studios", "platform"}


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKC", name).lower().replace("&", " and ")
    tokens = re.sub(r"[^\w\s]", " ", s).split()
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def squash(norm: str) -> str:
    return norm.replace(" ", "")


def core_key(norm: str) -> str:
    tokens = norm.split()
    while len(tokens) > 1 and tokens[-1] in GENERIC_SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def clean_display(raw: str) -> str:
    parts = raw.replace(",", " ").split()
    while len(parts) > 1 and re.sub(r"\W", "", parts[-1]).lower() in LEGAL_SUFFIXES:
        parts.pop()
    return " ".join(parts)


def domain_of(value: str | None) -> str:
    if not value:
        return ""
    v = value.strip().lower()
    host = urlsplit(v if "//" in v else "//" + v).hostname or ""
    return host[4:] if host.startswith("www.") else host


@dataclass
class Resolution:
    raw: str
    canonical: str
    method: str
    score: float | None
    is_new: bool


class EntityResolver:
    def __init__(self, seeds: list[dict]):
        self.by_key: dict[str, tuple[str, str]] = {}      # squashed name -> (canonical, 'canonical'|'alias')
        self.by_core: dict[str, set[str]] = {}
        self.by_domain: dict[str, str] = {}
        for s in seeds:
            self._add(s["canonical"], s.get("aliases", []), s.get("domain"), strict=True)

    def _add(self, canonical: str, aliases: list[str], domain: str | None, strict: bool = False) -> None:
        for name, kind in [(canonical, "canonical")] + [(a, "alias") for a in aliases]:
            norm = normalize(name)
            key = squash(norm)
            existing = self.by_key.get(key)
            if existing and existing[0] != canonical:
                if strict:
                    raise ValueError(f"seed conflict: '{name}' maps to both '{existing[0]}' and '{canonical}'")
                continue
            self.by_key.setdefault(key, (canonical, kind))
            self.by_core.setdefault(core_key(norm), set()).add(canonical)
        d = domain_of(domain)
        if d:
            self.by_domain.setdefault(d, canonical)

    def _match_domain(self, domain: str | None) -> str | None:
        d = domain_of(domain)
        labels = d.split(".") if d else []
        for i in range(max(len(labels) - 1, 0)):               # careers.openai.com -> openai.com
            cand = ".".join(labels[i:])
            if cand in self.by_domain:
                return self.by_domain[cand]
        return None

    def _match_fuzzy(self, key: str) -> tuple[str, float] | None:
        n = len(key)
        max_d = 0 if n < 6 else (1 if n < 10 else 2)
        if max_d == 0:
            return None
        best: dict[str, tuple[int, str]] = {}                  # canonical -> (distance, matched key)
        for k, (canon, _) in self.by_key.items():
            if len(k) < 6 or abs(len(k) - n) > max_d or k[0] != key[0]:
                continue
            d = Levenshtein.distance(key, k)
            if 0 < d <= max_d and (canon not in best or d < best[canon][0]):
                best[canon] = (d, k)
        if not best:
            return None
        ranked = sorted(best.items(), key=lambda kv: kv[1][0])
        if len(ranked) > 1 and ranked[0][1][0] == ranked[1][1][0]:
            return None                                        # ambiguous -> do not guess
        canon, (_, k) = ranked[0]
        return canon, round(fuzz.ratio(key, k), 1)

    def resolve(self, raw: str, domain: str | None = None) -> Resolution:
        raw_clean = " ".join((raw or "").split())
        if not raw_clean:
            return Resolution(raw or "", "", "empty", None, False)
        norm = normalize(raw_clean)
        key = squash(norm)

        hit = self.by_key.get(key)
        if hit:
            return Resolution(raw, hit[0], "exact_normalized" if hit[1] == "canonical" else "alias", 100.0, False)

        canon = self._match_domain(domain)
        if canon:
            return Resolution(raw, canon, "domain", 100.0, False)

        cands = self.by_core.get(core_key(norm))
        if cands and len(cands) == 1 and len(core_key(norm)) >= 3:
            return Resolution(raw, next(iter(cands)), "core_name", 95.0, False)

        fuzzy = self._match_fuzzy(key)
        if fuzzy:
            return Resolution(raw, fuzzy[0], "fuzzy_edit_distance", fuzzy[1], False)

        display = clean_display(raw_clean)
        self._add(display, [], domain)                         # remember it: same name -> same canonical
        return Resolution(raw, display, "new_entity", None, True)

    async def resolve_and_log(self, db, raw: str, record_type: str, domain: str | None = None) -> Resolution:
        r = self.resolve(raw, domain)
        await db.log_entity(record_type, raw, r.canonical, r.method, r.score)
        return r


def load_seeds(path: str = "seeds/canonical_startups.json") -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)