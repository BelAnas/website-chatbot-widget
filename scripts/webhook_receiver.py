"""A tiny stand-in for a CRM: receives lead webhooks and checks their signature.

Run:  python -m scripts.webhook_receiver --port 8023 --secret change-me
Then start the chatbot with LEAD_WEBHOOK_URL=http://127.0.0.1:8023/leads LEAD_WEBHOOK_SECRET=change-me

The check in `do_POST` is the code a real receiver needs: recompute the signature over the
timestamp and the raw body, compare, and refuse anything older than five minutes.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from chatbot.leads import verify_signature


def make_handler(secret: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - name fixed by the standard library
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            valid = verify_signature(secret, self.headers.get("X-Webhook-Timestamp", ""), body,
                                     self.headers.get("X-Webhook-Signature", ""))
            if valid:
                lead = json.loads(body)["lead"]
                print(f"lead received, signature valid: {lead['name'] or '(no name)'} <{lead['email']}> "
                      f"reason={lead['reason']} interest={lead['interest']!r}", flush=True)
            else:
                print("rejected: bad or missing signature", flush=True)
            self.send_response(200 if valid else 401)
            self.end_headers()

        def log_message(self, *args: object) -> None:   # keep the terminal for the lines above
            pass

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8023)
    parser.add_argument("--secret", default="change-me")
    args = parser.parse_args()
    print(f"waiting for leads on http://127.0.0.1:{args.port}/leads", flush=True)
    HTTPServer(("127.0.0.1", args.port), make_handler(args.secret)).serve_forever()


if __name__ == "__main__":
    main()
