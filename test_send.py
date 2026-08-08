"""One-shot test: queue a sample outreach email to a given address at a specific time."""
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from datetime import datetime, timezone, timedelta
from trade_leads.gmail import authenticate as gmail_authenticate, get_signature_html, build_html_body
from trade_leads.templates import TemplateManager
from trade_leads import queue as send_queue

TO_EMAIL  = "henrylucas101@icloud.com"
SEND_AT   = datetime.now(timezone.utc) + timedelta(minutes=5)   # always UTC
TRADE     = "plumber"
COMPANY   = "HL Plumbing"
AREA      = "Guildford"
HOOK      = "I came across HL Plumbing and noticed you've placed your phone number front and centre — you're clearly encouraging customers to call you directly."
SUBJECT   = "Missed calls might be costing HL Plumbing jobs"
NAME      = "Henry"

tm = TemplateManager(Path(__file__).parent / "templates")
body = tm.fill(
    1,
    company=COMPANY, trade=TRADE, trade_plural=TRADE + "s",
    area=AREA, hook=HOOK, name=NAME,
)

local_str = SEND_AT.astimezone().strftime("%a %d %b at %H:%M")
print("=" * 60)
print(f"To      : {TO_EMAIL}")
print(f"Subject : {SUBJECT}")
print(f"Send at : {local_str} (local) / {SEND_AT.strftime('%H:%M')} UTC")
print("-" * 60)
print(body)
print("=" * 60)

print("\nAuthenticating Gmail...")
service   = gmail_authenticate()
sig_html  = get_signature_html(service)
body_html = build_html_body(body, sig_html)

print("Initialising Railway queue...")
send_queue.init_queue()

print(f"Enqueueing for {local_str}...")
send_queue.enqueue(
    to_email=TO_EMAIL, subject=SUBJECT,
    body_plain=body, body_html=body_html,
    send_at=SEND_AT, lead_ref="TEST",
)

print(f"\nDone. Railway worker will send this at {local_str}.")
