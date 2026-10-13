"""The conversation engine: one visitor message in, a stream of events out.

Events (each is a dict with a "type"):
  token        a piece of the answer
  sources      links to the website sections the answer came from
  lead_prompt  the widget should show the contact form
  lead_saved   the visitor typed an email address in the chat and it was stored as a lead
  done         the reply is complete; carries timings, token counts and cost
"""
from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Callable

from .config import Settings
from .index import Hit, SiteIndex
from .leads import PROMPTS, detect_intent, extract_contact, strip_contact
from .llm import Provider, make_provider
from .store import Store
from .text import tokenize

# Messages that need no search and no model call.
_SMALL_TALK: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(hi|hello|hey|hiya|good (morning|afternoon|evening))( there)?[\s!.,]*$", re.I),
     "Hello! I can answer questions about {site}: products, prices, delivery, returns and more. What would you like to know?"),
    (re.compile(r"^(thanks|thank you|thx|cheers|great,? thanks|ok,? thanks|perfect)( a lot| very much| so much)?[\s!.,]*$", re.I),
     "You're welcome! Ask me anything else about {site}."),
    (re.compile(r"^(bye|goodbye|see you|that'?s all)[\s!.,]*$", re.I),
     "Goodbye, and thanks for visiting {site}."),
    (re.compile(r"\b(are you (a |an )?(bot|robot|human|real person|ai|person)|who are you|what are you|am i (talking|speaking|chatting) (to|with) a)\b", re.I),
     "I'm an automated assistant for {site}. I answer from the pages of this website, and I can pass you to the team whenever you want a person."),
]

REFUSAL = "I could not find that on the {site} website, so I would rather not guess."
LEAD_THANKS = "Thank you. I have passed {email} to the team together with this conversation, and someone will contact you."
HANDOFF_ONLY = "Of course, I can pass this to the team."
ALREADY_PASSED = "The team already has your details and this conversation, and will be in touch."
# Words people wrap around their contact details: "sure, my email is ... and my phone is ...".
_CONTACT_WORDS = frozenset(tokenize("sure s email e mail address phone number mobile cell name here reach call contact best it's"))
# When several signals fire on one message, the strongest one names the form.
_REASON_RANK = {"wants_human": 3, "buying_interest": 2, "not_covered": 1}

# A message that opens like this continues the previous question whatever it matches.
_CONTINUES = re.compile(r"^\s*(and|what about|how about|also|same for|or)\b", re.I)


def small_talk(message: str, site_name: str) -> str | None:
    for pattern, reply in _SMALL_TALK:
        if pattern.search(message.strip()):
            return reply.format(site=site_name)
    return None


def to_sources(hits: list[Hit], limit: int = 2) -> list[dict]:
    """One link per website section, best first."""
    seen, out = set(), []
    for hit in hits:
        if hit.chunk.url not in seen:
            seen.add(hit.chunk.url)
            out.append({"label": hit.chunk.label, "url": hit.chunk.url})
    return out[:limit]


