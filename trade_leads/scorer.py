from __future__ import annotations
import re

# Each pattern represents a distinct signal that the business misses/ignores calls.
# One matching pattern per review = +1 to score (prevents one rant from maxing score).
_PATTERNS = [
    r"didn'?t call back",
    r"did not call back",
    r"couldn'?t get hold",
    r"could not get hold",
    r"no answer",
    r"never respond",
    r"never repl",
    r"had to chase",
    r"left.{0,10}message",
    r"\bignored\b",
    r"no reply",
    r"took ages to repl",
    r"didn'?t respond",
    r"did not respond",
    r"couldn'?t reach",
    r"could not reach",
    r"hard to reach",
    r"no one answered",
    r"nobody answered",
    r"never answers",
    r"\bvoicemail\b",
    r"unreachable",
    r"couldn'?t get through",
    r"slow to respond",
    r"slow to reply",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _PATTERNS]


def score_reviews(review_texts: list[str]) -> tuple[int, list[str]]:
    """Return (score capped at 3, list of short excerpts from matching reviews)."""
    score = 0
    excerpts: list[str] = []

    for text in review_texts:
        for pattern in _COMPILED:
            m = pattern.search(text)
            if m:
                score += 1
                start = max(0, m.start() - 40)
                end = min(len(text), m.end() + 40)
                excerpt = ("..." if start > 0 else "") + text[start:end].strip() + ("..." if end < len(text) else "")
                excerpts.append(excerpt)
                break  # one score increment per review regardless of how many patterns match

        if score >= 3:
            break

    return min(score, 3), excerpts[:3]
