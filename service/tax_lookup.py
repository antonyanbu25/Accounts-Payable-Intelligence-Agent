"""
Deterministic tax-rate lookup over the ingested Source C corpus.

Selection is NEVER done by embedding similarity -- filtering is exact-match
on category/HSN-SAC, state scope, and effective date range. Chroma's role
here is corpus storage and (optionally) a semantic ranking signal for
explanatory text -- it never decides a monetary rate. See plan v4.
"""
import datetime
import os
from dataclasses import dataclass, field
from typing import Optional

import chromadb

CHROMA_DIR = os.path.join(os.path.dirname(__file__), "chroma_db")


@dataclass
class TaxLookupResult:
    status: str  # "found" | "ambiguous" | "not_found"
    rate_pct: Optional[float] = None
    tds_section: Optional[str] = None
    doc_type: Optional[str] = None
    source_filename: Optional[str] = None
    key_clause: Optional[str] = None
    candidates: list = field(default_factory=list)  # populated when ambiguous


class TaxCorpus:
    """Loads the ingested Chroma collection once and serves deterministic lookups."""

    def __init__(self, chroma_dir: str = CHROMA_DIR):
        client = chromadb.PersistentClient(path=chroma_dir)
        collection = client.get_collection("tax_docs")
        raw = collection.get(include=["metadatas", "documents"])
        self.docs = []
        for meta, text in zip(raw["metadatas"], raw["documents"]):
            self.docs.append({**meta, "full_text": text})

    def _candidates(self, doc_type: str, category: str, hsn_or_sac: Optional[str],
                     invoice_date: datetime.date) -> list:
        """HSN/SAC code is the authoritative match key when the document lists
        codes and the query provides one -- category-name matching is only a
        fallback for documents with no codes attached (e.g. the general
        split rule). This matters in practice: a single circular can cover
        multiple categories (e.g. TDS Section 194J spans both Software and
        Services), and extraction may tag it with only one category name
        even though every relevant code is correctly listed -- matching on
        the code avoids depending on that category list being exhaustive."""
        out = []
        for d in self.docs:
            if d["doc_type"] != doc_type:
                continue
            codes = d["hsn_or_sac_codes"].split(",") if d["hsn_or_sac_codes"] else []
            cats = d["categories"].split(",") if d["categories"] else []
            if hsn_or_sac and codes:
                if hsn_or_sac not in codes:
                    continue
            elif category:
                if category not in cats:
                    continue
            eff_from = datetime.date.fromisoformat(d["effective_from"])
            eff_to = datetime.date.fromisoformat(d["effective_to"]) if d["effective_to"] else None
            if invoice_date < eff_from:
                continue
            if eff_to is not None and invoice_date > eff_to:
                continue
            out.append(d)
        return out

    def lookup_rate(self, doc_type: str, category: str, hsn_or_sac: Optional[str],
                     invoice_date: datetime.date) -> TaxLookupResult:
        """doc_type: 'GST_RATE' or 'TDS_RATE'."""
        candidates = self._candidates(doc_type, category, hsn_or_sac, invoice_date)
        if len(candidates) == 0:
            return TaxLookupResult(status="not_found")
        if len(candidates) > 1:
            return TaxLookupResult(status="ambiguous", candidates=candidates)
        d = candidates[0]
        return TaxLookupResult(
            status="found",
            rate_pct=float(d["rate_pct"]) if d["rate_pct"] != -1.0 else None,
            tds_section=d["tds_section"] or None,
            doc_type=d["doc_type"],
            source_filename=d["filename"],
            key_clause=d["key_clause"],
        )


def determine_tax(corpus: TaxCorpus, category: str, hsn_or_sac: str, invoice_date: datetime.date,
                   needs_tds: bool) -> dict:
    """Convenience wrapper combining a GST-rate lookup and, if applicable, a TDS lookup.
    Both lookups use the HSN/SAC code as the primary match key (see _candidates)."""
    gst = corpus.lookup_rate("GST_RATE", category, hsn_or_sac, invoice_date)
    result = {"gst": gst, "tds": None}
    if needs_tds:
        result["tds"] = corpus.lookup_rate("TDS_RATE", category, hsn_or_sac, invoice_date)
    return result
