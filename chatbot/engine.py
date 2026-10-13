"""The conversation engine: one visitor message in, a stream of events out.

Events (each is a dict with a "type"):
  token        a piece of the answer
  sources      links to the website sections the answer came from
  lead_prompt  the widget should show the contact form
  done         the reply is complete; carries timings, token counts and cost
"""
from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator

from .config import Settings
from .index import Hit, SiteIndex
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

REFUSAL = ("I could not find that on the {site} website, so I would rather not guess. "
           "Leave your details and someone from the team will answer you directly.")

# Words that point back at the previous question: "how much is it?", "and the Pro?".
_REFERS_BACK = re.compile(r"\b(it|its|that|this|those|these|they|them|one|ones|same)\b|^(and|what about|how about|also)\b", re.I)


def search_query(message: str, history: list[dict]) -> str:
    """Adds the previous question when the new one does not stand on its own.

    "How much is it?" finds nothing by itself. Joined with "Tell me about the Lift Pro" it
    finds the right page. This costs no model call, unlike asking a model to rewrite the question.
    """
    previous = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    if previous and (len(tokenize(message)) <= 3 or _REFERS_BACK.search(message)):
        return f"{previous} {message}"
    return message


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
        self.provider = provider or make_provider(settings)
        self.min_relevance = (settings.min_relevance if settings.min_relevance is not None
                              else index.embedder.default_min_relevance)

    def retrieve(self, message: str, history: list[dict]) -> tuple[str, list[Hit], bool]:
        """Returns the query used, the passages found, and whether the site covers the question."""
        query = search_query(message, history)
        hits = self.index.search(query, self.settings.search_mode, self.settings.top_k)
        # The scope gate. It looks at similarity of meaning, not at the fused rank score,
        # because a rank says which passage is best, not whether any passage is good.
        in_scope = bool(hits) and max(h.cosine for h in hits) >= self.min_relevance
        return query, hits, in_scope

    async def reply(self, session_id: str, message: str, page_url: str = "") -> AsyncIterator[dict]:
        started = time.perf_counter()
        first_token_ms: float | None = None
        site = self.settings.site_name
        self.store.touch_session(session_id, page_url)
        # History is read from our own database, never taken from the browser: a visitor
        # cannot plant a fake assistant message to steer the model.
        history = self.store.history(session_id, self.settings.max_history_turns * 2)
        self.store.add_message(session_id, "user", message)

        text, sources, usage, kind = "", [], {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}, "answer"
        canned = small_talk(message, site)
        if canned is not None:
            kind = "small_talk"
        else:
            query, hits, in_scope = self.retrieve(message, history)
            if not in_scope:
                kind, canned = "refusal", REFUSAL.format(site=site)
                self.store.add_unanswered(session_id, message, max((h.cosine for h in hits), default=None))

        if canned is not None:
            for piece in re.findall(r"\S+\s*", canned):
                first_token_ms = first_token_ms or (time.perf_counter() - started) * 1000
                yield {"type": "token", "text": piece}
            text = canned
        else:
            used: list[int] | None = None
            async for event in self.provider.stream(message, query, hits, history, site):
                if event["type"] == "token":
                    first_token_ms = first_token_ms or (time.perf_counter() - started) * 1000
                    text += event["text"]
                    yield event
                elif event["type"] == "used":
                    used = event["chunk_ids"]
                elif event["type"] == "usage":
                    usage = event
            shown = [h for h in hits if h.chunk.id in used] if used else hits
            sources = to_sources(shown)
            yield {"type": "sources", "sources": sources}

        self.store.add_message(session_id, "assistant", text, {"kind": kind, "sources": sources})
        yield {"type": "done", "kind": kind, "provider": self.provider.name,
               "first_token_ms": round(first_token_ms or 0, 1),
               "total_ms": round((time.perf_counter() - started) * 1000, 1),
               "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
               "cost_usd": usage["cost_usd"]}
