"""Push gmail_token.json into the Railway PostgreSQL database so the worker can read it.
Run this once (and again whenever you re-authenticate Gmail)."""
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import os, json, psycopg2

TOKEN_PATH   = Path("gmail_token.json")
DATABASE_URL = os.environ["RAILWAY_DATABASE_URL"]

token_json = TOKEN_PATH.read_text(encoding="utf-8")
# Validate it parses
json.loads(token_json)

conn = psycopg2.connect(DATABASE_URL)
with conn:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        cur.execute("""
            INSERT INTO settings (key, value) VALUES ('gmail_token_json', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (token_json,))
conn.close()
print("Token stored in Railway database.")
print(f"Account: {json.loads(token_json).get('account') or 'default'}")
