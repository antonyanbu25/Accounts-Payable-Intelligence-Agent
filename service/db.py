"""
Postgres access for the native orchestration layer (uc1_orchestration.py /
uc2_orchestration.py), replacing the Postgres nodes n8n's
build_uc1_workflow.py / build_uc2_workflow.py used to run. Every query here
is a parameterized, line-for-line port of the corresponding n8n SQL --
same columns, same joins, same WHERE/ORDER logic -- with one deliberate
improvement: real bind parameters (%(name)s) instead of n8n's raw string
interpolation with manually-doubled quotes, which was only ever a workaround
for n8n's own expression syntax, not a design choice worth preserving.

Connection pattern draws from a small pool (see _get_db_pool), not a fresh
psycopg2.connect() per call -- found live (recruiter-reported latency,
timed directly against this app's real DATABASE_URL) that opening a fresh
connection costs 0.9-2.0s here (a remote Postgres TCP+TLS handshake +
auth), while a query itself is ~90ms. A single UC1 request opens 1-2 of
these per answer (resolve_vendor + retrieve_invoice_facts_uc1, e.g.), so
connect-per-call was multiplying that handshake cost into every single
answer's latency. frontend/app.py's OWN separate get_db_connection() (its
Browse Mock Data / Manual Form invoice picker) had the identical pattern
and got the identical fix first; this is that same fix applied here.
_get_db_pool() is memoized the same way service/main.py already memoizes
get_corpus()/get_client() (@lru_cache, one instance for the process
lifetime) -- no Streamlit available on this side to use its
@st.cache_resource equivalent.

DATABASE_URL is read lazily, inside _get_db_pool(), NOT at module import
time -- this module is now imported transitively by main.py itself, so
reading the env var at module scope would break /health and every
existing test in any environment without Postgres configured.
"""
import os
from functools import lru_cache
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool


