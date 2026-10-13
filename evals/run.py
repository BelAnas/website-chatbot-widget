"""Measures the chatbot on the labelled sets in this folder and writes a report.

    python -m evals.run                         everything, offline, on the mock answer writer
    python -m evals.run --embedder hash         the same with the offline stand-in embedder
    python -m evals.run --provider openai --limit 20
                                                answers from a real model on a sample (reads
                                                OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL)

What is measured
  search      does the right page come back, for keyword, meaning and hybrid search
  scope gate  are site questions let through and off-topic questions refused
  answers     does the answer contain the fact the website states, and link to the right page
  follow-ups  does "and express?" get answered using the previous question
  leads       precision and recall of the lead rules on labelled visitor messages
  speed       time to the first word and to the full answer

The questions are split in two halves. Anything chosen by looking at results (the search
mode, the refusal threshold, the mock's scoring) was chosen on the "dev" half; the numbers
to quote are the ones on the "test" half.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import statistics
import time
from pathlib import Path

from chatbot.config import ROOT, Settings
from chatbot.crawl import load_directory
from chatbot.embed import make_embedder
from chatbot.engine import ChatEngine
from chatbot.index import SiteIndex
from chatbot.leads import detect_intent
from chatbot.llm import make_provider
from chatbot.store import Store

HERE = Path(__file__).resolve().parent
MODES = ("bm25", "dense", "hybrid")


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (HERE / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def page_of(url: str) -> str:
    """/demo/delivery.html#express-delivery -> delivery"""
    return url.split("#")[0].rsplit("/", 1)[-1].removesuffix(".html")


def pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def has_fact(answer: str, facts: list[str]) -> bool:
    return any(f.lower() in answer.lower() for f in facts)


# ---- search ----
def search_metrics(index: SiteIndex, rows: list[dict], mode: str) -> dict:
    ranks = []
    for row in rows:
        pages = [page_of(h.chunk.url) for h in index.search(row["q"], mode, 5)]
        ranks.append(next((i + 1 for i, p in enumerate(pages) if p in row["pages"]), None))
    n = len(rows)
    return {"questions": n,
            "right_page_first_pct": pct(sum(r == 1 for r in ranks), n),
            "right_page_in_top3_pct": pct(sum(r is not None and r <= 3 for r in ranks), n),
            "mrr": round(sum(1 / r for r in ranks if r) / n, 3)}


# ---- scope gate ----
def gate_scores(engine: ChatEngine, rows: list[dict]) -> list[float]:
    return [max((h.cosine for h in engine.retrieve(row["q"], [])[1]), default=0.0) for row in rows]


