from __future__ import annotations
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path("trade_leads.db")

# Pipeline columns added in v2 — kept as a constant so migrate_db and helpers stay in sync.
_PIPELINE_COLUMNS: list[tuple[str, str]] = [
    ("status",              "TEXT    DEFAULT 'not_contacted'"),
    ("draft",               "TEXT"),
    ("draft_generated_at",  "TEXT"),
    ("selected_hook",       "TEXT"),
    ("template_number",     "INTEGER"),
    ("sent_at",             "TEXT"),
    ("followup1_due",       "TEXT"),
    ("followup2_due",       "TEXT"),
    ("followup1_sent_at",   "TEXT"),
    ("followup2_sent_at",   "TEXT"),
    ("notes",               "TEXT"),
    ("response_received",   "INTEGER DEFAULT 0"),
    ("outcome",             "TEXT"),
    ("subject",             "TEXT"),
    ("gmail_draft_id",          "TEXT"),
    ("scheduled_send_at",       "TEXT"),
    ("followup1_scheduled_at",  "TEXT"),
    ("followup2_scheduled_at",  "TEXT"),
    ("followup1_draft_id",      "TEXT"),
    ("followup2_draft_id",      "TEXT"),
]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _merge_row(row: sqlite3.Row) -> dict:
    """Merge the data JSON blob with the pipeline SQL columns into one flat dict."""
    result: dict = json.loads(row["data"])
    result["_rowid"] = row["rowid"]
    result["_place_id"] = row["place_id"]
    for col, _ in _PIPELINE_COLUMNS:
        result[col] = row[col] if col in row.keys() else None
    # Ensure status always has a value (old rows have NULL before migration)
    if not result.get("status"):
        result["status"] = "not_contacted"
    return result


def init_db() -> None:
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_cache (
                cache_key  TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                place_id   TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    migrate_db()


def migrate_db() -> None:
    """Add any missing pipeline columns to the leads table without touching existing data."""
    with _conn() as conn:
        existing = _existing_columns(conn, "leads")
        for col_name, col_def in _PIPELINE_COLUMNS:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE leads ADD COLUMN {col_name} {col_def}")


# ---------------------------------------------------------------------------
# Cache helpers (unchanged from v1)
# ---------------------------------------------------------------------------

def get_cached(cache_key: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT data FROM api_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        return json.loads(row["data"]) if row else None


def set_cached(cache_key: str, data: dict) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO api_cache (cache_key, data) VALUES (?, ?)",
            (cache_key, json.dumps(data)),
        )


# ---------------------------------------------------------------------------
# Lead write helpers
# ---------------------------------------------------------------------------

def save_lead(place_id: str, lead_data: dict) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO leads (place_id, data, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(place_id) DO UPDATE SET
                data = excluded.data,
                updated_at = excluded.updated_at
            """,
            (place_id, json.dumps(lead_data)),
        )


def update_lead_pipeline(place_id: str, **fields) -> None:
    """Update any subset of pipeline columns for a lead identified by place_id."""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [place_id]
    with _conn() as conn:
        conn.execute(
            f"UPDATE leads SET {set_clause} WHERE place_id = ?", values
        )


# ---------------------------------------------------------------------------
# Lead read helpers
# ---------------------------------------------------------------------------

def get_all_leads() -> list[dict]:
    """Return all lead data blobs (v1 compat — no pipeline columns)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT data FROM leads ORDER BY updated_at DESC"
        ).fetchall()
        return [json.loads(row["data"]) for row in rows]


def get_all_leads_with_pipeline() -> list[dict]:
    """Return all leads with both business data and pipeline columns merged."""
    cols = ", ".join(col for col, _ in _PIPELINE_COLUMNS)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT rowid, place_id, data, {cols} FROM leads ORDER BY updated_at DESC"
        ).fetchall()
        return [_merge_row(row) for row in rows]


def get_lead_by_id(rowid: int) -> dict | None:
    """Fetch a single lead by its SQLite rowid (the user-facing numeric ID)."""
    cols = ", ".join(col for col, _ in _PIPELINE_COLUMNS)
    with _conn() as conn:
        row = conn.execute(
            f"SELECT rowid, place_id, data, {cols} FROM leads WHERE rowid = ?",
            (rowid,),
        ).fetchone()
        return _merge_row(row) if row else None


