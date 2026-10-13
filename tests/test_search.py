"""Keyword search, embeddings, the hybrid merge and saving the index."""
from __future__ import annotations

import numpy as np

from chatbot.bm25 import BM25
from chatbot.embed import HashEmbedder
from chatbot.index import SiteIndex
from chatbot.text import sentences, stem, tokenize


def test_stemming_joins_word_forms_without_merging_different_words() -> None:
    assert stem("desks") == stem("desk")
    assert stem("deliveries") == stem("delivery") == stem("delivered") == "deliver"
    assert stem("glass") == "glass" and stem("5") == "5"
    assert tokenize("How much does the Lift Pro cost?") == ["lift", "pro", "cost"]
    assert tokenize("$1,190 and 0.1 W") == ["1", "190", "0.1", "w"]


def test_sentence_split_keeps_decimals_and_prices_whole() -> None:
    assert sentences("It costs $39.50 today. Express is $79. Done!") == ["It costs $39.50 today.", "Express is $79.", "Done!"]


def test_bm25_prefers_the_passage_with_the_rare_word() -> None:
    bm25 = BM25(["the desk is made of oak", "the desk is made of walnut", "chairs come in navy"])
    scores = bm25.scores("walnut desk")
    assert scores[1] > scores[0] > scores[2] == 0


def test_every_search_mode_finds_the_obvious_page(index: SiteIndex) -> None:
    for mode in ("bm25", "dense", "hybrid"):
        hits = index.search("express delivery cost", mode, 3)
        assert hits[0].chunk.label == "Delivery › Express delivery", mode
        assert hits[0].chunk.url == "/demo/delivery.html#express-delivery"


def test_hybrid_ignores_the_keyword_ranking_when_no_word_matches(index: SiteIndex) -> None:
    # "shipment" is not a word of the site, so BM25 scores are all zero. The fused result must
    # then be the meaning search alone, not padded with whatever passages come first in the file.
    assert index.search("shipment zzzz", "bm25", 5) == []
    hybrid, dense = index.search("shipment zzzz", "hybrid", 5), index.search("shipment zzzz", "dense", 5)
    assert [h.chunk.id for h in hybrid] == [h.chunk.id for h in dense]
    assert all(h.keyword == 0 for h in hybrid)


def test_hybrid_rewards_passages_that_both_searches_like(index: SiteIndex) -> None:
    hits = index.search("warranty on desk motors", "hybrid", 3)
    assert hits[0].chunk.label.startswith("Warranty")
    assert hits[0].keyword > 0 and hits[0].cosine > 0
    assert hits == sorted(hits, key=lambda h: -h.score)


def test_index_round_trip_and_embedder_mismatch(index: SiteIndex, tmp_path) -> None:  # type: ignore[no-untyped-def]
    index.save(tmp_path)
    loaded = SiteIndex.load(tmp_path, HashEmbedder())
    assert loaded is not None and len(loaded.chunks) == len(index.chunks) == index.meta["passages"]
    assert np.allclose(loaded.vectors, index.vectors)
    assert loaded.search("locker bank", "hybrid", 1)[0].chunk.id == index.search("locker bank", "hybrid", 1)[0].chunk.id

    class OtherEmbedder(HashEmbedder):
        name = "another-model"

    # Vectors of one model mean nothing to another: the index must be rebuilt, not reused.
    assert SiteIndex.load(tmp_path, OtherEmbedder()) is None
    assert SiteIndex.load(tmp_path / "missing", HashEmbedder()) is None


def test_hash_embedder_is_deterministic_and_normalised() -> None:
    e = HashEmbedder()
    a, b = e.embed(["standing desk price", ""]), e.embed(["standing desk price"])
    assert np.allclose(a[0], b[0]) and np.isclose(np.linalg.norm(a[0]), 1.0) and not a[1].any()
