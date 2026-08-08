"""Railway cron worker — checks the send queue every 5 minutes and sends due emails
via the Gmail API (HTTPS) instead of SMTP, so it works inside Railway's network."""
import base64
import json
import os
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import psycopg2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

DATABASE_URL  = os.environ["DATABASE_URL"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]


def _get_service(conn):
    """Load Gmail OAuth token from the settings table in Railway PostgreSQL."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = 'gmail_token_json'")
        row = cur.fetchone()
    if not row:
        raise RuntimeError("gmail_token_json not found in settings table — run setup_worker_token.py")
    creds = Credentials.from_authorized_user_info(json.loads(row[0]))
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def _send(service, to_email: str, subject: str, body_plain: str, body_html: str | None) -> None:
    msg = MIMEMultipart("alternative")
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body_plain, "plain"))
    if body_html:
        msg.attach(MIMEText(body_html, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def _ensure_table(conn) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS send_queue (
                    id          SERIAL      PRIMARY KEY,
                    to_email    TEXT        NOT NULL,
                    subject     TEXT        NOT NULL,
                    body_plain  TEXT        NOT NULL,
                    body_html   TEXT,
                    send_at     TIMESTAMPTZ NOT NULL,
                    sent_at     TIMESTAMPTZ,
                    lead_ref    TEXT,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)


def run() -> None:
    now  = datetime.now(timezone.utc)
    conn = psycopg2.connect(DATABASE_URL)
    _ensure_table(conn)

    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, to_email, subject, body_plain, body_html
               FROM send_queue
               WHERE send_at <= %s AND sent_at IS NULL
               ORDER BY send_at ASC""",
            (now,),
        )
        rows = cur.fetchall()

    if not rows:
        print("Nothing to send.")
        conn.close()
        return

    service = _get_service(conn)
    sent = errors = 0
    for row_id, to_email, subject, body_plain, body_html in rows:
        try:
            _send(service, to_email, subject, body_plain, body_html)
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE send_queue SET sent_at = %s WHERE id = %s",
                        (now, row_id),
                    )
            print(f"Sent  : {to_email} — {subject}")
            sent += 1
        except Exception as exc:
            print(f"Error : {to_email} — {exc}")
            errors += 1

    conn.close()
    print(f"Done — sent: {sent}, errors: {errors}")


if __name__ == "__main__":
    run()
