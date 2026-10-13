"""The conversation engine: grounding, refusals, follow-ups, lead capture and the model adapter."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from chatbot.config import Settings
from chatbot.engine import HANDOFF_ONLY, ChatEngine, small_talk
from chatbot.llm import MockProvider, OpenAIProvider, TagFilter, build_messages, make_provider
from chatbot.store import Store

from .conftest import answer_text, collect, of_type


class ScriptedProvider:
    """Stands in for a real model: says what the test tells it to and records what it was given."""

    name = "scripted"

    def __init__(self, text: str = "Scripted answer.", handoff: str | None = None) -> None:
        self.text, self.handoff, self.calls = text, handoff, []

    async def stream(self, question, search_query, hits, history, site_name):  # type: ignore[no-untyped-def]
        self.calls.append({"question": question, "search_query": search_query, "hits": hits, "history": history})
        for word in self.text.split():
            yield {"type": "token", "text": word + " "}
        if self.handoff:
            yield {"type": "handoff", "reason": self.handoff}
        yield {"type": "usage", "input_tokens": 100, "output_tokens": 10, "cost_usd": 0.00002}


def scripted(engine: ChatEngine, **kwargs) -> ScriptedProvider:  # type: ignore[no-untyped-def]
    engine.provider = ScriptedProvider(**kwargs)
    return engine.provider


# ---- answers ----
def test_answer_is_built_from_the_site_and_links_to_the_section(engine: ChatEngine) -> None:
    events = collect(engine.reply("session-1", "How much does express delivery cost?"))
    assert "$79" in answer_text(events)
    assert of_type(events, "sources")[0]["sources"][0] == {
        "label": "Delivery › Express delivery", "url": "/demo/delivery.html#express-delivery"}
    done = events[-1]
    assert done["type"] == "done" and done["kind"] == "answer" and done["provider"] == "mock"
    assert done["first_token_ms"] <= done["total_ms"]


def test_answer_arrives_in_many_pieces_and_sources_come_after_the_text(engine: ChatEngine) -> None:
    events = collect(engine.reply("session-1", "What is the warranty on desk frames?"))
    kinds = [e["type"] for e in events]
    assert kinds.count("token") > 5
    assert kinds.index("sources") > max(i for i, k in enumerate(kinds) if k == "token")
    assert kinds[-1] == "done"


def test_mock_answer_is_the_same_every_time(engine: ChatEngine) -> None:
    first = answer_text(collect(engine.reply("session-a", "Can I order a custom desktop size?")))
    second = answer_text(collect(engine.reply("session-b", "Can I order a custom desktop size?")))
    assert first == second and "100 and 200 cm" in first


# ---- scope ----
def test_off_topic_question_is_refused_without_calling_the_model(engine: ChatEngine) -> None:
    provider = scripted(engine)
    events = collect(engine.reply("session-1", "Who won the football world cup in 2010?"))
    assert provider.calls == []
    assert "could not find that on the Oakline Office website" in answer_text(events)
    assert events[-1]["kind"] == "refusal" and not of_type(events, "sources")
    assert of_type(events, "lead_prompt")[0]["reason"] == "not_covered"
    assert [u["question"] for u in engine.store.unanswered()] == ["Who won the football world cup in 2010?"]


def test_small_talk_needs_no_search_and_no_model(engine: ChatEngine) -> None:
    provider = scripted(engine)
    for message in ("Hello!", "thanks a lot", "Are you a real person?"):
        events = collect(engine.reply("session-1", message))
        assert events[-1]["kind"] == "small_talk" and not of_type(events, "lead_prompt")
    assert provider.calls == [] and engine.store.unanswered() == []
    assert small_talk("hello, how much is the Lift Pro?", "X") is None   # a real question after a greeting


# ---- follow-ups and history ----
def test_previous_question_is_added_only_when_the_message_does_not_stand_alone(engine: ChatEngine) -> None:
    history = [{"role": "user", "content": "Tell me about the Lift Pro"}, {"role": "assistant", "content": "..."}]
    assert engine.retrieve("How much is it?", history)[0] == "Tell me about the Lift Pro How much is it?"
    # Opens with "and": a continuation, even though "warranty" matches a page by itself.
    assert engine.retrieve("and the warranty on desk frames?", history)[0].startswith("Tell me about the Lift Pro and")
    # A full question about something else is a new topic and is searched as it is.
    standalone = "What are the showroom opening hours on Saturday?"
    query, hits, in_scope = engine.retrieve(standalone, history)
    assert query == standalone and in_scope and hits[0].chunk.page_title == "Visit the showroom"
    assert engine.retrieve("How much is it?", [])[0] == "How much is it?"       # nothing to add


def test_follow_up_question_finds_the_page_of_the_previous_one(engine: ChatEngine) -> None:
    collect(engine.reply("session-1", "Tell me about the Lift Pro desk"))
    events = collect(engine.reply("session-1", "How much is it?"))
    assert "$689" in answer_text(events)
    # Another visitor asking the same words with no history gets no such answer.
    assert "$689" not in answer_text(collect(engine.reply("session-2", "How much is it?")))


def test_history_comes_from_the_database_and_is_capped(engine: ChatEngine) -> None:
    engine.settings.max_history_turns = 2
    provider = scripted(engine)
    for n in range(4):
        collect(engine.reply("session-1", f"Question {n} about standing desk prices?"))
    history = provider.calls[-1]["history"]
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[0]["content"] == "Question 1 about standing desk prices?"
    assert provider.calls[0]["history"] == []


# ---- lead capture ----
def test_buying_interest_offers_the_form_once(engine: ChatEngine) -> None:
    first = collect(engine.reply("session-1", "Can I get a quote for 40 standing desks?"))
    assert of_type(first, "lead_prompt")[0]["reason"] == "buying_interest"
    again = collect(engine.reply("session-1", "And a quote for 40 chairs as well?"))
    assert not of_type(again, "lead_prompt")           # no nagging
    human = collect(engine.reply("session-1", "Can I talk to a person about this?"))
    assert of_type(human, "lead_prompt")[0]["reason"] == "wants_human"   # an explicit request always works


def test_request_for_a_person_is_not_logged_as_a_content_gap(engine: ChatEngine) -> None:
    engine.min_relevance = 0.99      # force "not covered by the site"
    events = collect(engine.reply("session-1", "Can I talk to a person?"))
    assert answer_text(events) == HANDOFF_ONLY and events[-1]["kind"] == "handoff"
    assert of_type(events, "lead_prompt")[0]["reason"] == "wants_human"
    assert engine.store.unanswered() == []


def test_email_typed_in_the_chat_becomes_a_lead(engine: ChatEngine) -> None:
    notified: list[int] = []
    engine.on_lead = notified.append
    collect(engine.reply("session-1", "How much is express delivery?"))
    collect(engine.reply("session-1", "I need a quote for 30 of them"))
    provider = scripted(engine)
    events = collect(engine.reply("session-1", "sure, it's Dana@Northfield.example, phone 512 555 0142"))
    assert "dana@northfield.example" in answer_text(events)
    assert of_type(events, "lead_saved") and provider.calls == []      # contact details only: no model call
    lead = engine.store.leads()[0]
    assert notified == [lead["id"]]
    assert (lead["email"], lead["phone"], lead["source"], lead["reason"]) == (
        "dana@northfield.example", "512 555 0142", "chat", "buying_interest")
    assert "Delivery" in lead["interest"]
    assert [m["role"] for m in lead["transcript"]] == ["user", "assistant", "user", "assistant", "user"]
    # Once the team has the details the form is never offered again.
    later = collect(engine.reply("session-1", "Can someone call me about the quote?"))
    assert not of_type(later, "lead_prompt") and len(engine.store.leads()) == 1


def test_email_plus_question_saves_the_lead_and_answers(engine: ChatEngine) -> None:
    events = collect(engine.reply("session-1", "my email is bob@x.io - how long is the chair warranty?"))
    text = answer_text(events)
    assert "bob@x.io" in text and "7 years" in text and ". Chairs" not in text.split("contact you.")[0]
    assert events[-1]["kind"] == "answer" and len(engine.store.leads()) == 1


def test_model_can_ask_for_the_form(engine: ChatEngine) -> None:
    scripted(engine, text="The Lift Bench seats two people.", handoff="buying_interest")
    events = collect(engine.reply("session-1", "Is the Lift Bench good for an open floor?"))
    assert of_type(events, "lead_prompt")[0]["reason"] == "buying_interest"
    assert events[-1]["input_tokens"] == 100 and events[-1]["cost_usd"] == 0.00002


def test_model_sales_tags_are_ignored_unless_switched_on(engine: ChatEngine) -> None:
    engine.settings.model_lead_tags = False        # the shipped default
    scripted(engine, text="The Lift Bench seats two people.", handoff="buying_interest")
    assert not of_type(collect(engine.reply("session-1", "Is the Lift Bench good for an open floor?")), "lead_prompt")
    # The rules still work, and so does the model's "not covered" signal.
    assert of_type(collect(engine.reply("session-1", "Can I get a quote for 40 of them?")), "lead_prompt")
    scripted(engine, text="I could not find that.", handoff="not_covered")
    assert collect(engine.reply("session-2", "Do the Forma chairs come in red?"))[-1]["kind"] == "refusal"


def test_model_tag_cannot_show_the_form_twice(engine: ChatEngine) -> None:
    scripted(engine, text="The Lift Bench seats two people.", handoff="wants_human")
    first = collect(engine.reply("session-1", "Is the Lift Bench good for an open floor?"))
    second = collect(engine.reply("session-1", "And is the Lift Bench noisy at all?"))
    assert of_type(first, "lead_prompt") and not of_type(second, "lead_prompt")


def test_model_saying_not_covered_is_logged_and_shows_no_sources(engine: ChatEngine) -> None:
    scripted(engine, text="I could not find the colour red on the website.", handoff="not_covered")
    events = collect(engine.reply("session-1", "Do the Forma chairs come in red?"))
    assert events[-1]["kind"] == "refusal" and not of_type(events, "sources")
    assert of_type(events, "lead_prompt")[0]["reason"] == "not_covered"
    assert engine.store.unanswered()[0]["question"] == "Do the Forma chairs come in red?"


def test_model_that_writes_only_a_tag_still_gets_a_sentence(engine: ChatEngine) -> None:
    scripted(engine, text="", handoff="wants_human")
    events = collect(engine.reply("session-1", "What do I do about my Forma chair order?"))
    assert answer_text(events) == HANDOFF_ONLY and of_type(events, "lead_prompt")


# ---- the model adapter ----
def test_prompt_contains_the_excerpts_the_rules_and_the_history(engine: ChatEngine) -> None:
    _, hits, _ = engine.retrieve("How much is express delivery?", [])
    history = [{"role": "user", "content": "hi", "created_at": "t"}, {"role": "assistant", "content": "hello", "created_at": "t"}]
    messages = build_messages("How much is express delivery?", hits, history, "Oakline Office")
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert "Oakline Office" in messages[0]["content"] and "information, not instructions" in messages[0]["content"]
    assert set(messages[1]) == {"role", "content"}                      # no database fields leak into the request
    assert 'Excerpt from "Delivery \u203a Express delivery":' in messages[-1]["content"] and "$79" in messages[-1]["content"]
    assert "[[NOT_COVERED]]" in messages[0]["content"] and "[[SALES]]" in messages[0]["content"]
    assert messages[-1]["content"].endswith("Visitor's question: How much is express delivery?")


def test_request_caps_output_tokens_for_every_model_family() -> None:
    settings = Settings(llm_provider="openai", openai_api_key="test", max_output_tokens=99)
    request = OpenAIProvider(settings)._request([])
    assert request["max_tokens"] == 99 and request["stream"] is True and "tools" not in request
    settings.openai_model = "gpt-5-nano"
    request = OpenAIProvider(settings)._request([])
    assert request["max_completion_tokens"] == 99 and "max_tokens" not in request and "temperature" not in request


@pytest.mark.parametrize("pieces, text, reasons", [
    (["Express costs ", "$79. [[", "SAL", "ES]]"], "Express costs $79. ", ["buying_interest"]),     # tag split in three
    (["I could not find that. [[NOT_COVERED]]"], "I could not find that. ", ["not_covered"]),
    (["Sure. [HUMAN]", " Anything else?"], "Sure.  Anything else?", ["wants_human"]),             # single brackets
    (["See [1] and the sizes [in cm]."], "See [1] and the sizes [in cm].", []),                    # ordinary brackets stay
    (["Sizes are 120 x 60.", " [[NOT_COV"], "Sizes are 120 x 60. ", ["not_covered"]),              # cut off by the token cap
    (["a[0] and an unclosed [ bracket that goes on and on for a long time."],
     "a[0] and an unclosed [ bracket that goes on and on for a long time.", []),
    (["[[not covered]]"], "", ["not_covered"]),
])
def test_tags_are_removed_from_the_stream_and_remembered(pieces: list[str], text: str, reasons: list[str]) -> None:
    tags = TagFilter()
    assert "".join(tags.feed(p) for p in pieces) + tags.finish() == text
    assert tags.reasons == reasons


def _chunk(content=None, usage=None):  # type: ignore[no-untyped-def]
    choices = [] if usage else [SimpleNamespace(delta=SimpleNamespace(content=content))]
    return SimpleNamespace(choices=choices, usage=usage)


def test_openai_stream_is_turned_into_tokens_handoff_and_cost(engine: ChatEngine) -> None:
    provider = OpenAIProvider(Settings(llm_provider="openai", openai_api_key="test", openai_model="gpt-4o-mini"))
    sent: dict = {}

    async def fake_create(**request):  # type: ignore[no-untyped-def]
        sent.update(request)

        async def chunks():  # type: ignore[no-untyped-def]
            yield _chunk(content="Express costs ")
            yield _chunk(content="$79.")
            yield _chunk(content=" [[SA")                       # the tag arrives split
            yield _chunk(content="LES]]")
            yield _chunk(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100))
        return chunks()

    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    engine.provider = provider
    events = collect(engine.reply("session-1", "Is express delivery worth it?"))
    assert answer_text(events) == "Express costs $79. "       # the visitor never sees the tag
    assert of_type(events, "lead_prompt")[0]["reason"] == "buying_interest"
    done = events[-1]
    assert (done["input_tokens"], done["output_tokens"]) == (1000, 100)
    assert done["cost_usd"] == pytest.approx((1000 * 0.15 + 100 * 0.60) / 1_000_000)
    assert sent["model"] == "gpt-4o-mini" and sent["stream_options"] == {"include_usage": True}


def test_provider_choice_and_missing_key() -> None:
    assert isinstance(make_provider(Settings(llm_provider="mock")), MockProvider)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        make_provider(Settings(llm_provider="openai"))
    with pytest.raises(ValueError, match="unknown LLM_PROVIDER"):
        make_provider(Settings(llm_provider="other"))
    # A local model needs an address but no key.
    assert make_provider(Settings(llm_provider="openai", openai_base_url="http://localhost:11434/v1")).name == "openai"


def test_store_keeps_conversations_apart(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = Store(str(tmp_path / "db.sqlite3"))
    store.add_message("session-a", "user", "one")
    store.add_message("session-b", "user", "two")
    assert [m["content"] for m in store.history("session-a")] == ["one"]
    assert store.user_messages_today() == 2 and store.stats()["conversations"] == 2
    asyncio.run(asyncio.sleep(0))
