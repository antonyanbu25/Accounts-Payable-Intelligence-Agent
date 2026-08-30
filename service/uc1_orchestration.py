"""
Native Python replacement for n8n's UC1 workflow (n8n/build_uc1_workflow.py)
-- conversational vendor/invoice/tax lookup. Every branch, SQL query,
prompt, and response shape here is a deliberate, verbatim port of that file;
re-read it directly if anything here looks like it needs adjusting, rather
than trusting memory.

IMPORT-ORDER NOTE: see uc2_orchestration.py's module docstring -- the same
`from main import ...` / "import main first" rule applies here.

GAP FIX (new behavior, not in the n8n original): n8n's "Retrieve Facts" had
no not-found handling at all -- a named invoice not belonging to the
resolved vendor, or a vendor with zero invoices, silently produced no
downstream execution and no response. Python has no equivalent of n8n's
"0 items => silently stop", so *some* explicit handling is unavoidable;
_handle_single_invoice() below returns a proper {"error": "not_found", ...}
for both cases instead.

TWO GENUINE SIMPLIFICATIONS vs. the n8n original (not just parity): the
comparison branch's n8n-specific .item/pairedItem/executionOrder/"First
Comparison Item?" collapse-node machinery has no Python equivalent need --
a plain ordered loop over 2 rows is used instead. The "Vendor Resolved
(First Item)?" collapse node likewise only existed to handle n8n's
multi-item execution model; taking candidates[0] directly is already
correct here.
"""
import json
import re
from datetime import date

import db
from anthropic_client import call_text, parse_intent
from main import check_narration_endpoint, do_compute, tax_lookup
from models import ComputeRequest, NarrationCheckRequest, TaxLookupRequest
from shared_constants import HSN_SAC_BY_CATEGORY

GUARD_FALLBACK_SUFFIX = " [Showing the verified figures directly -- the written summary did not pass our accuracy check.]"

UNSUPPORTED_RESPONSE = {
    "error": "unsupported",
    "message": ("This assistant only answers questions about vendor balances, tax treatment, and general "
                "vendor information in the accounts payable system -- it can't help with that."),
}

# Found by an independent recruiter-style evaluation (round 3): a genuine
# AP-domain question naming no vendor at all ("what do we owe right now")
# used to fall through to intent="unsupported" (no aggregate_lookup value
# existed) and get this exact same generic UNSUPPORTED_RESPONSE text as a
# truly off-topic question ("what's the weather"). Both are refused either
# way, but a scope limit ("I only look up one vendor at a time") reads very
# differently from an out-of-domain refusal, and conflating the two made the
# system look less capable than it is.
AGGREGATE_UNSUPPORTED_RESPONSE = {
    "error": "aggregate_unsupported",
    "message": ("I can look up one vendor at a time, not a running total across every vendor or invoice -- "
                "which vendor did you mean? (Aggregating balances across vendors, or ranking them against "
                "each other, isn't supported yet.)"),
}