def get_next_uncontacted() -> dict | None:
    """Return the next lead that:
    - has status = 'not_contacted' (or NULL for pre-migration rows)
    - has a non-empty email in the data JSON
    - does NOT have email_validation_status of 'invalid' or 'do_not_mail'
    """
    cols = ", ".join(col for col, _ in _PIPELINE_COLUMNS)
    query = f"""
        SELECT rowid, place_id, data, {cols}
        FROM leads
        WHERE COALESCE(status, 'not_contacted') = 'not_contacted'
          AND COALESCE(json_extract(data, '$.email'), '') != ''
          AND COALESCE(json_extract(data, '$.email_validation_status'), '') NOT IN ('invalid', 'do_not_mail')
        ORDER BY updated_at ASC
        LIMIT 1
    """
    with _conn() as conn:
        row = conn.execute(query).fetchone()
        return _merge_row(row) if row else None


def get_due_followups() -> list[dict]:
    """Return leads where follow-up 1 or 2 is due today or overdue and not yet sent."""
    today = date.today().isoformat()
    cols = ", ".join(col for col, _ in _PIPELINE_COLUMNS)
    query = f"""
        SELECT rowid, place_id, data, {cols}
        FROM leads
        WHERE
            (followup1_due IS NOT NULL AND followup1_due <= ? AND followup1_sent_at IS NULL)
            OR
            (followup2_due IS NOT NULL AND followup2_due <= ? AND followup2_sent_at IS NULL)
        ORDER BY
            COALESCE(
                CASE WHEN followup1_due IS NOT NULL AND followup1_sent_at IS NULL THEN followup1_due END,
                CASE WHEN followup2_due IS NOT NULL AND followup2_sent_at IS NULL THEN followup2_due END
            ) ASC
    """
    with _conn() as conn:
        rows = conn.execute(query, (today, today)).fetchall()
        return [_merge_row(row) for row in rows]


def get_status_summary() -> dict:
    """Aggregate counts for the status command."""
    today = date.today().isoformat()

    with _conn() as conn:
        # Total
        total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]

        # Count by pipeline status
        status_rows = conn.execute("""
            SELECT COALESCE(status, 'not_contacted') AS s, COUNT(*) AS cnt
            FROM leads
            GROUP BY COALESCE(status, 'not_contacted')
        """).fetchall()
        status_counts: dict[str, int] = {row["s"]: row["cnt"] for row in status_rows}

        # Follow-up overdue counts
        fu1_due = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE followup1_due <= ? AND followup1_sent_at IS NULL AND followup1_due IS NOT NULL",
            (today,),
        ).fetchone()[0]
        fu2_due = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE followup2_due <= ? AND followup2_sent_at IS NULL AND followup2_due IS NOT NULL",
            (today,),
        ).fetchone()[0]

        # Next 5 follow-ups (across both follow-up slots)
        next_fu_rows = conn.execute(f"""
            SELECT rowid, data, followup1_due, followup2_due, followup1_sent_at, followup2_sent_at
            FROM leads
            WHERE
                (followup1_due IS NOT NULL AND followup1_sent_at IS NULL)
                OR
                (followup2_due IS NOT NULL AND followup2_sent_at IS NULL)
            ORDER BY
                COALESCE(
                    CASE WHEN followup1_due IS NOT NULL AND followup1_sent_at IS NULL THEN followup1_due END,
                    CASE WHEN followup2_due IS NOT NULL AND followup2_sent_at IS NULL THEN followup2_due END
                ) ASC
            LIMIT 5
        """).fetchall()

        next_followups = []
        for row in next_fu_rows:
            data = json.loads(row["data"])
            # Determine which follow-up slot is next
            if row["followup1_sent_at"] is None and row["followup1_due"]:
                slot, due = "follow-up 1", row["followup1_due"]
            else:
                slot, due = "follow-up 2", row["followup2_due"]
            next_followups.append({
                "rowid": row["rowid"],
                "name": data.get("name", ""),
                "slot": slot,
                "due": due,
            })

        # All lead data for trade/area breakdown (done in Python)
        all_rows = conn.execute("SELECT data FROM leads").fetchall()

    trade_counts: dict[str, int] = {}
    area_counts: dict[str, int] = {}
    for row in all_rows:
        d = json.loads(row["data"])
        t = d.get("trade", "unknown") or "unknown"
        trade_counts[t] = trade_counts.get(t, 0) + 1

        address = d.get("address", "")
        area = _parse_area(address)
        if area:
            area_counts[area] = area_counts.get(area, 0) + 1

    return {
        "total": total,
        "status_counts": status_counts,
        "followup1_due_count": fu1_due,
        "followup2_due_count": fu2_due,
        "next_followups": next_followups,
        "trade_counts": trade_counts,
        "area_counts": area_counts,
    }


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

