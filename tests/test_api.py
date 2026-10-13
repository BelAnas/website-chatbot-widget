"""The HTTP service: the event stream, the guards, the lead endpoint, admin and the files."""
from __future__ import annotations

import csv
import io
import json
import re
import time

import httpx
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.guards import RateLimiter, origin_allowed
from chatbot.leads import verify_signature

from .conftest import DEMO

ADMIN = {"Authorization": "Bearer test-token"}


def ask(client: TestClient, message: str, session: str = "session-0001", **headers: str):  # type: ignore[no-untyped-def]
    return client.post("/api/chat", json={"session_id": session, "message": message}, headers=headers)


def events_of(response) -> list[dict]:  # type: ignore[no-untyped-def]
    """Parses the event stream exactly as the widget does."""
    out = []
    for block in response.text.split("\n\n"):
        name, data = re.search(r"^event: (.+)$", block, re.M), re.search(r"^data: (.+)$", block, re.M)
        if name and data:
            event = json.loads(data.group(1))
            assert event["type"] == name.group(1)
            out.append(event)
    return out


def test_chat_streams_tokens_then_sources_then_done(client: TestClient) -> None:
    response = ask(client, "How much is express delivery?")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache" and response.headers["x-accel-buffering"] == "no"
    events = events_of(response)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "token" and kinds[-2:] == ["sources", "done"]
    assert "$79" in "".join(e["text"] for e in events if e["type"] == "token")


def test_health_and_widget_config(client: TestClient) -> None:
    health = client.get("/api/health").json()
    assert health["status"] == "ok" and health["provider"] == "mock" and health["index"]["pages"] == 14
    config = client.get("/api/widget-config").json()
    assert config["site_name"] == "Oakline Office" and len(config["suggestions"]) == 3 and config["max_message_chars"] == 500


def test_bad_requests_are_rejected_before_any_work(client: TestClient) -> None:
    assert ask(client, "hello", session="short").status_code == 422
    assert ask(client, "hello", session="has spaces in it").status_code == 422
    assert ask(client, "   ").status_code == 422
    too_long = ask(client, "word " * 200)
    assert too_long.status_code == 413 and "500 characters" in too_long.json()["detail"]
    assert client.app.state.engine.store.stats()["visitor_messages"] == 0   # nothing was stored or answered


def test_rate_limit_per_visitor(client: TestClient) -> None:
    client.app.state.settings.rate_limit_per_minute = 3     # the limiter was built with the default of 20
    codes = [ask(client, "hello").status_code for _ in range(22)]
    assert codes[:20] == [200] * 20 and codes[20:] == [429, 429]
    assert int(ask(client, "hello").headers["retry-after"]) >= 1


def test_daily_budget_stops_new_messages(client: TestClient) -> None:
    client.app.state.settings.daily_message_budget = 2
    assert [ask(client, "hello").status_code for _ in range(3)] == [200, 200, 429]
    assert "limit for today" in ask(client, "hello").json()["detail"]


def test_rate_limiter_window_slides() -> None:
    limiter = RateLimiter(2, window_s=60)
    assert limiter.allow("ip", now=0)[0] and limiter.allow("ip", now=10)[0]
    allowed, wait = limiter.allow("ip", now=20)
    assert not allowed and 40 <= wait <= 41
    assert limiter.allow("other-ip", now=20)[0]            # one visitor does not block another
    assert limiter.allow("ip", now=61)[0]                  # the first call has left the window


def test_origin_rules() -> None:
    assert origin_allowed(None, "chat.example", [])                                  # curl, same-origin GET
    assert origin_allowed("https://chat.example", "chat.example", [])               # the service's own pages
    assert origin_allowed("https://www.client.com", "chat.example", ["https://www.client.com"])
    assert not origin_allowed("https://evil.example", "chat.example", ["https://www.client.com"])
    assert not origin_allowed("https://www.client.com.evil.example", "chat.example", ["https://www.client.com"])
    assert origin_allowed("https://anything.example", "chat.example", ["*"])


def test_only_allowed_websites_may_call_the_api(settings, engine) -> None:  # type: ignore[no-untyped-def]
    settings.allowed_origins = ["https://www.client.com"]
    with TestClient(create_app(settings, engine)) as client:
        refused = ask(client, "hello", Origin="https://evil.example")
        assert refused.status_code == 403 and "access-control-allow-origin" not in refused.headers
        ok = ask(client, "hello", Origin="https://www.client.com")
        assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "https://www.client.com"
        assert ok.headers["vary"] == "Origin"
        preflight = client.options("/api/chat", headers={"Origin": "https://www.client.com",
                                                         "Access-Control-Request-Method": "POST"})
        assert preflight.status_code == 204 and "POST" in preflight.headers["access-control-allow-methods"]
        assert client.options("/api/chat", headers={"Origin": "https://evil.example"}).status_code == 403
        # The widget file itself is public: the browser must be able to load it from any page.
        assert client.get("/widget.js", headers={"Origin": "https://evil.example"}).status_code == 200


LEAD = {"session_id": "session-0001", "name": "Dana Lee", "email": "dana@northfield.example",
        "company": "Northfield Logistics", "consent": True}


def test_lead_form_is_validated_field_by_field(client: TestClient) -> None:
    response = client.post("/api/leads", json={**LEAD, "name": "", "email": "nope", "consent": False})
    assert response.status_code == 422 and set(response.json()["errors"]) == {"name", "email", "consent"}
    assert client.get("/api/admin/overview", headers=ADMIN).json()["leads"] == []


