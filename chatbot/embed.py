"""Embedders: text in, a vector of numbers out. Two texts about the same thing get close vectors."""
from __future__ import annotations

import hashlib
from typing import Protocol

import numpy as np

from .text import tokenize


class Embedder(Protocol):
    name: str
    # Below this similarity between a question and its best passage, the site does not cover
    # the question. Each value was chosen on the development half of evals/questions.jsonl.
    default_min_relevance: float

    def embed(self, texts: list[str]) -> np.ndarray: ...


class FastEmbedder:
    """A real sentence-embedding model (ONNX, runs on CPU, about 70 MB downloaded once)."""

    default_min_relevance = 0.62

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5") -> None:
        from fastembed import TextEmbedding  # imported late: the tests never load the model

        self.name = model
        self._model = TextEmbedding(model_name=model)

    def embed(self, texts: list[str]) -> np.ndarray:
        vectors = np.array(list(self._model.embed(texts)), dtype=np.float32)
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


class HashEmbedder:
    """Offline stand-in used by the tests: words are hashed into 512 buckets.

    It only knows shared words, not meaning, so it is no substitute for the real model.
    It exists so the whole pipeline runs with no download and gives the same result every time.
    """

    name = "hash-512"
    default_min_relevance = 0.22

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), 512), dtype=np.float32)
        for row, text in enumerate(texts):
            for token in tokenize(text):
                out[row, int.from_bytes(hashlib.md5(token.encode()).digest()[:4], "big") % 512] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1.0, norms)


def make_embedder(kind: str, model: str = "BAAI/bge-small-en-v1.5") -> Embedder:
    if kind == "hash":
        return HashEmbedder()
    if kind == "fastembed":
        return FastEmbedder(model)
    raise ValueError(f"unknown embedder {kind!r} (use fastembed or hash)")
