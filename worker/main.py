"""Railway cron worker — checks the send queue every 5 minutes and sends due emails."""
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import psycopg2

DATABASE_URL     = os.environ["DATABASE_URL"]
GMAIL_ADDRESS    = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PW     = os.environ["GMAIL_APP_PASSWORD"]


def _send(to_email: str, subject: str, body_plain: str, body_html: str | None) -> None:
    msg = MIMEMultipart("alternative")
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body_plain, "plain"))
    if body_html:
        msg.attach(MIMEText(body_html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PW)
        smtp.sendmail(GMAIL_ADDRESS, to_email, msg.as_string())


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

    sent = errors = 0
    for row_id, to_email, subject, body_plain, body_html in rows:
        try:
            _send(to_email, subject, body_plain, body_html)
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