def test_lead_is_stored_with_reason_topics_and_transcript(client: TestClient) -> None:
    ask(client, "How much is express delivery?")
    ask(client, "Can I get a quote for 40 of them?")
    response = client.post("/api/leads", json=LEAD)
    assert response.json() == {"ok": True, "lead_id": 1}
    overview = client.get("/api/admin/overview", headers=ADMIN).json()
    lead = overview["leads"][0]
    assert (lead["name"], lead["email"], lead["company"]) == ("Dana Lee", "dana@northfield.example", "Northfield Logistics")
    assert lead["reason"] == "buying_interest" and lead["consent"] == 1 and lead["source"] == "form"
    assert "Delivery" in lead["interest"] and len(lead["transcript"]) == 4
    assert lead["webhook_status"] == "not configured"
    assert overview["stats"] == {"conversations": 1, "visitor_messages": 2, "leads": 1, "unanswered": 0}


def test_honeypot_submission_is_dropped_silently(client: TestClient) -> None:
    response = client.post("/api/leads", json={**LEAD, "website": "http://spam.example"})
    assert response.json() == {"ok": True}
    assert client.get("/api/admin/overview", headers=ADMIN).json()["leads"] == []


def test_lead_is_sent_to_the_crm_webhook_with_a_valid_signature(settings, engine) -> None:  # type: ignore[no-untyped-def]
    settings.lead_webhook_url, settings.lead_webhook_secret = "https://crm.example/hook", "s3cret"
    received: list[httpx.Request] = []

    def crm(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(500 if len(received) == 1 else 200)   # the CRM fails once, then recovers

    app = create_app(settings, engine)
    app.state.webhook_client = httpx.AsyncClient(transport=httpx.MockTransport(crm))
    app.state.webhook_backoff_s = 0
    with TestClient(app) as client:
        client.post("/api/leads", json=LEAD)
        # An email typed into the chat goes to the CRM too.
        ask(client, "my email is bob@x.io", session="session-0002")
        for _ in range(100):
            leads = client.get("/api/admin/overview", headers=ADMIN).json()["leads"]
            if len(leads) == 2 and all(lead["webhook_status"] == "delivered" for lead in leads):
                break
            time.sleep(0.02)
    assert [lead["webhook_status"] for lead in leads] == ["delivered", "delivered"]
    assert sorted(lead["webhook_attempts"] for lead in leads) == [1, 2]
    last = received[-1]
    assert verify_signature("s3cret", last.headers["X-Webhook-Timestamp"], last.content, last.headers["X-Webhook-Signature"])
    assert {json.loads(r.content)["lead"]["email"] for r in received} == {"dana@northfield.example", "bob@x.io"}


def test_admin_needs_the_token(client: TestClient) -> None:
    assert client.get("/api/admin/overview").status_code == 401
    assert client.get("/api/admin/overview", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/admin/leads.csv?token=wrong").status_code == 401
    assert client.get("/api/admin/overview", headers=ADMIN).status_code == 200
    # The admin API is never opened to other websites, allow-list or not.
    assert "access-control-allow-origin" not in client.get("/api/admin/overview", headers={**ADMIN, "Origin": "https://x.example"}).headers


def test_csv_export_defuses_spreadsheet_formulas(client: TestClient) -> None:
    client.post("/api/leads", json={**LEAD, "name": "=HYPERLINK(\"http://evil\")", "company": ""})
    response = client.get("/api/admin/leads.csv?token=test-token")
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows[0]["name"].startswith("'=HYPERLINK") and rows[0]["company"] == "" and rows[0]["email"] == LEAD["email"]


def test_unanswered_questions_reach_the_admin_view(client: TestClient) -> None:
    ask(client, "Who won the football world cup in 2010?")
    gaps = client.get("/api/admin/overview", headers=ADMIN).json()["unanswered"]
    assert [g["question"] for g in gaps] == ["Who won the football world cup in 2010?"]


def test_model_failure_becomes_a_polite_error_event(client: TestClient) -> None:
    class Broken:
        name = "broken"

        async def stream(self, *args):  # type: ignore[no-untyped-def]
            raise RuntimeError("upstream is down")
            yield {}

    client.app.state.engine.provider = Broken()
    events = events_of(ask(client, "How much is express delivery?"))
    assert [e["type"] for e in events] == ["error"] and "upstream" not in events[0]["message"]


def test_files_the_browser_and_the_crawler_need(client: TestClient) -> None:
    widget = client.get("/widget.js")
    assert widget.status_code == 200 and "javascript" in widget.headers["content-type"]
    assert client.get("/", follow_redirects=False).headers["location"] == "/demo/index.html"
    page = client.get("/demo/delivery.html")
    assert page.status_code == 200 and '<script src="/widget.js"' in page.text
    assert client.get("/admin").status_code == 200
    assert "Sitemap: http://testserver/demo/sitemap.xml" in client.get("/robots.txt").text
    assert "<loc>http://testserver/demo/delivery.html</loc>" in client.get("/sitemap.xml").text


def test_widget_never_puts_visitor_or_model_text_into_html() -> None:
    source = (DEMO.parent / "chatbot" / "static" / "widget.js").read_text(encoding="utf-8")
    # The only innerHTML in the file is the fixed icon of the launcher button.
    assert source.count("innerHTML") == 2 and source.count(".innerHTML = '<svg") == 1
    assert "eval(" not in source and "document.write" not in source
    assert len(source.encode()) < 25_000      # stays a small file: no framework crept in
    admin = (DEMO.parent / "chatbot" / "static" / "admin.html").read_text(encoding="utf-8")
    assert ".innerHTML" not in admin