@lru_cache()
def _get_db_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """A real pool, not a single shared connection cached the same way --
    FastAPI can genuinely serve concurrent requests on multiple threads,
    and a bare psycopg2 connection isn't safe for simultaneous use across
    them. minconn=1 keeps at least one warm connection open at all times;
    maxconn=5 is comfortably above this service's actual concurrent need
    (each UC1/UC2 request opens at most 2 connections in sequence, never
    several at once)."""
    return psycopg2.pool.ThreadedConnectionPool(
        minconn=1, maxconn=5, dsn=os.environ["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor,
    )


def get_db_connection():
    """Borrows a connection from the shared pool (see _get_db_pool) instead
    of opening a fresh one. Callers MUST release it via
    release_db_connection(conn) when done -- never conn.close(), which
    would destroy the pooled connection outright instead of returning it
    for reuse, defeating the whole point of pooling.

    Pre-ping's before returning it -- found live: a pooled connection can go
    stale/closed server-side (e.g. Postgres's own idle-connection timeout,
    reached overnight between real requests) without psycopg2 knowing until
    something actually tries to use it. Without this check, the CALLER's
    real query is what discovers the connection is dead, surfacing as a
    real, user-visible failure on whatever request happens to draw the
    short straw. A cheap SELECT 1 here catches that first, on a connection
    nothing else depends on yet, and swaps in a fresh one before handing
    anything back -- at most one extra sub-millisecond round trip on an
    already-open connection, negligible next to the multi-second handshake
    cost pooling itself removed. Retries once (covers "the one connection we
    grabbed happened to be the dead one"); if a second, freshly-opened
    connection ALSO fails the ping, that's a real outage, not staleness --
    let it raise so the caller's own error handling sees it, same as
    before this fix existed. Catches psycopg2.Error broadly (not just
    OperationalError) -- a connection the SERVER silently dropped raises
    OperationalError on the next use, but one already marked .closed for
    some other reason raises InterfaceError instead; both are connection-
    health failures this ping needs to catch, and since the query here is
    the fixed, always-valid "SELECT 1", nothing else (a syntax/integrity
    error) can reach this except clause to be wrongly swallowed by it."""
    pool = _get_db_pool()
    last_err = None
    for _ in range(2):
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            return conn
        except psycopg2.Error as e:
            last_err = e
            pool.putconn(conn, close=True)  # discard, don't recycle a dead connection
    raise last_err


def release_db_connection(conn) -> None:
    _get_db_pool().putconn(conn)


def _floatify(row: Optional[dict], keys: list) -> Optional[dict]:
    """psycopg2 returns Postgres NUMERIC columns as decimal.Decimal, not
    float (n8n's JS did Number(...) on the equivalent fields everywhere it
    built a request body). Cast once, here, at the DB boundary -- single
    source of truth instead of scattered float(...) calls through
    orchestration code."""
    if row is None:
        return None
    for k in keys:
        if row.get(k) is not None:
            row[k] = float(row[k])
    return row


# Numeric columns shared by every invoice-facts query below.
_INVOICE_NUMERIC_KEYS = [
    "base_amount", "gst_rate_stated", "gst_amount_stated", "po_amount", "received_amount",
    "advances_applied", "unapplied_advances", "credits_applied", "payments_made",
]

# Shared SELECT/FROM/JOIN fragment for the two "full invoice facts" queries
# (single-invoice and comparison) -- identical column list and join graph
# in both n8n originals (RETRIEVE_FACTS_SQL_TEMPLATE /
# RETRIEVE_FACTS_SQL_TEMPLATE_COMPARISON), which duplicated this verbatim
# only because n8n needed two separate Postgres nodes (see the comment on
# the comparison branch in n8n/build_uc1_workflow.py for why). That reason
# doesn't apply to a plain function call, so it's factored into one place
# here -- same columns, same joins, not a behavior change.
_FACTS_SELECT_JOIN = """SELECT
  i.invoice_id, TO_CHAR(i.invoice_date, 'YYYY-MM-DD') AS invoice_date, i.base_amount, i.gst_rate_stated, i.gst_amount_stated, i.status AS invoice_status,
  i.po_id,
  v.vendor_id, v.legal_name, v.registered_state, v.status AS vendor_status, v.payment_terms, v.gstin,
  vo.submitted_state AS onboarding_state,
  ci.category_id AS invoice_category_id, ci.category_name AS invoice_category, ci.hsn_or_sac_code AS invoice_hsn_sac,
  po.category_id AS po_category_id, cp.category_name AS po_category,
  po.po_amount, po.status AS po_status,
  r.received_amount, r.status AS receipt_status,
  o.state AS office_state,
  COALESCE((SELECT SUM(amount) FROM advance WHERE applied_against_invoice_id = i.invoice_id), 0) AS advances_applied,
  COALESCE((SELECT SUM(amount) FROM advance WHERE vendor_id = v.vendor_id AND applied_against_invoice_id IS NULL), 0) AS unapplied_advances,
  COALESCE((SELECT SUM(amount) FROM credit_note WHERE invoice_id = i.invoice_id), 0) AS credits_applied,
  COALESCE((SELECT SUM(amount) FROM payment WHERE invoice_id = i.invoice_id), 0) AS payments_made,
  (SELECT COUNT(*) FROM invoice WHERE vendor_id = v.vendor_id AND status = 'open') AS vendor_open_invoice_count
FROM invoice i
JOIN vendor_master v ON i.vendor_id = v.vendor_id
JOIN vendor_onboarding vo ON v.vendor_id = vo.vendor_id
JOIN category ci ON i.category_id = ci.category_id
LEFT JOIN purchase_order po ON i.po_id = po.po_id
LEFT JOIN category cp ON po.category_id = cp.category_id
LEFT JOIN receipt r ON r.po_id = po.po_id
LEFT JOIN requisition req ON po.requisition_id = req.requisition_id
LEFT JOIN office o ON req.office_id = o.office_id"""


def resolve_vendor(name: str) -> list:
    """Port of RESOLVE_VENDOR_SQL. word_similarity (asymmetric trigram
    match, not symmetric similarity()) -- see the n8n file's own comment for
    why: a short genuine nickname ("TechNova") against a long legal name
    needs the asymmetric "does the query match well against SOME part of
    the name" question, not a length-penalized symmetric one. Threshold 0.4,
    top 5 by score desc, same as the original."""
    sql = """SELECT vendor_id, legal_name, word_similarity(%(name)s, legal_name) AS score
FROM vendor_master
WHERE word_similarity(%(name)s, legal_name) > 0.4
ORDER BY score DESC
LIMIT 5;"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, {"name": name})
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        release_db_connection(conn)
    for row in rows:
        _floatify(row, ["score"])
    return rows


def retrieve_invoice_facts_uc1(vendor_id: int, invoice_id: Optional[int] = None) -> Optional[dict]:
    """Port of RETRIEVE_FACTS_SQL_TEMPLATE/_EXPR (build_uc1_workflow.py).
    If invoice_id is given, filters to exactly that one (scoped to the
    resolved vendor, so a mismatched invoice number for a different vendor
    correctly returns no row rather than silently picking a wrong one);
    otherwise falls back to that vendor's most recent invoice. Returns None
    on zero rows -- callers must handle this explicitly (see the gap-fix in
    uc1_orchestration.py)."""
    where = "WHERE i.vendor_id = %(vendor_id)s"
    params = {"vendor_id": vendor_id}
    order = "ORDER BY i.invoice_date DESC"
    if invoice_id is not None:
        where += " AND i.invoice_id = %(invoice_id)s"
        params["invoice_id"] = invoice_id
        order = ""
    sql = f"{_FACTS_SELECT_JOIN}\n{where}\n{order}\nLIMIT 1;"
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            row = dict(row) if row else None
    finally:
        release_db_connection(conn)
    return _floatify(row, _INVOICE_NUMERIC_KEYS)


def list_open_invoices(vendor_id: int) -> list:
    """Found by an independent recruiter-style evaluation (round 3): a
    vendor question naming no specific invoice used to silently answer from
    only the most recent one instead of asking which -- see the
    ambiguous_invoice clarification in uc1_orchestration.py's
    _handle_single_invoice(). This lists the candidates to disambiguate
    among, same "ask, don't guess" discipline resolve_vendor() above already
    applies to an ambiguous vendor NAME."""
    sql = """SELECT invoice_id, TO_CHAR(invoice_date, 'YYYY-MM-DD') AS invoice_date, base_amount
FROM invoice
WHERE vendor_id = %(vendor_id)s AND status = 'open'
ORDER BY invoice_date DESC;"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, {"vendor_id": vendor_id})
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        release_db_connection(conn)
    for row in rows:
        _floatify(row, ["base_amount"])
    return rows


def retrieve_invoice_facts_comparison(vendor_id: int, invoice_ids: list) -> list:
    """Port of RETRIEVE_FACTS_SQL_TEMPLATE_COMPARISON/_EXPR_COMPARISON.
    i.invoice_id is a primary key, so = ANY(ids) returns one row per
    requested invoice that actually exists -- no LIMIT needed. Caller is
    responsible for the dedup/cap-at-2 logic (same as the n8n original,
    which did this in the JS body-builder, not the SQL)."""
    sql = f"{_FACTS_SELECT_JOIN}\nWHERE i.vendor_id = %(vendor_id)s AND i.invoice_id = ANY(%(invoice_ids)s)\nORDER BY i.invoice_date DESC;"
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, {"vendor_id": vendor_id, "invoice_ids": invoice_ids})
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        release_db_connection(conn)
    return [_floatify(r, _INVOICE_NUMERIC_KEYS) for r in rows]


