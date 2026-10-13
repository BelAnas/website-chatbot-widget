"""The searchable index of one website: passages, a keyword index and one vector per passage."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .bm25 import BM25
from .chunking import Chunk, chunk_pages
from .crawl import Page
from .embed import Embedder

RRF_K = 60  # the usual constant of reciprocal rank fusion; larger = flatter weighting of ranks


@dataclass
class Hit:
    chunk: Chunk
    score: float      # the score of the chosen search mode, used for ordering
    cosine: float     # similarity of meaning between question and passage, used by the scope gate
    keyword: float    # BM25 score


class SiteIndex:
    """Everything is held in memory: a website is a few hundred passages, not millions."""

    def __init__(self, chunks: list[Chunk], vectors: np.ndarray, embedder: Embedder, meta: dict | None = None) -> None:
        self.chunks = chunks
        self.vectors = vectors
        self.embedder = embedder
        self.bm25 = BM25([c.indexed_text for c in chunks])
        self.meta = meta or {}

    # -- building and storing --
    @classmethod
    def build(cls, pages: list[Page], embedder: Embedder, source: str = "") -> "SiteIndex":
        chunks = chunk_pages(pages)
        if not chunks:
            raise ValueError("no text found: the site has no readable pages")
        vectors = embedder.embed([c.indexed_text for c in chunks])
        meta = {"source": source, "pages": len(pages), "passages": len(chunks),
                "words": sum(p.word_count for p in pages), "embedder": embedder.name,
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        return cls(chunks, vectors, embedder, meta)

    def save(self, folder: str | Path) -> None:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "chunks.json").write_text(json.dumps([c.to_dict() for c in self.chunks], ensure_ascii=False), encoding="utf-8")
        np.save(folder / "vectors.npy", self.vectors)
        (folder / "manifest.json").write_text(json.dumps(self.meta, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, folder: str | Path, embedder: Embedder) -> "SiteIndex | None":
        """Returns None when there is no index, or when it was built with another embedder."""
        folder = Path(folder)
        try:
            meta = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            if meta.get("embedder") != embedder.name:
                return None
            chunks = [Chunk(**c) for c in json.loads((folder / "chunks.json").read_text(encoding="utf-8"))]
            return cls(chunks, np.load(folder / "vectors.npy"), embedder, meta)
        except (OSError, ValueError, TypeError):
            return None

    # -- searching --
    def search(self, query: str, mode: str = "hybrid", k: int = 4) -> list[Hit]:
        """Best `k` passages for a question. `mode` is hybrid, dense (meaning) or bm25 (keywords)."""
        keyword = np.array(self.bm25.scores(query), dtype=np.float32)
        cosine = self.vectors @ self.embedder.embed([query])[0]
        if mode == "bm25":
            score = keyword
        elif mode == "dense":
            score = cosine
        elif mode == "hybrid":
            # Reciprocal rank fusion: a passage ranked high by either search rises, and the two
            # score scales (BM25 is unbounded, cosine is 0 to 1) never have to be compared.
            score = np.zeros(len(self.chunks), dtype=np.float32)
            for values in (keyword, cosine):
                ranking = np.argsort(-values, kind="stable")
                points = 1.0 / (RRF_K + 1 + np.arange(len(ranking)))
                # A passage with no shared word has no keyword rank at all. Without this line
                # its position in the file would count as a rank and pollute the result.
                points[values[ranking] <= 0] = 0.0
                score[ranking] += points
        else:
            raise ValueError(f"unknown search mode {mode!r} (use hybrid, dense or bm25)")
        order = np.argsort(-score, kind="stable")[:k]
        return [Hit(self.chunks[i], float(score[i]), float(cosine[i]), float(keyword[i])) for i in order if score[i] > 0]
