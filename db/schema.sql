-- Accounts Payable Intelligence Agent — Database Schema
-- Target: PostgreSQL (uses the pg_trgm extension for fuzzy vendor-name matching)
-- Two logical "systems" (Source A: Procurement Portal, Source B: SAP-style
-- Vendor & Payments DB) live in one physical database for this mock, but are
-- kept as clearly separate table groups to reflect that they are genuinely
-- siloed systems in the real scenario (see plan v4 for the full rationale).

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- =========================================================================
-- SHARED REFERENCE DATA
-- Real master/reference data (tax codes) legitimately lives outside either
-- silo, same as it would in a real organization.
-- =========================================================================

CREATE TABLE category (
    category_id     SERIAL PRIMARY KEY,
    category_name   TEXT NOT NULL,              -- furniture, software, services, food, appliances
    hsn_or_sac_code TEXT NOT NULL,               -- the real lookup key into Source C tax docs
    code_type       TEXT NOT NULL CHECK (code_type IN ('HSN', 'SAC'))  -- HSN = goods, SAC = services
);

-- =========================================================================
-- SOURCE A — PROCUREMENT PORTAL (workflow: request -> approval -> fulfillment)
-- =========================================================================

CREATE TABLE office (
    office_id   SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    city        TEXT NOT NULL,
    state       TEXT NOT NULL          -- drives place-of-supply comparison against vendor GSTIN state
);

-- Deliberately holds its OWN copy of vendor state, captured at onboarding time.
-- This is allowed to drift from vendor_master.registered_state (Source B) —
-- that drift is the seeded, authority-rule-resolved conflict (see plan v4).
CREATE TABLE vendor_onboarding (
    vendor_id           INTEGER PRIMARY KEY,   -- shared vendor code across A and B (see note below)
    onboarding_status   TEXT NOT NULL CHECK (onboarding_status IN ('pending', 'approved', 'rejected')),
    submitted_state      TEXT NOT NULL,        -- what was typed in at onboarding time — may be stale/wrong
    onboarding_date      DATE NOT NULL
);

CREATE TABLE requisition (
    requisition_id  SERIAL PRIMARY KEY,
    office_id       INTEGER NOT NULL REFERENCES office(office_id),
    requester       TEXT NOT NULL,
    department      TEXT NOT NULL,
    category_id     INTEGER NOT NULL REFERENCES category(category_id),
    estimated_amount NUMERIC(12,2) NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('draft', 'submitted', 'approved', 'rejected')),
    created_date    DATE NOT NULL
);

CREATE TABLE purchase_order (
    po_id           SERIAL PRIMARY KEY,
    requisition_id  INTEGER NOT NULL REFERENCES requisition(requisition_id),
    vendor_id       INTEGER NOT NULL,          -- FK to vendor_master added below, after that table exists
    category_id     INTEGER NOT NULL REFERENCES category(category_id),
    po_amount       NUMERIC(12,2) NOT NULL,    -- tax-exclusive, base value only
    issued_date     DATE NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('issued', 'amended', 'closed', 'cancelled'))
);

CREATE TABLE receipt (
    receipt_id      SERIAL PRIMARY KEY,
    po_id           INTEGER NOT NULL REFERENCES purchase_order(po_id),
    received_date   DATE NOT NULL,
    received_amount NUMERIC(12,2) NOT NULL,    -- tax-exclusive, base value only
    status          TEXT NOT NULL CHECK (status IN ('full', 'partial'))
);

-- =========================================================================
-- SOURCE B — SAP-STYLE VENDOR & PAYMENTS DB (the money)
-- =========================================================================

CREATE TABLE vendor_master (
    vendor_id        INTEGER PRIMARY KEY,      -- same vendor code as vendor_onboarding.vendor_id
    legal_name       TEXT NOT NULL,
    gstin            TEXT NOT NULL,            -- first 2 digits encode the registered state
    registered_state TEXT NOT NULL,            -- derived from GSTIN; the LEGAL authority for tax jurisdiction
    pan              TEXT NOT NULL,
    payment_terms    TEXT NOT NULL,            -- e.g. 'Net 30'
    status           TEXT NOT NULL CHECK (status IN ('active', 'blocked'))
);

-- Now that vendor_master exists, complete the FKs that reference it
ALTER TABLE vendor_onboarding
    ADD CONSTRAINT fk_onboarding_vendor FOREIGN KEY (vendor_id) REFERENCES vendor_master(vendor_id);
ALTER TABLE purchase_order
    ADD CONSTRAINT fk_po_vendor FOREIGN KEY (vendor_id) REFERENCES vendor_master(vendor_id);

-- Trigram index powering fuzzy vendor-name resolution ("Vendor X" -> vendor_id)
CREATE INDEX idx_vendor_name_trgm ON vendor_master USING gin (legal_name gin_trgm_ops);

CREATE TABLE invoice (
    invoice_id        SERIAL PRIMARY KEY,
    po_id              INTEGER REFERENCES purchase_order(po_id),  -- NULLABLE: null = non-PO / maverick spend
    vendor_id          INTEGER NOT NULL REFERENCES vendor_master(vendor_id),
    invoice_date       DATE NOT NULL,          -- the sole date key for effective-dated tax lookups
    category_id        INTEGER NOT NULL REFERENCES category(category_id),  -- vs. PO's category_id = seeded conflict
    base_amount        NUMERIC(12,2) NOT NULL, -- tax-exclusive
    gst_rate_stated    NUMERIC(5,2),           -- what the VENDOR's own invoice claims — evidence only, never
    gst_amount_stated  NUMERIC(12,2),          -- a computation input. May be wrong (see the seeded case).
    status             TEXT NOT NULL CHECK (status IN ('open', 'cancelled', 'disputed'))
);

CREATE TABLE advance (
    advance_id               SERIAL PRIMARY KEY,
    vendor_id                INTEGER NOT NULL REFERENCES vendor_master(vendor_id),
    po_id                     INTEGER REFERENCES purchase_order(po_id),   -- nullable
    amount                    NUMERIC(12,2) NOT NULL,
    advance_date              DATE NOT NULL,
    applied_against_invoice_id INTEGER REFERENCES invoice(invoice_id)      -- NULL = unapplied (advisory-only)
);

CREATE TABLE payment (
    payment_id   SERIAL PRIMARY KEY,
    invoice_id   INTEGER NOT NULL REFERENCES invoice(invoice_id),
    amount       NUMERIC(12,2) NOT NULL,
    payment_date DATE NOT NULL,
    type         TEXT NOT NULL CHECK (type IN ('full', 'partial'))
);

CREATE TABLE credit_note (
    credit_id  SERIAL PRIMARY KEY,
    vendor_id  INTEGER NOT NULL REFERENCES vendor_master(vendor_id),
    invoice_id INTEGER REFERENCES invoice(invoice_id),   -- nullable
    amount     NUMERIC(12,2) NOT NULL,
    reason     TEXT NOT NULL,
    credit_date DATE NOT NULL
);
