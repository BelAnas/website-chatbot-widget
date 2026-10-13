"""Lead capture: spotting interest, validating contact details and notifying the sales team."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time

import httpx

# --- 1. Intent rules -------------------------------------------------------------------------
# These run on every visitor message, before and regardless of the model. They are free,
# instant and explainable, and they keep lead capture working when the model is the mock or
# is down. The model can add to them through its handoff tool, but cannot switch them off.

_COUNTED_THING = (r"desks?|chairs?|units?|workstations?|people|persons?|employees|staff|seats?|lockers?|stools?|"
                  r"pedestals?|screens?|items?|pieces|sets?|users?|colleagues|students|classrooms?|offices|floors?")

_BUYING = [re.compile(p, re.I) for p in (
    r"\b(quote|quotation|estimate|proposal|pro[- ]?forma|rfq|rfp|tender)\b",
    r"\b(bulk|wholesale|volume|trade|corporate|team|group)\s+(orders?|pric\w+|discounts?|purchas\w+|rates?|deals?|buy\w*)\b",
    r"\b(discounts?|pric\w+|deals?|rates?)\s+(for|on)\s+(bulk|large|big|volume|multiple|teams?|compan\w+|schools?)\b",
    # A quantity of ten or more of something a company buys: "40 desks", "about 25 new workstations".
    rf"\b[1-9]\d{{1,3}}\+?\s+(?:\w+\s+){{0,2}}(?:{_COUNTED_THING})\b",
    r"\b(fit(ting)?[- ]?out|furnish\w*|equip(ping)?|kit(ting)? out|refurbish\w*|refit\w*|relocat\w+)\b",
    r"\b(new|second|bigger|larger) (office|branch|premises|headquarters|hq|campus|site)\b",
    r"\bfor (our|my|the|a) (whole |entire |new )?(team|company|office|school|staff|department|startup|business|employees|agency|firm|organi[sz]ation|workplace|co-?working)\b",
    r"\b(purchase orders?|procurement|purchasing department|vendor (form|registration|onboarding)|supplier (form|registration))\b",
    r"\b(place|make|put in|submit) (an?|a large|a big|my|our) order\b",
    r"\b(want|wish|like|looking|need|ready|plan(ning)?|hoping|intend(ing)?) to (buy|order|purchase|get)\b",
    r"\binterested in (buying|ordering|purchasing|getting|your)\b",
    r"\bhow (do|can) (i|we) (buy|order|purchase)\b",
    r"\b(send|give|make|email|mail) (me|us) (an?|your|the)?\s*(offer|price list|prices|pricing)\b",
    # Quantities written as words: "about a hundred desks", "fifty chairs".
    rf"\b(dozens?|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundreds?|thousands?)\s+(?:of\s+)?(?:\w+\s+){{0,2}}(?:{_COUNTED_THING})\b",
    r"\b(big|large|huge|sizeable|major|substantial) (order|purchase|project)\b",
    # A company speaking: "we need more desks", "our team is looking for chairs".
    r"\b(we|our (team|company|office|startup|school|firm))\b[^.?!]*\b(need|want|require|looking for|buying|ordering)\b[^.?!]*\b(desks|chairs|workstations|furniture|lockers|stools|screens)\b",
    r"\b(become|becoming) a (customer|client)\b|\b(open|set up|create) (an|a business|a trade|a company|a corporate) account\b",
)]

_HUMAN = [re.compile(p, re.I) for p in (
    r"\b(speak|talk|chat|speaking|talking) (to|with) (a |an |the |your |some)?(one|body|human|person|agent|rep\w*|sales\w*|advis[eo]r|consultant|manager|expert|team|real|specialist|planner)",
    r"\b(call|phone|ring|email|e-mail|contact|message|text) me\b",
    r"\b(call|ring) ?(me )?back\b|\bget back to me\b|\bget in touch\b|\breach out\b",
    r"\b(contact|reach|email|call|phone) (you|sales|someone|somebody|the team|your team|a person|support|customer service)\b",
    r"\b(book|schedule|arrange|set up|reserve|organi[sz]e|request) (a|an|the|my|our)?\s*(\w+ ){0,2}(demo|visit|appointment|tour|call|meeting|consultation|viewing|callback)\b",
    r"\b(account manager|sales (team|rep\w*|person|department|staff)|customer service|a representative)\b",
    r"\b(real|actual|live) (person|human|agent)\b|\bhuman being\b|\bto a human\b",
    r"\bwho (can|do|should|could|would) (i|we) (talk|speak|contact|call|email|ask|write)\b",
    r"\b(reps?|representatives?)\b|\b(with|to|from|contact|reach) sales\b|\b(put|get) (me|us) (in touch|through)\b",
)]


def detect_intent(message: str) -> str | None:
    """Returns "wants_human", "buying_interest" or None."""
    if any(p.search(message) for p in _HUMAN):
        return "wants_human"
    if any(p.search(message) for p in _BUYING):
        return "buying_interest"
    return None


PROMPTS = {
    "buying_interest": "Buying for a team? Leave your details and the sales team will send you a quote.",
    "wants_human": "Leave your details and someone from the team will contact you.",
    "not_covered": "Leave your details and someone from the team will answer your question.",
}

# --- 2. Contact details ----------------------------------------------------------------------
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_EMAIL_FULL = re.compile(rf"^{_EMAIL.pattern}$")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{7,17}\d(?!\w)")


def extract_contact(message: str) -> dict:
    """Finds an email address and a phone number that a visitor typed into the chat."""
    email = _EMAIL.search(message)
    phone = _PHONE.search(message)
    out = {}
    if email:
        out["email"] = email.group(0).rstrip(".").lower()
    if phone and sum(c.isdigit() for c in phone.group(0)) >= 8:
        out["phone"] = phone.group(0).strip()
    return out


def strip_contact(message: str) -> str:
    """The message without the contact details, to see whether a question is left."""
    return _PHONE.sub(" ", _EMAIL.sub(" ", message))


def validate_lead(data: dict) -> tuple[dict, dict]:
    """Returns (clean values, errors by field). The form is only stored when there are no errors."""
    clean = {k: re.sub(r"\s+", " ", str(data.get(k) or "")).strip() for k in ("name", "email", "phone", "company", "message")}
    clean["email"] = clean["email"].lower()
    errors = {}
    if not clean["name"]:
        errors["name"] = "Please enter your name."
    elif len(clean["name"]) > 100:
        errors["name"] = "The name is too long."
    if not _EMAIL_FULL.match(clean["email"]) or len(clean["email"]) > 200:
        errors["email"] = "Please enter a valid email address."
    if clean["phone"] and not 7 <= sum(c.isdigit() for c in clean["phone"]) <= 15:
        errors["phone"] = "Please enter a valid phone number, or leave it empty."
    if len(clean["company"]) > 150:
        errors["company"] = "The company name is too long."
    if len(clean["message"]) > 1000:
        errors["message"] = "The message is too long (1,000 characters at most)."
    if not data.get("consent"):
        errors["consent"] = "Please tick the box so that we may contact you."
    return clean, errors


# --- 3. Webhook ------------------------------------------------------------------------------
def sign(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over "<timestamp>.<body>". The timestamp stops an old request being replayed."""
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str, max_age_s: int = 300) -> bool:
    """What the receiving system (CRM, automation tool) runs to be sure the lead came from us."""
    try:
        fresh = abs(time.time() - int(timestamp)) <= max_age_s
    except ValueError:
        return False
    return fresh and hmac.compare_digest(sign(secret, timestamp, body), signature)


