"""Reads a website and turns each page into clean sections of text.

Two sources are supported: a live site (sitemap first, then same-site links) and a folder of
.html files. Both go through the same extractor, so what is tested offline on the folder is
what runs against a real site.
"""
from __future__ import annotations

import re
import time
import urllib.robotparser
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

USER_AGENT = "WebsiteChatbotCrawler/1.0"

# Everything inside these tags is layout or code, never page content.
_SKIP_TAGS = frozenset({"script", "style", "nav", "header", "footer", "aside", "form", "noscript",
                        "svg", "button", "template", "iframe", "select"})
# Layout blocks that are plain <div>s: recognised by their class or id.
_NOISE_ATTR = re.compile(r"cookie|consent|banner|newsletter|breadcrumb|sidebar|popup|modal|share|advert", re.I)
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                        "source", "track", "wbr"})
_BLOCK_TAGS = frozenset({"p", "div", "li", "ul", "ol", "tr", "td", "th", "table", "section", "article",
                         "br", "dd", "dt", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"})
_HEADING_TAGS = frozenset({"h2", "h3"})
_NOT_A_PAGE = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|css|js|pdf|zip|xml|json|mp4|woff2?)$", re.I)


@dataclass
class Section:
    heading: str
    anchor: str
    text: str


@dataclass
class Page:
    url: str
    title: str
    sections: list[Section] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return sum(len(s.text.split()) for s in self.sections)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class _Extractor(HTMLParser):
    """Walks the HTML once and collects the title, the links and the text under each heading."""

    def __init__(self, only_main: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.only_main = only_main          # the page has a <main>: ignore everything outside it
        self.stack: list[str] = []
        self.skip_from: int | None = None   # stack depth at which the skipped block started
        self.main_depth: int | None = None
        self.in_title = False
        self.title_tag = ""
        self.h1 = ""
        self.links: list[str] = []
        self.sections: list[Section] = [Section("", "", "")]
        self.capture: str | None = None     # "h1" or "heading" while inside one
        self.buffer: list[str] = []

    # -- helpers --
    def _visible(self) -> bool:
        if self.skip_from is not None:
            return False
        return self.main_depth is not None if self.only_main else True

    # -- parser callbacks --
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])   # links are collected even from menus: they lead to pages
        if tag == "title":
            self.in_title = True
        if tag in _VOID_TAGS:
            if tag == "br" and self._visible():
                self.sections[-1].text += " "
            return
        self.stack.append(tag)
        if self.skip_from is None and (tag in _SKIP_TAGS or _NOISE_ATTR.search(a.get("class", "") + " " + a.get("id", ""))):
            # <header> inside <article> is content in some themes, but on a product or help
            # page it is almost always the site menu, so it is dropped everywhere.
            self.skip_from = len(self.stack)
            return
        if tag == "main" and self.main_depth is None:
            self.main_depth = len(self.stack)
        if not self._visible():
            return
        if tag == "h1" and not self.h1:
            self.capture, self.buffer = "h1", []
        elif tag in _HEADING_TAGS:
            self.capture, self.buffer = "heading", []
            self.sections.append(Section("", a.get("id", ""), ""))
        elif tag in _BLOCK_TAGS:
            self.sections[-1].text += "\n"

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in _VOID_TAGS or tag not in self.stack:
            return
        # Real pages have unclosed <p> and <li>: close everything up to the matching tag.
        while self.stack:
            closed = self.stack.pop()
            depth = len(self.stack) + 1
            if self.skip_from is not None and depth == self.skip_from:
                self.skip_from = None
            if self.main_depth is not None and depth == self.main_depth:
                self.main_depth = None
            if closed == tag:
                break
        if self.capture == "h1" and tag == "h1":
            self.h1, self.capture = _clean("".join(self.buffer)), None
        elif self.capture == "heading" and tag in _HEADING_TAGS:
            self.sections[-1].heading, self.capture = _clean("".join(self.buffer)), None
        elif tag in _BLOCK_TAGS and self._visible():
            self.sections[-1].text += "\n"

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_tag += data
            return
        if not self._visible():
            return
        if self.capture:
            self.buffer.append(data)
        else:
            self.sections[-1].text += data


