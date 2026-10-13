"""Shared fixtures. Everything here is offline: hash embedder, mock answer writer, temp database."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.config import Settings
from chatbot.crawl import load_directory
from chatbot.embed import HashEmbedder
from chatbot.engine import ChatEngine
from chatbot.index import SiteIndex
from chatbot.llm import MockProvider
from chatbot.store import Store

DEMO = Path(__file__).resolve().parent.parent / "demo_site"


def collect(stream) -> list[dict]:  # type: ignore[no-untyped-def]
    """Runs one engine reply to the end and returns its events."""
    async def run() -> list[dict]:
        return [event async for event in stream]
    return asyncio.run(run())


def answer_text(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "token")


def of_type(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["type"] == kind]


@pytest.fixture(scope="session")
def index() -> SiteIndex:
    return SiteIndex.build(load_directory(DEMO, "/demo"), HashEmbedder(), str(DEMO))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(embedder="hash", llm_provider="mock", mock_delay_ms=0, db_path=str(tmp_path / "chat.sqlite3"),
                    index_dir=str(tmp_path / "index"), admin_token="test-token")


@pytest.fixture
def engine(settings: Settings, index: SiteIndex) -> ChatEngine:
    return ChatEngine(settings, index, Store(settings.db_path), MockProvider())


@pytest.fixture
def client(settings: Settings, engine: ChatEngine):  # type: ignore[no-untyped-def]
    with TestClient(create_app(settings, engine)) as test_client:
        yield test_client