class ChatEngine:
    def __init__(self, settings: Settings, index: SiteIndex, store: Store, provider: Provider | None = None) -> None:
        self.settings = settings
        self.index = index
        self.store = store
        self.provider = provider or make_provider(settings, index.embedder)
        self.min_relevance = (settings.min_relevance if settings.min_relevance is not None
                              else index.embedder.default_min_relevance)
        # Called with the lead id when a lead is created inside a chat turn (the API sends the webhook).
        self.on_lead: Callable[[int], None] | None = None

    def retrieve(self, message: str, history: list[dict]) -> tuple[str, list[Hit], bool]:
        """Returns the query used, the passages found, and whether the site covers the question.

        Follow-ups: "How much is it?" finds nothing useful alone. So the message is searched
        alone first, and if it does not clearly match a page, it is searched again together
        with the visitor's previous question. A message that matches well alone is a new
        topic and is left alone. This costs a few milliseconds and no model call, unlike
        asking a model to rewrite the question.
        """
        query = message
        hits = self.index.search(query, self.settings.search_mode, self.settings.top_k)
        previous = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        best = max((h.cosine for h in hits), default=0.0)
        if previous and (best < self.index.embedder.standalone_relevance or _CONTINUES.match(message)):
            query = f"{previous} {message}"
            hits = self.index.search(query, self.settings.search_mode, self.settings.top_k)
        # The scope gate. It looks at similarity of meaning, not at the fused rank score,
        # because a rank says which passage is best, not whether any passage is good.
        in_scope = bool(hits) and max(h.cosine for h in hits) >= self.min_relevance
        return query, hits, in_scope

    async def reply(self, session_id: str, message: str, page_url: str = "") -> AsyncIterator[dict]:
        started = time.perf_counter()
        first_token_ms: float | None = None
        site = self.settings.site_name
        session = self.store.touch_session(session_id, page_url)
        # History is read from our own database, never taken from the browser: a visitor
        # cannot plant a fake assistant message to steer the model.
        history = self.store.history(session_id, self.settings.max_history_turns * 2)
        self.store.add_message(session_id, "user", message)

        text, sources, kind = "", [], "answer"
        usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        reasons: list[str] = []
        has_lead = bool(session["lead_id"])

        def say(canned: str) -> list[dict]:
            nonlocal text, first_token_ms
            first_token_ms = first_token_ms or (time.perf_counter() - started) * 1000
            text += canned
            return [{"type": "token", "text": piece} for piece in re.findall(r"\S+\s*", canned)]

        # 1. An email address typed into the chat is a lead, with or without the form.
        question = message
        contact = extract_contact(message)
        if contact.get("email") and not has_lead:
            lead_id = self.store.add_lead(
                session_id, contact["email"], phone=contact.get("phone", ""), message=message, source="chat",
                reason=session["lead_reason"] or "typed in chat", interest=self.store.session_topics(session_id),
                page_url=page_url)
            has_lead = True
            for event in say(LEAD_THANKS.format(email=contact["email"])):
                yield event
            yield {"type": "lead_saved", "lead_id": lead_id}
            if self.on_lead:
                self.on_lead(lead_id)
            question = strip_contact(message)
            if len([t for t in tokenize(question) if t not in _CONTACT_WORDS]) < 3:
                question = ""          # nothing but contact details: no search, no model call
            else:
                text += " "
                yield {"type": "token", "text": " "}
            kind = "lead_saved"

        # 2. Answer the question: small talk, refusal, or a grounded answer from the model.
        canned = small_talk(question, site) if question else None
        if canned is not None:
            kind = "small_talk"
            for event in say(canned):
                yield event
        elif question:
            query, hits, in_scope = self.retrieve(question, history)
            if not in_scope and detect_intent(question):
                # "Can I talk to someone?" is not a question the pages answer, and not a gap in
                # the content either: agree and let the form below do the rest.
                kind = "handoff"
                if not has_lead:
                    for event in say(HANDOFF_ONLY):
                        yield event
                else:
                    for event in say(ALREADY_PASSED):
                        yield event
            elif not in_scope:
                self.store.add_unanswered(session_id, question, max((h.cosine for h in hits), default=None))
                if kind != "lead_saved":   # the thank-you already promised a person
                    kind = "refusal"
                    reasons.append("not_covered")
                    for event in say(REFUSAL.format(site=site)):
                        yield event
            else:
                kind = "answer"
                used: list[int] | None = None
                wrote = False
                async for event in self.provider.stream(question, query, hits, history, site):
                    if event["type"] == "token":
                        first_token_ms = first_token_ms or (time.perf_counter() - started) * 1000
                        text += event["text"]
                        wrote = True
                        yield event
                    elif event["type"] == "used":
                        used = event["chunk_ids"]
                    elif event["type"] == "handoff":
                        reasons.append(event["reason"])
                    elif event["type"] == "usage":
                        usage = event
                if not wrote:              # the model only called the tool and wrote nothing
                    for event in say(HANDOFF_ONLY):
                        yield event
                if "not_covered" in reasons:
                    # The gate let the question through but the model found no answer in the passages.
                    kind = "refusal"
                    self.store.add_unanswered(session_id, question, max(h.cosine for h in hits))
                else:
                    sources = to_sources([h for h in hits if h.chunk.id in used] if used else hits)
                    yield {"type": "sources", "sources": sources}

        # 3. Offer the contact form. The rules always run; the model can only add to them.
        if kind != "small_talk":
            rule = detect_intent(message)
            if rule:
                reasons.append(rule)
        reason = max(reasons, key=_REASON_RANK.__getitem__) if reasons else None
        # Shown once per conversation, so the bot does not nag. An explicit request for a person
        # always gets the form again.
        if reason and not has_lead and (not session["lead_prompted"] or reason == "wants_human"):
            self.store.mark_lead_prompted(session_id, reason)
            yield {"type": "lead_prompt", "reason": reason, "text": PROMPTS[reason]}

        self.store.add_message(session_id, "assistant", text, {"kind": kind, "sources": sources})
        yield {"type": "done", "kind": kind, "provider": self.provider.name,
               "first_token_ms": round(first_token_ms or 0, 1),
               "total_ms": round((time.perf_counter() - started) * 1000, 1),
               "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
               "cost_usd": usage["cost_usd"]}
