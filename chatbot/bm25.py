"""BM25 keyword search (Okapi), written out in full because it is 40 lines and has no dependency."""
from __future__ import annotations

import math
from collections import Counter

from .text import tokenize


class BM25:
    def __init__(self, documents: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.term_counts = [Counter(tokenize(d)) for d in documents]
        self.lengths = [sum(c.values()) for c in self.term_counts]
        self.avg_length = (sum(self.lengths) / len(self.lengths)) if documents else 0.0
        df: Counter[str] = Counter()
        for counts in self.term_counts:
            df.update(counts.keys())
        n = len(documents)
        # Rare words weigh more: "walnut" says more about a passage than "desk".
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = tokenize(query)
        out = []
        for counts, length in zip(self.term_counts, self.lengths):
            score = 0.0
            for t in terms:
                tf = counts.get(t, 0)
                if tf:
                    norm = tf + self.k1 * (1 - self.b + self.b * length / (self.avg_length or 1))
                    score += self.idf[t] * tf * (self.k1 + 1) / norm
            out.append(score)
        return out
