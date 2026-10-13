"""Command line: python -m chatbot.cli <command>.

serve    start the service, the demo website and the widget
ingest   read a website (folder or address) and build the search index
search   show which passages a question finds
ask      ask one question in the terminal
leads    list the captured leads
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .config import Settings
from .crawl import load_site
from .embed import make_embedder
from .index import SiteIndex


def build_index(settings: Settings, source: str | None = None, quiet: bool = False) -> SiteIndex:
    source = source or settings.site_source
    pages = load_site(source, settings.site_base_url, settings.max_pages)
    index = SiteIndex.build(pages, make_embedder(settings.embedder, settings.embedding_model), source)
    index.save(settings.index_dir)
    if not quiet:
        m = index.meta
        print(f"indexed {m['pages']} pages, {m['passages']} passages, {m['words']} words from {source} ({m['embedder']})")
    return index


def get_index(settings: Settings) -> SiteIndex:
    """Loads the saved index, or builds it on first use so that one command is enough."""
    index = SiteIndex.load(settings.index_dir, make_embedder(settings.embedder, settings.embedding_model))
    return index or build_index(settings, quiet=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chatbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve"); p.add_argument("--port", type=int); p.add_argument("--host", default="127.0.0.1")
    p = sub.add_parser("ingest"); p.add_argument("source", nargs="?", help="folder of .html files or https:// address")
    p = sub.add_parser("search"); p.add_argument("question"); p.add_argument("--mode", default=None); p.add_argument("-k", type=int, default=4)
    p = sub.add_parser("ask"); p.add_argument("question")
    sub.add_parser("leads")
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command == "ingest":
        build_index(settings, args.source)
    elif args.command == "search":
        for hit in get_index(settings).search(args.question, args.mode or settings.search_mode, args.k):
            print(f"{hit.score:.4f}  meaning {hit.cosine:.2f}  keywords {hit.keyword:5.2f}  {hit.chunk.label}\n        {hit.chunk.text[:110]}")
    elif args.command == "ask":
        from .engine import ChatEngine
        from .store import Store

        engine = ChatEngine(settings, get_index(settings), Store(":memory:"))

        async def run() -> None:
            async for event in engine.reply("cli-session", args.question):
                if event["type"] == "token":
                    print(event["text"], end="", flush=True)
                elif event["type"] == "sources":
                    print("\n" + "\n".join(f"  source: {s['label']}  {s['url']}" for s in event["sources"]), end="")
                elif event["type"] == "lead_prompt":
                    print(f"\n  [contact form offered: {event['reason']}]", end="")
            print()

        asyncio.run(run())
    elif args.command == "leads":
        from .store import Store

        for lead in Store(settings.db_path).leads():
            print(f"{lead['created_at']}  {lead['name']} <{lead['email']}>  {lead['reason']}  webhook={lead['webhook_status']}")
    elif args.command == "serve":
        import uvicorn

        from .api import create_app

        port = args.port or settings.port
        uvicorn.run(create_app(settings), host=args.host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