def webhook_payload(lead: dict, site_name: str) -> dict:
    fields = ("id", "name", "email", "phone", "company", "message", "reason", "interest", "source",
              "consent", "page_url", "created_at")
    return {"event": "lead.created", "site": site_name, "lead": {k: lead[k] for k in fields},
            "transcript": [{"role": m["role"], "content": m["content"]} for m in lead["transcript"]]}


async def send_webhook(url: str, secret: str, payload: dict, attempts: int = 3, backoff_s: float = 1.0,
                       client: httpx.AsyncClient | None = None) -> tuple[str, int]:
    """Posts the lead. Returns (status, attempts made). A lead is never lost: it is already stored."""
    body = json.dumps(payload, ensure_ascii=False).encode()
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=10)
    status = "failed"
    try:
        for attempt in range(1, attempts + 1):
            timestamp = str(int(time.time()))
            headers = {"Content-Type": "application/json", "X-Webhook-Timestamp": timestamp}
            if secret:
                headers["X-Webhook-Signature"] = sign(secret, timestamp, body)
            try:
                response = await client.post(url, content=body, headers=headers)
                if response.status_code < 300:
                    return "delivered", attempt
                status = f"failed: HTTP {response.status_code}"
                if response.status_code < 500 and response.status_code != 429:
                    return status, attempt   # the receiver rejected it: retrying will not help
            except httpx.HTTPError as exc:
                status = f"failed: {type(exc).__name__}"
            if attempt < attempts:
                await asyncio.sleep(backoff_s * 2 ** (attempt - 1))
        return status, attempts
    finally:
        if own_client:
            await client.aclose()
