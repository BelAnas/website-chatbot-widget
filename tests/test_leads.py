"""Lead rules, contact details, form validation and the signed webhook."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from chatbot.leads import (detect_intent, extract_contact, send_webhook, sign, strip_contact, validate_lead,
                           verify_signature, webhook_payload)


@pytest.mark.parametrize("message, expected", [
    ("Can I get a quote for 40 desks?", "buying_interest"),
    ("What are your bulk discounts?", "buying_interest"),
    ("We are fitting out a new office in March", "buying_interest"),
    ("I'd like to order chairs for our team", "buying_interest"),
    ("do you accept purchase orders", "buying_interest"),
    ("Can I talk to a person?", "wants_human"),
    ("please call me back", "wants_human"),
    ("I want to book a showroom visit", "wants_human"),
    # A request for a person wins over buying interest: it names the form the visitor sees.
    ("Can someone from sales contact me about 25 workstations?", "wants_human"),
    ("How long does delivery take?", None),
    ("Does the Forma Plus hold 150 kg?", None),
    ("Is the return window 30 days?", None),
    ("The desk is 120 x 60 cm, right?", None),
    ("What are the showroom opening hours?", None),
])
def test_detect_intent(message: str, expected: str | None) -> None:
    assert detect_intent(message) == expected


def test_extract_contact_finds_email_and_phone() -> None:
    assert extract_contact("Sure: Jane.Doe@Acme.co.uk, phone +1 (512) 555-0199.") == {
        "email": "jane.doe@acme.co.uk", "phone": "+1 (512) 555-0199"}
    assert extract_contact("email me at bob@x.io.") == {"email": "bob@x.io"}
    # Prices, sizes and order quantities are not phone numbers.
    assert extract_contact("the 160 x 80 desk for $689, 40 units") == {}
    assert extract_contact("write to me @home") == {}
    assert strip_contact("bob@x.io how long is the warranty?").strip() == "how long is the warranty?"


def test_validate_lead_reports_every_problem_by_field() -> None:
    clean, errors = validate_lead({"name": "  ", "email": "not-an-email", "phone": "12", "consent": False})
    assert set(errors) == {"name", "email", "phone", "consent"}
    clean, errors = validate_lead({"name": " Dana   Lee ", "email": "Dana@Northfield.Example", "phone": "(512) 555-0142",
                                   "company": None, "consent": True})
    assert errors == {}
    assert clean["name"] == "Dana Lee" and clean["email"] == "dana@northfield.example" and clean["company"] == ""
    assert "message" in validate_lead({"name": "A", "email": "a@b.co", "consent": True, "message": "x" * 1001})[1]


def test_signature_accepts_only_the_right_secret_body_and_a_fresh_timestamp() -> None:
    body, now = b'{"event":"lead.created"}', str(int(time.time()))
    signature = sign("secret", now, body)
    assert verify_signature("secret", now, body, signature)
    assert not verify_signature("other-secret", now, body, signature)
    assert not verify_signature("secret", now, body + b" ", signature)
    old = str(int(time.time()) - 3600)
    assert not verify_signature("secret", old, body, sign("secret", old, body))   # replayed an hour later
    assert not verify_signature("secret", "not-a-number", body, signature)


LEAD = {"id": 7, "name": "Dana", "email": "dana@northfield.example", "phone": "", "company": "", "message": "",
        "reason": "buying_interest", "interest": "Standing desks", "source": "form", "consent": 1, "page_url": "",
        "created_at": "2026-01-01 10:00:00", "webhook_status": "x", "session_id": "s",
        "transcript": [{"role": "user", "content": "quote please", "created_at": "t"}]}


def _send(responses: list[int | Exception], secret: str = "secret") -> tuple[tuple[str, int], list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        outcome = responses[len(seen) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome)

    async def run() -> tuple[str, int]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await send_webhook("https://crm.example/hook", secret, webhook_payload(LEAD, "Shop"),
                                      attempts=3, backoff_s=0, client=client)

    return asyncio.run(run()), seen


def test_webhook_is_signed_and_carries_the_lead_and_transcript() -> None:
    (status, attempts), seen = _send([200])
    assert (status, attempts) == ("delivered", 1)
    request = seen[0]
    assert verify_signature("secret", request.headers["X-Webhook-Timestamp"], request.content, request.headers["X-Webhook-Signature"])
    payload = json.loads(request.content)
    assert payload["event"] == "lead.created" and payload["site"] == "Shop"
    assert payload["lead"]["email"] == "dana@northfield.example" and "webhook_status" not in payload["lead"]
    assert payload["transcript"] == [{"role": "user", "content": "quote please"}]


def test_webhook_retries_server_errors_and_network_failures() -> None:
    assert _send([500, httpx.ConnectError("down"), 204])[0] == ("delivered", 3)
    assert _send([503, 503, 503])[0] == ("failed: HTTP 503", 3)
    assert _send([httpx.ConnectError("down")] * 3)[0] == ("failed: ConnectError", 3)


def test_webhook_does_not_retry_when_the_receiver_rejects_the_request() -> None:
    (status, attempts), seen = _send([400, 200])
    assert (status, attempts) == ("failed: HTTP 400", 1) and len(seen) == 1


def test_webhook_without_a_secret_is_sent_unsigned() -> None:
    _, seen = _send([200], secret="")
    assert "X-Webhook-Signature" not in seen[0].headers
