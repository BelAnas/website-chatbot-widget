"""The crawler: what it keeps, what it drops, and where it is allowed to go."""
from __future__ import annotations

import httpx

from chatbot.chunking import MAX_WORDS, chunk_pages
from chatbot.crawl import Page, Section, crawl, extract, load_directory

from .conftest import DEMO

PAGE = """<!doctype html><html><head><title>Delivery | Shop</title>
<style>.x{color:red}</style><script>var secret = "tracking code";</script></head><body>
<header><nav><a href="/a.html">Menu item</a><a href="https://other.example/x">Partner</a></nav></header>
<div class="cookie-banner">We use cookies</div>
<main><h1>Delivery</h1><p>We deliver &amp; install.</p>
<h2 id="costs">Costs</h2><p>Standard delivery costs $39.</p><ul><li>Free over $500</li><li>Express: $79</li></ul>
<h3>Small print</h3><p>Prices exclude tax.<br>Subject to change.</p>
<aside>Subscribe to our newsletter</aside><form><input name="q"><button>Search</button></form>
</main><footer><p>Copyright Shop</p></footer></body></html>"""


def test_extract_keeps_content_and_drops_layout() -> None:
    page = extract(PAGE, "https://shop.example/delivery.html")
    text = " ".join(s.text for s in page.sections)
    assert page.title == "Delivery"
    assert "We deliver & install." in text and "Standard delivery costs $39." in text
    for noise in ("Menu item", "cookies", "tracking code", "newsletter", "Copyright", "Search", "color:red"):
        assert noise not in text


def test_extract_makes_one_section_per_heading_with_its_anchor() -> None:
    page = extract(PAGE, "https://shop.example/delivery.html")
    assert [(s.heading, s.anchor) for s in page.sections] == [("", ""), ("Costs", "costs"), ("Small print", "")]
    # List items and line breaks end a sentence, so they do not run together.
    assert page.sections[1].text == "Standard delivery costs $39. Free over $500. Express: $79."
    assert page.sections[2].text == "Prices exclude tax. Subject to change."


def test_extract_collects_links_even_from_the_menu() -> None:
    page = extract(PAGE, "https://shop.example/delivery.html")
    assert page.links == ["https://shop.example/a.html", "https://other.example/x"]


def test_extract_without_main_tag_and_with_unclosed_tags() -> None:
    html = ("<html><title>FAQ - Shop</title><body><nav>Home<p>Deep menu</nav><div id='sidebar'>Ads<p>More ads</div>"
            "<h2>Returns</h2><p>Thirty days<li>No fee<h2>Other</h2><p>Text</body></html>")
    page = extract(html, "https://shop.example/faq")
    assert page.title == "FAQ"
    assert [s.heading for s in page.sections] == ["Returns", "Other"]
    assert "menu" not in page.sections[0].text and "ads" not in page.sections[0].text.lower()
    assert "Thirty days" in page.sections[0].text


def test_demo_site_loads_clean() -> None:
    pages = load_directory(DEMO, "/demo")
    assert len(pages) == 14
    text = " ".join(s.text for p in pages for s in p.sections)
    for noise in ("cookies", "Newsletter", "analyticsQueue", "fictional company", "Subscribe"):
        assert noise not in text
    delivery = next(p for p in pages if p.url == "/demo/delivery.html")
    assert delivery.title == "Delivery" and delivery.sections[1].anchor == "standard-delivery"


def _site(routes: dict[str, tuple[str, str]]) -> httpx.Client:
    """A fake website: path -> (content type, body). Records which paths were requested."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path if request.url.host == "shop.example" else str(request.url))
        if request.url.host != "shop.example" or request.url.path not in routes:
            return httpx.Response(404)
        content_type, body = routes[request.url.path]
        return httpx.Response(200, headers={"content-type": content_type}, text=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.requested = requested  # type: ignore[attr-defined]
    return client


def _html(title: str, body: str = "") -> tuple[str, str]:
    return "text/html", f"<html><body><main><h1>{title}</h1><p>About {title}.</p>{body}</main></body></html>"


def test_crawl_uses_the_sitemap_and_obeys_robots() -> None:
    client = _site({
        "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /private\nSitemap: https://shop.example/map.xml\n"),
        "/map.xml": ("application/xml", "<urlset><url><loc>https://shop.example/a</loc></url>"
                     "<url><loc>https://shop.example/private/b</loc></url><url><loc>https://other.example/c</loc></url>"
                     "<url><loc>https://shop.example/logo.png</loc></url></urlset>"),
        "/a": _html("A", '<a href="/not-in-sitemap">x</a>'),
        "/private/b": _html("B"),
        "/not-in-sitemap": _html("N"),
    })
    pages = crawl("https://shop.example/", delay_s=0, client=client)
    assert [p.title for p in pages] == ["A"]
    requested = client.requested  # type: ignore[attr-defined]
    assert "/private/b" not in requested          # forbidden by robots.txt
    assert not any("other.example" in r for r in requested)   # another website
    assert "/logo.png" not in requested and "/not-in-sitemap" not in requested


def test_crawl_follows_links_when_there_is_no_sitemap() -> None:
    client = _site({
        "/": _html("Home", '<a href="/a">a</a> <a href="https://other.example/z">z</a> <a href="/file.pdf">pdf</a>'),
        "/a": _html("A", '<a href="/b#part">b</a> <a href="/">home</a>'),
        "/b": _html("B"),
        "/data": ("application/json", "{}"),
    })
    pages = crawl("https://shop.example/", delay_s=0, client=client)
    assert [p.title for p in pages] == ["Home", "A", "B"]
    assert crawl("https://shop.example/", max_pages=2, delay_s=0, client=client)[-1].title == "A"


def test_long_sections_are_split_at_sentence_ends() -> None:
    long_text = " ".join(f"Sentence number {i} has exactly seven words." for i in range(60))
    page = Page("https://shop.example/p", "Guide", [Section("Part", "part", long_text), Section("", "", "Short intro.")])
    chunks = chunk_pages([page])
    assert len(chunks) > 3 and all(len(c.text.split()) <= MAX_WORDS for c in chunks)
    assert all(c.text.endswith(".") for c in chunks)
    assert chunks[0].url == "https://shop.example/p#part" and chunks[0].label == "Guide › Part"
    assert chunks[-1].url == "https://shop.example/p" and chunks[-1].label == "Guide"
