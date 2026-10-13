"""Turns pages into the passages that are searched and shown to the model."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .crawl import Page
from .text import sentences

# A section longer than this is cut at sentence ends. Website sections are short, so this
# mostly matters for long articles; 120 words keeps one passage about one subject.
MAX_WORDS = 120


@dataclass
class Chunk:
    id: int
    url: str          # page address plus #anchor when the heading has one
    page_title: str
    heading: str
    text: str

    @property
    def label(self) -> str:
        return f"{self.page_title} › {self.heading}" if self.heading else self.page_title

    @property
    def indexed_text(self) -> str:
        # The title and heading are searched too: "delivery" may only appear in the page title.
        return f"{self.page_title}. {self.heading}. {self.text}" if self.heading else f"{self.page_title}. {self.text}"

    def to_dict(self) -> dict:
        return asdict(self)


def _split(text: str) -> list[str]:
    parts, current, count = [], [], 0
    for sentence in sentences(text):
        words = len(sentence.split())
        if current and count + words > MAX_WORDS:
            parts.append(" ".join(current))
            current, count = [], 0
        current.append(sentence)
        count += words
    if current:
        parts.append(" ".join(current))
    return parts


def chunk_pages(pages: list[Page]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for page in pages:
        for section in page.sections:
            url = f"{page.url}#{section.anchor}" if section.anchor else page.url
            for part in _split(section.text):
                chunks.append(Chunk(len(chunks), url, page.title, section.heading, part))
    return chunks
