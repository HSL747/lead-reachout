"""Check the Railway send queue status."""
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import os, psycopg2, re

url = os.environ["RAILWAY_DATABASE_URL"]
conn = psycopg2.connect(url)
with conn.cursor() as cur:
    cur.execute("""
        SELECT id, to_email, subject, send_at, sent_at, lead_ref, created_at
        FROM send_queue
        ORDER BY created_at DESC
        LIMIT 10
    """)
    rows = cur.fetchall()

if not rows:
    print("send_queue is empty.")
else:
    print(f"{'ID':>4}  {'To':<30}  {'Send at':<20}  {'Sent at':<20}  Subject")
    print("-" * 110)
    for row in rows:
        id_, to, subj, send_at, sent_at, ref, created = row
        sent_str = str(sent_at)[:19] if sent_at else "NOT SENT"
        send_str = str(send_at)[:19] if send_at else "-"
        print(f"{id_:>4}  {to:<30}  {send_str:<20}  {sent_str:<20}  {subj}")

conn.close()