def extract(html: str, url: str) -> Page:
    """HTML in, a Page with one Section per heading out. Menus, footers and scripts are dropped."""
    parser = _Extractor(only_main=bool(re.search(r"<main[\s>]", html, re.I)))
    parser.feed(html)
    parser.close()
    # "Delivery | Oakline Office" -> "Delivery": the visible <h1> is the better title.
    title = parser.h1 or re.split(r"\s+[|–—-]\s+", _clean(parser.title_tag))[0] or url
    sections = []
    for s in parser.sections:
        # Each block ends a thought: make sure it ends a sentence too, so list items do not run together.
        lines = [_clean(line) for line in s.text.split("\n")]
        text = " ".join(line if line[-1] in ".!?:;" else line + "." for line in lines if line)
        if text:
            sections.append(Section(s.heading, s.anchor, text))
    links = []
    for href in parser.links:
        absolute = urldefrag(urljoin(url, href))[0]
        if absolute.startswith(("http://", "https://")) and absolute not in links:
            links.append(absolute)
    return Page(url=url, title=title, sections=sections, links=links)


def load_directory(folder: str | Path, base_url: str) -> list[Page]:
    """Reads every .html file of a folder. `base_url` is where those files are published."""
    pages = []
    for path in sorted(Path(folder).glob("*.html")):
        page = extract(path.read_text(encoding="utf-8"), f"{base_url.rstrip('/')}/{path.name}")
        if page.sections:
            pages.append(page)
    return pages


def _sitemap_urls(client: httpx.Client, sitemap_url: str, depth: int = 0) -> list[str]:
    try:
        r = client.get(sitemap_url)
    except httpx.HTTPError:
        return []
    if r.status_code != 200:
        return []
    locs = [m.strip() for m in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", r.text)]
    if "<sitemapindex" in r.text and depth < 2:
        return [u for loc in locs for u in _sitemap_urls(client, loc, depth + 1)]
    return locs


def crawl(start_url: str, max_pages: int = 200, delay_s: float = 0.2,
          client: httpx.Client | None = None) -> list[Page]:
    """Downloads the pages of one website.

    Order of work: read robots.txt, take the page list from the sitemap, and only if there is
    no sitemap follow links from the start page. Other hosts are never visited.
    """
    own_client = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True)
    try:
        root = urlparse(start_url)
        origin = f"{root.scheme}://{root.netloc}"
        robots = urllib.robotparser.RobotFileParser()
        sitemaps = [f"{origin}/sitemap.xml"]
        try:
            r = client.get(f"{origin}/robots.txt")
            robots.parse(r.text.splitlines() if r.status_code == 200 else [])
            sitemaps = [line.split(":", 1)[1].strip() for line in r.text.splitlines()
                        if line.lower().startswith("sitemap:")] or sitemaps
        except httpx.HTTPError:
            robots.parse([])

        def allowed(url: str) -> bool:
            p = urlparse(url)
            return p.netloc == root.netloc and not _NOT_A_PAGE.search(p.path) and robots.can_fetch(USER_AGENT, url)

        listed = [u for s in sitemaps for u in _sitemap_urls(client, s)]
        follow_links = not listed            # a sitemap is the owner's own list: trust it
        queue = deque(dict.fromkeys(listed or [start_url]))
        seen: set[str] = set()
        pages: list[Page] = []
        while queue and len(pages) < max_pages:
            url = urldefrag(queue.popleft())[0]
            if url in seen or not allowed(url):
                continue
            seen.add(url)
            try:
                r = client.get(url)
            except httpx.HTTPError:
                continue
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                continue
            page = extract(r.text, str(r.url))
            if page.sections:
                pages.append(page)
            if follow_links:
                queue.extend(link for link in page.links if link not in seen)
            if delay_s:
                time.sleep(delay_s)   # be a polite guest on someone else's server
        return pages
    finally:
        if own_client:
            client.close()


def load_site(source: str, base_url: str, max_pages: int = 200) -> list[Page]:
    """`source` is a folder or an http(s) address."""
    if source.startswith(("http://", "https://")):
        return crawl(source, max_pages=max_pages)
    return load_directory(source, base_url)