def choose_threshold(site: list[float], off: list[float]) -> tuple[float, float]:
    """The threshold with the best average of 'site question accepted' and 'off-topic refused'."""
    best: list[tuple[float, float]] = []
    for step in range(5, 96):
        t = step / 100
        score = (sum(s >= t for s in site) / len(site) + sum(o < t for o in off) / len(off)) / 2
        best.append((score, t))
    top = max(s for s, _ in best)
    winners = [t for s, t in best if s == top]
    return winners[len(winners) // 2], round(100 * top, 1)     # the middle of the best range: most margin both ways


# ---- answers ----
async def run_turn(engine: ChatEngine, session: str, message: str) -> dict:
    out = {"answer": "", "sources": [], "lead_prompt": None}
    async for event in engine.reply(session, message):
        if event["type"] == "token":
            out["answer"] += event["text"]
        elif event["type"] == "sources":
            out["sources"] = [page_of(s["url"]) for s in event["sources"]]
        elif event["type"] == "lead_prompt":
            out["lead_prompt"] = event["reason"]
        elif event["type"] == "done":
            out.update(kind=event["kind"], first_token_ms=event["first_token_ms"], total_ms=event["total_ms"],
                       input_tokens=event["input_tokens"], output_tokens=event["output_tokens"], cost_usd=event["cost_usd"])
    return out


async def answer_metrics(engine: ChatEngine, rows: list[dict], tag: str) -> tuple[dict, list[dict]]:
    results = []
    for i, row in enumerate(rows):
        turn = await run_turn(engine, f"eval-{tag}-{i:04d}", row["q"])
        results.append({"q": row["q"], "answer": turn["answer"], "kind": turn["kind"], "sources": turn["sources"],
                        "fact": has_fact(turn["answer"], row["facts"]),
                        "right_source": any(p in row["pages"] for p in turn["sources"]),
                        "first_token_ms": turn["first_token_ms"], "total_ms": turn["total_ms"],
                        "input_tokens": turn["input_tokens"], "output_tokens": turn["output_tokens"], "cost_usd": turn["cost_usd"]})
    n = len(rows)
    summary = {"questions": n,
               "answer_has_the_fact_pct": pct(sum(r["fact"] for r in results), n),
               "right_page_linked_pct": pct(sum(r["right_source"] for r in results), n),
               "wrongly_refused_pct": pct(sum(r["kind"] == "refusal" for r in results), n)}
    return summary, results


async def refusal_metrics(engine: ChatEngine, rows: list[dict], tag: str) -> tuple[dict, list[dict]]:
    results = []
    for i, row in enumerate(rows):
        turn = await run_turn(engine, f"eval-{tag}-{i:04d}", row["q"])
        results.append({"q": row["q"], "answer": turn["answer"], "kind": turn["kind"],
                        "cost_usd": turn["cost_usd"], "refused": turn["kind"] in ("refusal", "small_talk", "handoff")})
    return {"questions": len(rows), "refused_pct": pct(sum(r["refused"] for r in results), len(rows))}, results


async def followup_metrics(engine: ChatEngine, rows: list[dict]) -> tuple[dict, list[dict]]:
    results = []
    for i, row in enumerate(rows):
        session = f"eval-follow-{i:04d}"
        await run_turn(engine, session, row["first"])
        turn = await run_turn(engine, session, row["followup"])
        results.append({**row, "answer": turn["answer"], "fact": has_fact(turn["answer"], row["facts"]),
                        "cost_usd": turn["cost_usd"]})
    dependent = [r for r in results if r["depends_on_first"]]
    standalone = [r for r in results if not r["depends_on_first"]]
    return {"follow_ups": len(dependent), "follow_up_answered_pct": pct(sum(r["fact"] for r in dependent), len(dependent)),
            "new_topics": len(standalone), "new_topic_answered_pct": pct(sum(r["fact"] for r in standalone), len(standalone))}, results


# ---- leads ----
def lead_metrics(rows: list[dict]) -> dict:
    predicted = [detect_intent(r["text"]) is not None for r in rows]
    tp = sum(p and r["lead"] for p, r in zip(predicted, rows))
    fp = [r["text"] for p, r in zip(predicted, rows) if p and not r["lead"]]
    fn = [r["text"] for p, r in zip(predicted, rows) if not p and r["lead"]]
    precision, recall = tp / (tp + len(fp) or 1), tp / (tp + len(fn) or 1)
    return {"messages": len(rows), "leads": sum(r["lead"] for r in rows),
            "precision_pct": round(100 * precision, 1), "recall_pct": round(100 * recall, 1),
            "f1": round(2 * precision * recall / (precision + recall or 1), 3),
            "missed": fn, "false_alarms": fp}


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)