def retrieve_vendor_details(vendor_id: int) -> Optional[dict]:
    """Port of RETRIEVE_VENDOR_DETAILS_SQL_TEMPLATE/_EXPR -- vendor profile
    + a json_agg'd list of every invoice for that vendor, in one query
    (matching this codebase's existing single-query-per-retrieval style)."""
    sql = """SELECT
  v.legal_name, v.gstin, v.registered_state, v.pan, v.payment_terms, v.status AS vendor_status,
  vo.submitted_state AS onboarding_state, vo.onboarding_status, TO_CHAR(vo.onboarding_date, 'YYYY-MM-DD') AS onboarding_date,
  (SELECT json_agg(json_build_object(
      'invoice_id', i.invoice_id, 'category', c.category_name, 'base_amount', i.base_amount,
      'invoice_date', TO_CHAR(i.invoice_date, 'YYYY-MM-DD'), 'status', i.status
    ) ORDER BY i.invoice_date DESC)
   FROM invoice i JOIN category c ON i.category_id = c.category_id
   WHERE i.vendor_id = v.vendor_id) AS invoices
FROM vendor_master v
JOIN vendor_onboarding vo ON v.vendor_id = vo.vendor_id
WHERE v.vendor_id = %(vendor_id)s;"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, {"vendor_id": vendor_id})
            row = cur.fetchone()
            row = dict(row) if row else None
    finally:
        release_db_connection(conn)
    if row and row.get("invoices"):
        for inv in row["invoices"]:
            _floatify(inv, ["base_amount"])
    return row


def retrieve_invoice_facts_uc2(invoice_id: int) -> Optional[dict]:
    """Port of UC2's own RETRIEVE_FACTS_SQL (build_uc2_workflow.py). Kept
    deliberately separate from retrieve_invoice_facts_uc1: no
    vendor_open_invoice_count (UC2 already knows the specific invoice, no
    "which one of several" ambiguity to disclose) and filters directly on
    invoice_id, no vendor pre-resolution step."""
    sql = """SELECT
  i.invoice_id, TO_CHAR(i.invoice_date, 'YYYY-MM-DD') AS invoice_date, i.base_amount, i.gst_rate_stated, i.gst_amount_stated, i.status AS invoice_status,
  i.po_id,
  v.vendor_id, v.legal_name, v.registered_state, v.status AS vendor_status, v.payment_terms, v.gstin,
  vo.submitted_state AS onboarding_state,
  ci.category_id AS invoice_category_id, ci.category_name AS invoice_category, ci.hsn_or_sac_code AS invoice_hsn_sac,
  po.category_id AS po_category_id, cp.category_name AS po_category,
  po.po_amount, po.status AS po_status,
  r.received_amount, r.status AS receipt_status,
  o.state AS office_state,
  COALESCE((SELECT SUM(amount) FROM advance WHERE applied_against_invoice_id = i.invoice_id), 0) AS advances_applied,
  COALESCE((SELECT SUM(amount) FROM advance WHERE vendor_id = v.vendor_id AND applied_against_invoice_id IS NULL), 0) AS unapplied_advances,
  COALESCE((SELECT SUM(amount) FROM credit_note WHERE invoice_id = i.invoice_id), 0) AS credits_applied,
  COALESCE((SELECT SUM(amount) FROM payment WHERE invoice_id = i.invoice_id), 0) AS payments_made
FROM invoice i
JOIN vendor_master v ON i.vendor_id = v.vendor_id
JOIN vendor_onboarding vo ON v.vendor_id = vo.vendor_id
JOIN category ci ON i.category_id = ci.category_id
LEFT JOIN purchase_order po ON i.po_id = po.po_id
LEFT JOIN category cp ON po.category_id = cp.category_id
LEFT JOIN receipt r ON r.po_id = po.po_id
LEFT JOIN requisition req ON po.requisition_id = req.requisition_id
LEFT JOIN office o ON req.office_id = o.office_id
WHERE i.invoice_id = %(invoice_id)s
LIMIT 1;"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, {"invoice_id": invoice_id})
            row = cur.fetchone()
            row = dict(row) if row else None
    finally:
        release_db_connection(conn)
    return _floatify(row, _INVOICE_NUMERIC_KEYS)
