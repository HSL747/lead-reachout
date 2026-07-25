from __future__ import annotations
import logging
import os
from datetime import datetime

log = logging.getLogger(__name__)


def _get_conn():
    url = os.environ.get("RAILWAY_DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "RAILWAY_DATABASE_URL not set in .env — "
            "copy the DATABASE_URL from your Railway PostgreSQL service."
        )
    import psycopg2
    return psycopg2.connect(url)


def init_queue() -> None:
    """Create the send_queue table on Railway if it doesn't exist yet."""
    conn = _get_conn()
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
    conn.close()


def enqueue(
    to_email: str,
    subject: str,
    body_plain: str,
    body_html: str | None,
    send_at: datetime,
    lead_ref: str = "",
) -> None:
    """Add an email to the Railway send queue."""
    conn = _get_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO send_queue
                       (to_email, subject, body_plain, body_html, send_at, lead_ref)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (to_email, subject, body_plain, body_html, send_at, lead_ref),
            )
    conn.close()
    log.debug("Queued %s for %s", to_email, send_at.isoformat())