def _num(v) -> str:
    """Match JS's implicit number-to-string coercion (e.g. 'claimed ' + f.claimed)."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def handle_uc1_ask(question: str, history: list = None) -> dict:
    parsed = parse_intent(question, history)
    # Defense-in-depth, not just a prompt fix: a correctly-populated
    # category_mentioned is a strong, narrowly-scoped signal (a fixed enum,
    # only ever set when the model recognized a real category-level tax
    # question) that survives even when intent classification itself
    # wobbles under conversational pressure from unrelated prior history --
    # observed live: identical, CORRECT vendor_name_mentioned=""/
    # category_mentioned="Furniture" extraction, but intent flipped from
    # "tax_lookup" (no history) to "unsupported" (with a prior
    # specific-invoice turn in history) for the exact same question. Rather
    # than trust intent alone here, treat a populated category_mentioned as
    # proof the question was answerable and let it fall through to the
    # category-only branch below regardless of what intent says.
    if parsed.get("intent") == "unsupported" and not parsed.get("category_mentioned"):
        return UNSUPPORTED_RESPONSE
    if parsed.get("intent") == "aggregate_lookup":
        return AGGREGATE_UNSUPPORTED_RESPONSE

    vendor_name = parsed.get("vendor_name_mentioned") or ""
    candidates = db.resolve_vendor(vendor_name) if vendor_name else []
    # Port of "Vendor Ambiguous?": gap forced to 1 (never ambiguous) when
    # fewer than 2 candidates exist.
    gap = (candidates[0]["score"] - candidates[1]["score"]) if len(candidates) >= 2 else 1
    if gap < 0.2:
        names = [c["legal_name"] for c in candidates]
        return {"error": "ambiguous_vendor",
                "message": f"\"{vendor_name}\" matches multiple vendors on file: {', '.join(names)}. "
                           "Please specify which one you mean.",
                "candidates": names}
    vendor = candidates[0] if candidates else None

    category_mentioned = parsed.get("category_mentioned")
    if vendor is None:
        if category_mentioned:
            return _handle_category_only(parsed)
        return {"error": "not_found",
                "message": f"No vendor matching '{vendor_name}' was found, and no purchase category was "
                           "identifiable either -- cannot answer without guessing."}

    invoice_id_mentioned = parsed.get("invoice_id_mentioned")
    is_vendor_lookup = parsed.get("intent") == "vendor_lookup" and invoice_id_mentioned is None
    if is_vendor_lookup:
        return _handle_vendor_details(vendor["vendor_id"])

    additional_ids = parsed.get("additional_invoice_ids_mentioned") or []
    extra = [int(x) for x in additional_ids if int(x) != invoice_id_mentioned]
    if len(extra) > 0:
        return _handle_comparison(vendor, invoice_id_mentioned, additional_ids)

    return _handle_single_invoice(parsed, vendor)


def _handle_category_only(parsed: dict) -> dict:
    category = parsed.get("category_mentioned")
    explicit_date = parsed.get("explicit_date_mentioned")
    used_today = not explicit_date
    date_to_use = explicit_date or date.today().isoformat()

    tax = tax_lookup(TaxLookupRequest(
        category=category, hsn_or_sac=HSN_SAC_BY_CATEGORY.get(category, ""), invoice_date=date_to_use,
        vendor_state="Karnataka", office_state="Karnataka", needs_tds=True,
    ))

    date_note = (f"No date was stated in the question, so today's date ({date.today().isoformat()}) was "
                 "assumed -- say this explicitly.") if used_today else (
        f"The question specified the date {explicit_date} -- use that date, do not mention an assumption.")
    prompt = ("Write a short (2-3 sentence) plain-language answer to a general (non-vendor-specific) tax-rate "
              "question, using ONLY the rate and figures in this JSON -- never invent one. This is a "
              "category-level rate, not tied to any specific transaction, so do NOT state a CGST/SGST/IGST "
              "split (that depends on a specific vendor and office, which aren't known here). " + date_note
              + f" Category: {category}. Tax lookup result: " + json.dumps(tax.model_dump()))
    narrative = call_text(prompt, max_tokens=300)

    nums = []
    if tax.gst and tax.gst.rate_pct is not None:
        nums.append(tax.gst.rate_pct)
    if tax.tds and tax.tds.rate_pct is not None:
        nums.append(tax.tds.rate_pct)
    guard = check_narration_endpoint(NarrationCheckRequest(narrative_text=narrative, structured_values=nums))

    if guard.passed:
        return {"narrative": narrative, "evidence": tax.model_dump(), "guard": "passed",
                "note": "category-level answer, not tied to a specific vendor or transaction"}
    rate = tax.gst.rate_pct if tax.gst and tax.gst.rate_pct is not None else "unknown"
    return {"narrative": f"Applicable GST rate: {rate}%.{GUARD_FALLBACK_SUFFIX}",
            "evidence": tax.model_dump(), "guard": "failed_fallback_used"}


def _handle_vendor_details(vendor_id: int) -> dict:
    details = db.retrieve_vendor_details(vendor_id)

    # Found by an independent recruiter-style regression eval: asking about
    # a blocked vendor's "payment eligibility" with no invoice named (the
    # only case this branch handles -- see handle_uc1_ask's dispatch,
    # naming an invoice routes to _handle_single_invoice instead, which
    # already computes real eligibility) got a correct but implicit answer
    # -- it reported vendor_status: blocked without stating what that means
    # for payment. This branch has no PO/receipt/3-way-match join and
    # structurally can't compute invoice-level eligibility (a deliberate,
    # already-established scope boundary), but "vendor blocked" alone is
    # already sufficient to say plainly that nothing can be paid to them --
    # no computation needed for that specific case.
    blocked_note = ""
    if details.get("vendor_status") == "blocked":
        blocked_note = (" IMPORTANT: this vendor's status is BLOCKED -- you MUST state plainly that any "
                         "pending or future payment to this vendor cannot proceed until this is resolved, "
                         "regardless of how correct any individual invoice's numbers look.")

    prompt = ("Write a short (2-4 sentence), plain-language answer to a general question about a vendor (not "
              "a balance/tax calculation), using ONLY the facts in this JSON -- never invent or alter a "
              "figure, and never compute a total. If the vendor's onboarding state differs from its "
              "GSTIN-registered state, mention this briefly as a data-quality note (GSTIN is the authoritative "
              "one for tax purposes, but both are worth surfacing here). If the vendor has multiple invoices, "
              "summarize them briefly (count, and category/status pattern) rather than listing every field of "
              "every one -- the evidence panel already shows the full list." + blocked_note + " Vendor details: "
              + json.dumps(details, default=str))
    narrative = call_text(prompt, max_tokens=400)

    invoices = details.get("invoices") or []
    nums = []
    for inv in invoices:
        nums.append(inv["base_amount"])
        nums.append(inv["invoice_id"])
    nums.append(len(invoices))
    # payment_terms (e.g. "Net 30") can embed a legitimate number the
    # narration is likely to repeat -- real bug fix, see n8n's own comment.
    for d in re.findall(r"\d+", details.get("payment_terms") or ""):
        nums.append(int(d))
    guard = check_narration_endpoint(NarrationCheckRequest(narrative_text=narrative, structured_values=nums))

    if guard.passed:
        return {"narrative": narrative, "evidence": details, "guard": "passed",
                "note": "general vendor information, not a balance/tax calculation"}
    blocked_suffix = (" ⚠ Status is BLOCKED -- no payment to this vendor can proceed until this is resolved."
                       if details.get("vendor_status") == "blocked" else "")
    return {"narrative": (f"{details['legal_name']} -- status: {details['vendor_status']}, {len(invoices)} "
                           f"invoice(s) on file.{blocked_suffix}{GUARD_FALLBACK_SUFFIX}"),
            "evidence": details, "guard": "failed_fallback_used"}


def _build_compute_kwargs(facts: dict) -> dict:
    """Shared between _handle_single_invoice and _handle_comparison -- port
    of COMPUTE_BODY_EXPR, identical in both n8n originals."""
    return dict(
        base_amount=facts["base_amount"], advances_applied=facts["advances_applied"],
        credits_applied=facts["credits_applied"], payments_made=facts["payments_made"],
        unapplied_advances=facts["unapplied_advances"], po_amount=facts["po_amount"],
        receipt_amount=facts["received_amount"], vendor_status=facts["vendor_status"],
        po_status=facts["po_status"], invoice_status=facts["invoice_status"],
    )


def _tax_lookup_and_compute(facts: dict):
    """Shared single-invoice tax-lookup + compute step, used by both
    _handle_single_invoice and each pair in _handle_comparison. tax_lookup()
    is called unconditionally even when its result will be discarded by a
    category conflict -- preserved exactly as the n8n original does it, not
    "optimized" away, to avoid any risk of subtle divergence."""
    tax = tax_lookup(TaxLookupRequest(
        category=facts["invoice_category"], hsn_or_sac=facts["invoice_hsn_sac"],
        invoice_date=facts["invoice_date"], vendor_state=facts["registered_state"],
        office_state=facts["office_state"], needs_tds=True,
    ))
    conflict = bool(facts["po_category"]) and bool(facts["invoice_category"]) and facts["po_category"] != facts["invoice_category"]
    kwargs = _build_compute_kwargs(facts)
    if conflict:
        kwargs["category_conflict_po_category"] = facts["po_category"]
        kwargs["category_conflict_invoice_category"] = facts["invoice_category"]
    else:
        kwargs["gst_rate_pct"] = tax.gst.rate_pct
        kwargs["tds_rate_pct"] = tax.tds.rate_pct if tax.tds else 0.0
        kwargs["tds_section"] = tax.tds.tds_section if tax.tds else None
        kwargs["split_type"] = tax.split_type
    computed = do_compute(ComputeRequest(**kwargs))
    return tax, computed


def _handle_single_invoice(parsed: dict, vendor: dict) -> dict:
    vendor_id = vendor["vendor_id"]
    invoice_id_mentioned = parsed.get("invoice_id_mentioned")
    facts = db.retrieve_invoice_facts_uc1(vendor_id, invoice_id_mentioned)

    # Gap fix -- see module docstring.
    if facts is None:
        if invoice_id_mentioned is not None:
            return {"error": "not_found",
                    "message": f"No invoice matching id {invoice_id_mentioned} was found for vendor "
                               f"'{vendor['legal_name']}' -- cannot answer a question about a record that "
                               "does not exist."}
        return {"error": "not_found",
                "message": f"Vendor '{vendor['legal_name']}' has no invoices on file to answer this question about."}

    # Found by an independent recruiter-style evaluation (round 3): a vendor
    # with several open invoices and no invoice named in the question used
    # to silently answer from only the most recent one -- a disclosure
    # buried in the narration, not a decision surfaced to the user -- instead
    # of asking which one. resolve_vendor() above already gives an ambiguous
    # vendor NAME this exact "ask, don't guess" treatment (the ambiguous_
    # vendor error a few lines up); this mirrors that shape for an ambiguous
    # invoice, returning before any tax lookup/compute runs so a genuinely
    # ambiguous question never produces a confident-looking answer for the
    # wrong invoice.
    if invoice_id_mentioned is None and (facts["vendor_open_invoice_count"] or 0) > 1:
        open_invoices = db.list_open_invoices(vendor_id)
        candidates = [f"INV-{inv['invoice_id']} ({inv['invoice_date']}, ₹{_num(inv['base_amount'])})"
                      for inv in open_invoices]
        return {"error": "ambiguous_invoice",
                "message": f"{vendor['legal_name']} has {len(open_invoices)} open invoices, and no specific "
                           f"one was named in the question: {', '.join(candidates)}. Please specify which "
                           "invoice you mean.",
                "candidates": candidates}

    tax, computed = _tax_lookup_and_compute(facts)

    settled = (computed.advances_applied or 0) + (computed.credits_applied or 0) + (computed.payments_made or 0)
    settled_note = ""
    if settled > 0:
        settled_note = (f" IMPORTANT: net_disbursement_due is LOWER than gross_liability minus TDS alone "
                         f"because ₹{_num(settled)} has already been settled against this invoice -- advances "
                         f"applied: ₹{_num(computed.advances_applied)}, credits applied: "
                         f"₹{_num(computed.credits_applied)}, payments already made: "
                         f"₹{_num(computed.payments_made)}. You MUST state this breakdown plainly (which of "
                         "these it is and how much) so the net figure is never left looking unexplained.")

    # Found by an independent recruiter-style regression eval: UC2's
    # narration has always had a MUST-state instruction for a real,
    # non-zero unapplied advance (see uc2_orchestration.py's
    # NARRATE_DIFF_PROMPT_PREFIX) -- UC1's never did, only the settled-
    # amount note above. Without it, the LLM was handed
    # unapplied_advance_advisory in the JSON but never told it must mention
    # it, so it sometimes did and sometimes didn't -- and even when it
    # tried, the guard (see nums below, before this fix) had no entry for
    # this number and would reject the narration as an "unverified" figure,
    # forcing the templated fallback on a case that should have passed
    # cleanly. This predates the n8n-removal migration -- the original
    # n8n/build_uc1_workflow.py NARRATE_BODY_EXPR had the identical gap.
    unapplied_note = ""
    if computed.unapplied_advance_advisory:
        unapplied_note = (f" ALSO SEPARATELY: a separate unapplied advance of "
                           f"₹{_num(computed.unapplied_advance_advisory)} exists against this vendor/PO and has "
                           "NOT been netted into net_disbursement_due above -- you MUST state this plainly as "
                           "its own note, since releasing this payment without accounting for it carries a "
                           "real overpayment risk. Never omit or soften this note.")

    # GAP FIX (found live, not in the n8n original's port -- gst_rate_stated
    # was fetched by retrieve_invoice_facts_uc1() all along but never
    # compared against the true rate anywhere in this branch, so the
    # documented "vendor's own invoice states a superseded rate" scenario
    # -- README's Assumption & Decision Log, and eval case
    # T1-vendor-stated-superseded-rate -- was silently never actually
    # surfaced in the live narration; the eval only checks the computed
    # figures, never the narrative text, so nothing caught this. tax.gst
    # (from _tax_lookup_and_compute above) is never None on this path --
    # a category conflict short-circuits before reaching here (see
    # _tax_lookup_and_compute), and a real "not found" tax status still
    # returns a TaxLookupSubResult with a real (if None) rate_pct field.
    stale_rate_note = ""
    if facts.get("gst_rate_stated") is not None and tax.gst.rate_pct is not None \
            and facts["gst_rate_stated"] != tax.gst.rate_pct:
        stale_rate_note = (f" ALSO SEPARATELY: the vendor's own invoice states a GST rate of "
                            f"{_num(facts['gst_rate_stated'])}%, but the actual current rate for this category "
                            f"and date is {_num(tax.gst.rate_pct)}% -- you MUST state this discrepancy plainly. "
                            "The vendor's invoice is internally consistent with itself but still wrong; never "
                            "let the vendor-stated figure be mistaken for the correct one, and never use it in "
                            "place of the true rate above.")

    prompt = ("Write a short (2-4 sentence), plain-language answer to an accounts-payable question, using "
              "ONLY the numbers in this JSON -- never invent or alter a figure. Vendor: "
              f"{facts['legal_name']}. Onboarding state on file: {facts['onboarding_state']} (vs. "
              f"GSTIN-registered state used for tax: {facts['registered_state']} -- mention this discrepancy "
              "briefly if they differ)." + settled_note + unapplied_note + stale_rate_note
              + " Structured result: " + json.dumps(computed.model_dump()))
    narrative = call_text(prompt, max_tokens=400)

    nums = [computed.base_amount, computed.gross_liability, computed.net_disbursement_due, computed.tds_amount,
            computed.pre_tax_ledger_position, facts["invoice_id"], facts["vendor_open_invoice_count"],
            computed.advances_applied, computed.credits_applied, computed.payments_made,
            computed.unapplied_advance_advisory, facts.get("gst_rate_stated"), tax.gst.rate_pct]
    if computed.gst:
        nums.extend([computed.gst.get("gst_amount"), computed.gst.get("cgst"), computed.gst.get("sgst"), computed.gst.get("igst")])
    nums = [n for n in nums if n is not None]
    guard = check_narration_endpoint(NarrationCheckRequest(narrative_text=narrative, structured_values=nums))

    if guard.passed:
        return {"narrative": narrative, "evidence": computed.model_dump(), "tax_evidence": tax.model_dump(),
                "guard": "passed"}
    advisory_suffix = ""
    if computed.unapplied_advance_advisory:
        advisory_suffix = (f" ⚠ Advisory: an unapplied advance of ₹{_num(computed.unapplied_advance_advisory)} "
                            "exists against this vendor/PO and has NOT been netted here -- a possible "
                            "overpayment risk if missed.")
    stale_rate_suffix = ""
    if facts.get("gst_rate_stated") is not None and tax.gst.rate_pct is not None \
            and facts["gst_rate_stated"] != tax.gst.rate_pct:
        stale_rate_suffix = (f" ℹ The vendor's own invoice states a GST rate of {_num(facts['gst_rate_stated'])}%, "
                              f"but the correct current rate is {_num(tax.gst.rate_pct)}% -- the figures above "
                              "use the correct rate, not the vendor's stated one.")
    fallback = (f"Net disbursement due: {_num(computed.net_disbursement_due)} "
                f"(eligibility: {computed.eligibility}).{advisory_suffix}{stale_rate_suffix}{GUARD_FALLBACK_SUFFIX}")
    return {"narrative": fallback, "evidence": computed.model_dump(), "tax_evidence": tax.model_dump(),
            "guard": "failed_fallback_used"}


def _handle_comparison(vendor: dict, invoice_id_mentioned, additional_ids: list) -> dict:
    vendor_id = vendor["vendor_id"]
    ids = ([invoice_id_mentioned] if invoice_id_mentioned is not None else []) + [int(x) for x in additional_ids]
    seen = []
    for i in ids:
        if i not in seen:
            seen.append(i)
    capped = seen[:2]

    rows = db.retrieve_invoice_facts_comparison(vendor_id, capped)
    if len(rows) != 2:
        found_ids = [r["invoice_id"] for r in rows]
        missing = [i for i in capped if i not in found_ids]
        found_str = (f"Found: invoice {', '.join(str(i) for i in found_ids)}."
                     if found_ids else "None of the named invoices were found.")
        message = (f"Could not compare invoices {' and '.join(str(i) for i in capped)} for this vendor -- "
                   f"invoice(s) {', '.join(str(i) for i in missing)} could not be found. " + found_str)
        return {"error": "comparison_incomplete", "message": message}

    pairs = []
    for f in rows:
        tax, computed = _tax_lookup_and_compute(f)
        pairs.append({"facts": f, "tax": tax, "compute": computed})

    vendor_name = pairs[0]["facts"]["legal_name"]
    onboarding_note = ""
    if pairs[0]["facts"]["onboarding_state"] != pairs[0]["facts"]["registered_state"]:
        onboarding_note = (f" Onboarding state on file: {pairs[0]['facts']['onboarding_state']} (vs. "
                            f"GSTIN-registered state used for tax: {pairs[0]['facts']['registered_state']} -- "
                            "mention this discrepancy briefly.)")
    extra_ignored = ""
    if len(additional_ids) > 1:
        ids_str = " and ".join(str(p["facts"]["invoice_id"]) for p in pairs)
        extra_ignored = (f" NOTE: more than two invoices were named; only the first two ({ids_str}) were "
                          "compared -- say so briefly and suggest asking about the rest separately.")

    per_invoice_data = [{"invoice_id": p["facts"]["invoice_id"], "invoice_date": p["facts"]["invoice_date"],
                          "tax_lookup": p["tax"].model_dump(), "compute_result": p["compute"].model_dump()}
                         for p in pairs]
    prompt = (f"Write a short (4-7 sentence), plain-language answer comparing {len(pairs)} invoices for the "
              f"same vendor, {vendor_name}, using ONLY the numbers in this JSON -- never invent or alter a "
              "figure. Explicitly state whether the tax treatment (GST rate, TDS rate/section, split type) "
              "differs between the invoices and, if so, name the actual reason evidenced in the data "
              "(different category, different HSN/SAC code, a rate that changed between the two invoice "
              "dates, or a PO/invoice category conflict on one of them). If either invoice has a "
              "category_conflict or tax_treatment_refused, say so explicitly for that invoice rather than "
              "computing a false comparison for it." + onboarding_note + extra_ignored
              + " Per-invoice data: " + json.dumps(per_invoice_data))
    narrative = call_text(prompt, max_tokens=500)

    nums = []
    for p in pairs:
        nums.append(p["facts"]["invoice_id"])
        c = p["compute"]
        nums.extend(x for x in [c.base_amount, c.gross_liability, c.net_disbursement_due, c.tds_amount,
                                 c.pre_tax_ledger_position, c.advances_applied, c.credits_applied,
                                 c.payments_made] if x is not None)
        if c.gst:
            nums.extend(x for x in [c.gst.get("gst_amount"), c.gst.get("cgst"), c.gst.get("sgst"), c.gst.get("igst")]
                        if x is not None)
        t = p["tax"]
        if t.gst and t.gst.rate_pct is not None:
            nums.append(t.gst.rate_pct)
        if t.tds and t.tds.rate_pct is not None:
            nums.append(t.tds.rate_pct)
    guard = check_narration_endpoint(NarrationCheckRequest(narrative_text=narrative, structured_values=nums))

    invoices_out = [{"invoice_id": p["facts"]["invoice_id"], "invoice_date": p["facts"]["invoice_date"],
                      "evidence": p["compute"].model_dump(), "tax_evidence": p["tax"].model_dump()}
                     for p in pairs]

    if guard.passed:
        resp = {"comparison": True, "narrative": narrative, "invoices": invoices_out, "guard": "passed"}
        if len(additional_ids) > 1:
            resp["note"] = "Only the first two invoices named were compared."
        return resp

    summary = "; ".join(f"invoice {inv['invoice_id']}: net disbursement due "
                         f"{inv['evidence']['net_disbursement_due']} ({inv['evidence']['eligibility']})"
                         for inv in invoices_out)
    return {"comparison": True, "narrative": summary + GUARD_FALLBACK_SUFFIX, "invoices": invoices_out,
            "guard": "failed_fallback_used"}