import re as _re
_POSTCODE_STANDALONE_RE = _re.compile(r"^[A-Z]{1,2}\d", _re.IGNORECASE)
_POSTCODE_TRAILING_RE = _re.compile(r"\s+[A-Z]{1,2}\d[A-Z\d]* ?\d[A-Z]{2}$", _re.IGNORECASE)
_STARTS_WITH_DIGIT_RE = _re.compile(r"^\d")
_SKIP_PARTS = {"united kingdom", "uk", "england", "wales", "scotland", "northern ireland"}


def _parse_area(address: str) -> str:
    """Extract a town/city name from a UK-style comma-separated address."""
    parts = [p.strip() for p in address.split(",")]
    for part in parts[1:]:  # skip the street (first segment)
        low = part.lower()
        if low in _SKIP_PARTS:
            continue
        if _POSTCODE_STANDALONE_RE.match(part):  # pure postcode segment
            continue
        if _STARTS_WITH_DIGIT_RE.match(part):  # looks like a street / house number
            continue
        # Strip a trailing embedded postcode e.g. "Basingstoke RG22 5LH" -> "Basingstoke"
        cleaned = _POSTCODE_TRAILING_RE.sub("", part).strip()
        if cleaned:
            return cleaned
    return ""


def get_scheduled_send_times(for_date: "date | None" = None) -> list[datetime]:
    """Return all future scheduled send times across initial emails and follow-ups."""
    now = datetime.now()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT scheduled_send_at, followup1_scheduled_at, followup2_scheduled_at FROM leads"
        ).fetchall()
    result = []
    for row in rows:
        for col in ("scheduled_send_at", "followup1_scheduled_at", "followup2_scheduled_at"):
            val = row[col]
            if not val:
                continue
            try:
                dt = datetime.fromisoformat(val)
                if dt > now and (for_date is None or dt.date() == for_date):
                    result.append(dt)
            except (ValueError, TypeError):
                continue
    return result


def get_initial_drafts_due() -> list[dict]:
    """Return drafted initial emails whose scheduled send time has arrived."""
    now = datetime.now().isoformat()
    cols = ", ".join(col for col, _ in _PIPELINE_COLUMNS)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT rowid, place_id, data, {cols} FROM leads
                WHERE gmail_draft_id IS NOT NULL
                  AND scheduled_send_at IS NOT NULL
                  AND scheduled_send_at <= ?
                  AND COALESCE(status, 'not_contacted') = 'drafted'""",
            (now,),
        ).fetchall()
    return [_merge_row(row) for row in rows]


def get_followup_drafts_due() -> list[dict]:
    """Return follow-up drafts whose scheduled send time has arrived but haven't been sent yet."""
    now = datetime.now().isoformat()
    cols = ", ".join(col for col, _ in _PIPELINE_COLUMNS)
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT rowid, place_id, data, {cols} FROM leads
                WHERE (
                    (followup1_draft_id IS NOT NULL AND followup1_scheduled_at IS NOT NULL
                     AND followup1_scheduled_at <= ? AND followup1_sent_at IS NULL)
                    OR
                    (followup2_draft_id IS NOT NULL AND followup2_scheduled_at IS NOT NULL
                     AND followup2_scheduled_at <= ? AND followup2_sent_at IS NULL)
                )""",
            (now, now),
        ).fetchall()
    return [_merge_row(row) for row in rows]


def followup_due_dates(sent_at: str) -> tuple[str, str]:
    """Return (followup1_due, followup2_due) as ISO date strings from a sent_at ISO datetime."""
    sent = datetime.fromisoformat(sent_at).date()
    return (
        (sent + timedelta(days=4)).isoformat(),
        (sent + timedelta(days=9)).isoformat(),
    )
