#!/usr/bin/env python3
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
import psycopg2

conn = psycopg2.connect(os.environ["DATABASE_URL"])
conn.autocommit = True
cur = conn.cursor()

with open(os.path.join(os.path.dirname(__file__), "seed_data.sql")) as f:
    seed_sql = f.read()

cur.execute(seed_sql)
print("Seed data loaded.")

for table in ["office", "vendor_master", "vendor_onboarding", "category", "requisition",
              "purchase_order", "receipt", "invoice", "advance", "payment", "credit_note"]:
    cur.execute(f"SELECT COUNT(*) FROM {table};")
    print(f"  {table}: {cur.fetchone()[0]} rows")

conn.close()
