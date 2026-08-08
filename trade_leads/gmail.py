from __future__ import annotations
import base64
import html as html_module
import logging
import re
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

_UK_TZ = ZoneInfo("Europe/London")

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "gmail_token.json"
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]

# Scheduling config
_DAY_START_HOUR = 8    # 8 AM
_DAY_END_HOUR   = 17   # 5 PM
_MAX_PER_DAY    = 40


def is_configured() -> bool:
    """Return True if credentials.json is present."""
    return Path(CREDENTIALS_FILE).exists()


def authenticate():
    """Return an authorised Gmail API service."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    token_path = Path(TOKEN_FILE)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, _SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds)


def _fetch_signature_html(service) -> str:
    """Return the primary send-as signature as raw HTML, or '' if unavailable."""
    try:
        result = service.users().settings().sendAs().list(userId="me").execute()
        for alias in result.get("sendAs", []):
            if alias.get("isPrimary"):
                return alias.get("signature", "")
    except Exception as exc:
        log.debug("Could not fetch Gmail signature: %s", exc)
    return ""


def _plain_to_html(text: str) -> str:
    """Convert plain text body to minimal HTML, preserving line breaks."""
    escaped = html_module.escape(text)
    return escaped.replace("\n", "<br>\n")


def _build_mime(to_email: str, subject: str, body: str, sig_html: str) -> MIMEMultipart | MIMEText:
    if sig_html:
        body_html = (
            f"<div>{_plain_to_html(body)}</div>"
            f"<div><br></div>"
            f"<div>-- </div>"
            f"<div>{sig_html}</div>"
        )
        msg: MIMEMultipart | MIMEText = MIMEMultipart("alternative")
        msg["to"] = to_email
        msg["subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(body_html, "html"))
    else:
        msg = MIMEText(body, "plain")
        msg["to"] = to_email
        msg["subject"] = subject
    return msg


def next_send_slot(already_scheduled: list[datetime]) -> datetime:
    """Return the next free UTC send slot within 8am–5pm UK time, max 40/day.

    Returns a timezone-aware UTC datetime so it compares correctly against
    Railway PostgreSQL (which stores and compares everything in UTC).
    """
    window_minutes = (_DAY_END_HOUR - _DAY_START_HOUR) * 60
    interval = timedelta(minutes=window_minutes / _MAX_PER_DAY)
    now_utc = datetime.now(timezone.utc)

    def _to_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    for days_ahead in range(7):
        uk_date   = (now_utc.astimezone(_UK_TZ) + timedelta(days=days_ahead)).date()
        start_utc = datetime(uk_date.year, uk_date.month, uk_date.day,
                             _DAY_START_HOUR, 0, tzinfo=_UK_TZ).astimezone(timezone.utc)
        end_utc   = datetime(uk_date.year, uk_date.month, uk_date.day,
                             _DAY_END_HOUR, 0, tzinfo=_UK_TZ).astimezone(timezone.utc)

        slots = [start_utc + i * interval for i in range(_MAX_PER_DAY)]
        taken = [_to_utc(t) for t in already_scheduled
                 if _to_utc(t).date() == start_utc.date()]

        for slot in slots:
            if slot < now_utc:
                continue
            if slot >= end_utc:
                break
            half = interval / 2
            if not any(abs((slot - t).total_seconds()) <= half.total_seconds() for t in taken):
                return slot

    # Fallback: first slot 7 days out
    uk_date = (now_utc.astimezone(_UK_TZ) + timedelta(days=7)).date()
    return datetime(uk_date.year, uk_date.month, uk_date.day,
                    _DAY_START_HOUR, 0, tzinfo=_UK_TZ).astimezone(timezone.utc)


def get_signature_html(service) -> str:
    """Return the user's Gmail signature HTML (empty string if unavailable)."""
    return _fetch_signature_html(service)


def build_html_body(body_plain: str, sig_html: str) -> str:
    """Combine plain text body and HTML signature into a full HTML email body."""
    body_part = f"<div>{_plain_to_html(body_plain)}</div>"
    if sig_html:
        return f"{body_part}<div><br></div><div>-- </div><div>{sig_html}</div>"
    return body_part


def create_draft(service, to_email: str, subject: str, body: str) -> str:
    """Create a Gmail draft and return the draft ID."""
    sig_html = _fetch_signature_html(service)
    msg = _build_mime(to_email, subject, body, sig_html)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    draft = service.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw}},
    ).execute()
    return draft["id"]


def schedule_send(service, to_email: str, subject: str, body: str, send_at: datetime) -> str:
    """Schedule a message via Gmail's servers. Returns the message ID.

    Requires gmail.modify scope. Gmail handles delivery at send_at — no PC needed.
    The email appears under Scheduled in Gmail and can be cancelled there.
    """
    sig_html = _fetch_signature_html(service)
    msg = _build_mime(to_email, subject, body, sig_html)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    send_at_utc = send_at.astimezone(timezone.utc)
    scheduled_iso = send_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    result = service.users().messages().send(
        userId="me",
        body={"raw": raw, "scheduledTime": scheduled_iso},
    ).execute()
    return result["id"]


def send_draft(service, draft_id: str) -> str:
    """Send a saved Gmail draft immediately. Returns the sent message ID."""
    result = service.users().drafts().send(
        userId="me",
        body={"id": draft_id},
    ).execute()
    return result["id"]
