"""Settings, read from environment variables (and from a local .env file if there is one)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader. Real environment variables win over the file."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float_or_none(name: str) -> float | None:
    raw = _env(name)
    return float(raw) if raw else None


@dataclass
class Settings:
    # --- the website this bot speaks for ---
    site_name: str = "Oakline Office"
    # A folder of .html files, or the address of a live site to crawl.
    site_source: str = str(ROOT / "demo_site")
    # Prefix of the links shown under an answer when the source is a folder.
    site_base_url: str = "/demo"
    max_pages: int = 200

    # --- answer writer ---
    llm_provider: str = "mock"            # mock | openai
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = ""             # empty = OpenAI; http://localhost:11434/v1 = Ollama
    max_output_tokens: int = 220
    llm_timeout_s: float = 60
    mock_delay_ms: int = 12               # pause between words, so the mock visibly streams
    # Let the model's [[SALES]] / [[HUMAN]] tags show the contact form. Off by default: on the one
    # real model measured here they fired on ordinary questions too often (see the README).
    # The lead rules and the model's [[NOT_COVERED]] tag are always active.
    model_lead_tags: bool = False

    # --- search ---
    embedder: str = "fastembed"           # fastembed | hash
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    search_mode: str = "hybrid"           # hybrid | dense | bm25
    top_k: int = 4
    min_relevance: float | None = None    # None = the embedder's measured default

    # --- protection of the owner's API bill ---
    allowed_origins: list[str] = field(default_factory=list)  # other sites allowed to embed the widget
    rate_limit_per_minute: int = 20
    max_message_chars: int = 500
    max_history_turns: int = 6
    daily_message_budget: int = 2000
    trust_proxy: bool = False             # True behind nginx or a load balancer: read X-Forwarded-For

    # --- what the widget shows ---
    greeting: str = "Hi! Ask me anything about our products, delivery or returns."
    suggested_questions: list[str] = field(default_factory=lambda: [
        "How long does delivery take?", "What is the warranty on desks?", "Can I get a quote for my team?"])

    # --- leads ---
    lead_webhook_url: str = ""
    lead_webhook_secret: str = ""
    admin_token: str = ""                 # empty = a random one is printed at start

    # --- storage ---
    db_path: str = str(ROOT / "chat.sqlite3")
    index_dir: str = str(ROOT / ".index")
    port: int = 8022

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(ROOT / ".env")
        d = cls()
        return cls(
            site_name=_env("SITE_NAME") or d.site_name,
            site_source=_env("SITE_SOURCE") or d.site_source,
            site_base_url=_env("SITE_BASE_URL", d.site_base_url),
            max_pages=int(_env("MAX_PAGES", str(d.max_pages))),
            llm_provider=_env("LLM_PROVIDER", d.llm_provider).lower(),
            openai_api_key=_env("OPENAI_API_KEY"),
            openai_model=_env("OPENAI_MODEL", d.openai_model) or d.openai_model,
            openai_base_url=_env("OPENAI_BASE_URL"),
            max_output_tokens=int(_env("MAX_OUTPUT_TOKENS", str(d.max_output_tokens))),
            llm_timeout_s=float(_env("LLM_TIMEOUT_S", str(d.llm_timeout_s))),
            model_lead_tags=_env("MODEL_LEAD_TAGS").lower() in ("1", "true", "yes", "on"),
            mock_delay_ms=int(_env("MOCK_DELAY_MS", str(d.mock_delay_ms))),
            embedder=_env("EMBEDDER", d.embedder).lower(),
            embedding_model=_env("EMBEDDING_MODEL", d.embedding_model),
            search_mode=_env("SEARCH_MODE", d.search_mode).lower(),
            top_k=int(_env("TOP_K", str(d.top_k))),
            min_relevance=_float_or_none("MIN_RELEVANCE"),
            allowed_origins=[o.strip().rstrip("/") for o in _env("ALLOWED_ORIGINS").split(",") if o.strip()],
            rate_limit_per_minute=int(_env("RATE_LIMIT_PER_MINUTE", str(d.rate_limit_per_minute))),
            max_message_chars=int(_env("MAX_MESSAGE_CHARS", str(d.max_message_chars))),
            max_history_turns=int(_env("MAX_HISTORY_TURNS", str(d.max_history_turns))),
            daily_message_budget=int(_env("DAILY_MESSAGE_BUDGET", str(d.daily_message_budget))),
            trust_proxy=_env("TRUST_PROXY").lower() in ("1", "true", "yes"),
            greeting=_env("GREETING", d.greeting),
            suggested_questions=([q.strip() for q in _env("SUGGESTED_QUESTIONS").split("|") if q.strip()]
                                 or d.suggested_questions),
            lead_webhook_url=_env("LEAD_WEBHOOK_URL"),
            lead_webhook_secret=_env("LEAD_WEBHOOK_SECRET"),
            admin_token=_env("ADMIN_TOKEN"),
            db_path=_env("DB_PATH", d.db_path),
            index_dir=_env("INDEX_DIR", d.index_dir),
            port=int(_env("PORT", str(d.port))),
        )
