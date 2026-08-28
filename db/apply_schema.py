#!/usr/bin/env python3
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
import psycopg2

conn = psycopg2.connect(os.environ["DATABASE_URL"])
conn.autocommit = True
cur = conn.cursor()

with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as f:
    schema_sql = f.read()

cur.execute(schema_sql)
print("Schema applied successfully.")

cur.execute("""
    SELECT table_name FROM information_schema.tables
    WHERE table_schema='public' ORDER BY table_name;
""")
tables = [r[0] for r in cur.fetchall()]
print("Tables created:", tables)
conn.close()