async def main_async(args: argparse.Namespace) -> dict:
    settings = Settings.from_env()
    settings.embedder, settings.mock_delay_ms = args.embedder, 0
    settings.llm_provider = args.provider
    embedder = make_embedder(settings.embedder, settings.embedding_model)
    index = SiteIndex.build(load_directory(ROOT / "demo_site", "/demo"), embedder, "demo_site")
    questions = load("questions.jsonl")
    site = [q for q in questions if q["kind"] == "site"]
    off = [q for q in questions if q["kind"] == "off_topic"]
    uncovered = [q for q in questions if q["kind"] == "uncovered"]
    half = lambda rows, split: [r for r in rows if r["split"] == split]  # noqa: E731
    provider = make_provider(settings, embedder)
    model = settings.openai_model if args.provider == "openai" else "mock"
    report: dict = {"embedder": embedder.name, "provider": args.provider, "model": model, "search_mode": settings.search_mode,
                    "index": {k: index.meta[k] for k in ("pages", "passages", "words")}}

    def new_engine() -> ChatEngine:
        return ChatEngine(settings, index, Store(":memory:"), provider)

    if args.provider != "mock":
        # A real model costs time or money: answer a sample of the test half and stop.
        engine = new_engine()
        sample = half(site, "test")[: args.limit]
        started = time.perf_counter()
        answers, answer_rows = await answer_metrics(engine, sample, "live")
        gaps, gap_rows = await refusal_metrics(engine, half(uncovered, "test")[: max(1, args.limit // 3)], "live-gap")
        follow, follow_rows = await followup_metrics(engine, load("followups.jsonl")[: max(2, args.limit // 4)])
        calls = [r for r in answer_rows + gap_rows + follow_rows]
        report.update({
            "answers_test_sample": answers, "uncovered_test_sample": gaps, "follow_ups_sample": follow,
            "first_word_ms_median": round(statistics.median(r["first_token_ms"] for r in answer_rows), 1),
            "full_answer_ms_median": round(statistics.median(r["total_ms"] for r in answer_rows), 1),
            "input_tokens": sum(r["input_tokens"] for r in answer_rows), "output_tokens": sum(r["output_tokens"] for r in answer_rows),
            "cost_usd": round(sum(r.get("cost_usd", 0) for r in calls), 5),
            "wall_time_s": round(time.perf_counter() - started, 1),
            "details": {"answers": answer_rows, "uncovered": gap_rows, "follow_ups": follow_rows}})
        return report

    # 1. search, each mode, each half
    report["search"] = {mode: {split: search_metrics(index, half(site, split), mode) for split in ("dev", "test")} for mode in MODES}

    # 2. scope gate
    engine = new_engine()
    dev_site, dev_off = gate_scores(engine, half(site, "dev")), gate_scores(engine, half(off, "dev"))
    suggested, dev_balance = choose_threshold(dev_site, dev_off)
    t = engine.min_relevance
    test_site, test_off = gate_scores(engine, half(site, "test")), gate_scores(engine, half(off, "test"))
    test_unc = gate_scores(engine, half(uncovered, "test"))
    report["scope_gate"] = {
        "threshold_in_use": t, "threshold_suggested_by_dev_half": suggested, "dev_balanced_accuracy_pct": dev_balance,
        "test_site_questions_accepted_pct": pct(sum(s >= t for s in test_site), len(test_site)),
        "test_off_topic_refused_pct": pct(sum(s < t for s in test_off), len(test_off)),
        "test_uncovered_refused_pct": pct(sum(s < t for s in test_unc), len(test_unc)),
        "test_counts": {"site": len(test_site), "off_topic": len(test_off), "uncovered": len(test_unc)},
        "lowest_site_score": round(min(dev_site + test_site), 3), "highest_off_topic_score": round(max(dev_off + test_off), 3)}

    # 3. answers end to end (mock), both halves, plus follow-ups
    report["answers"] = {}
    details = {}
    timings: list[dict] = []
    for split in ("dev", "test"):
        summary, rows = await answer_metrics(new_engine(), half(site, split), split)
        report["answers"][split] = summary
        details[f"answers_{split}"] = [{k: r[k] for k in ("q", "answer", "fact", "right_source", "kind")} for r in rows]
        timings += rows
    summary, rows = await refusal_metrics(new_engine(), half(off, "test"), "off")
    report["answers"]["off_topic_test"] = summary
    report["follow_ups"], follow_rows = await followup_metrics(new_engine(), load("followups.jsonl"))
    details["follow_ups"] = [{k: r[k] for k in ("first", "followup", "answer", "fact")} for r in follow_rows]

    # 4. leads
    lead_rows = load("lead_messages.jsonl")
    report["lead_rules"] = {split: lead_metrics(half(lead_rows, split)) for split in ("dev", "test")}

    # 5. speed (mock writer with no artificial delay: this is search plus plumbing, not model time)
    started = time.perf_counter()
    for row in site:
        index.search(row["q"], settings.search_mode, settings.top_k)
    report["speed"] = {"search_ms_mean": round(1000 * (time.perf_counter() - started) / len(site), 2),
                       "first_word_ms_median": round(statistics.median(r["first_token_ms"] for r in timings), 1),
                       "first_word_ms_p95": percentile([r["first_token_ms"] for r in timings], 0.95),
                       "full_answer_ms_median": round(statistics.median(r["total_ms"] for r in timings), 1),
                       "questions": len(timings)}
    widget = (ROOT / "chatbot" / "static" / "widget.js").read_bytes()
    report["widget"] = {"bytes": len(widget), "gzip_bytes": len(gzip.compress(widget, 9))}
    report["details"] = details
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--embedder", default="fastembed", choices=["fastembed", "hash"])
    parser.add_argument("--provider", default="mock", choices=["mock", "openai"])
    parser.add_argument("--limit", type=int, default=20, help="questions answered by a real model")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    report = asyncio.run(main_async(args))
    name = args.out or (f"report-{args.embedder}.json" if args.provider == "mock" else f"report-live-{report['model'].replace(':', '-').replace('/', '-')}.json")
    (HERE / name).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "details"}, indent=2, ensure_ascii=False))
    print(f"\nreport written to evals/{name}")


if __name__ == "__main__":
    main()
