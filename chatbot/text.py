"""Small text helpers shared by the keyword index, the mock answer writer and the lead rules."""
from __future__ import annotations

import re

# Words that carry no topic. Question words are included: "how much does X cost" should match
# on "cost" and X, not on "how".
STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from had has have how i if in is it its
me my of on or our so than that the their them then there these they this to was we were what when
where which who why will with would you your about any am also get got please tell know want need
much many some just into up out no not yes ok okay hi hello hey thanks thank
""".split())

_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])")


def stem(token: str) -> str:
    """Very light stemming: enough to match desk/desks and deliver/delivery/delivered.

    A full stemmer is not needed on a few dozen pages, and a light one never merges two
    unrelated words, which keeps keyword matches explainable.
    """
    if len(token) <= 3 or token.isdigit():
        return token
    if token.endswith("ies") and len(token) > 5:
        token = token[:-3] + "y"      # deliveries -> delivery, then -> deliver below
    for suffix, repl in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ery", "er"), ("ed", ""), ("es", "e"), ("s", "")):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            # "glass" and "access" keep their double s.
            if suffix == "s" and token.endswith("ss"):
                return token
            return token[: len(token) - len(suffix)] + repl
    return token


def tokenize(text: str, keep_stopwords: bool = False) -> list[str]:
    tokens = _TOKEN.findall(text.lower())
    if not keep_stopwords:
        tokens = [t for t in tokens if t not in STOPWORDS]
    return [stem(t) for t in tokens]


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text.strip()) if s.strip()]
