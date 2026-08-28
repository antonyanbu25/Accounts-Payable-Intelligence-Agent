"""
Source C ingestion pipeline.

Reads every document in tax_docs/, and for each one:
  1. Asks Claude to extract structured metadata from the prose (category,
     state scope, effective dates, rate, TDS section, and the exact clause
     that states the rule) -- this is a constrained CLASSIFICATION/EXTRACTION
     task, not a computation. The extracted rate is never used to compute
     anything here; it's just metadata that lets deterministic filtering
     (tax_lookup.py) find the right document later.
  2. Embeds the full document text with a local model (no external API call).
  3. Stores both in a local Chroma collection.

Run this whenever a document in tax_docs/ changes. It is idempotent --
re-running clears and rebuilds the collection.
"""
import glob
import json
import os

import chromadb
from anthropic import Anthropic
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

TAX_DOCS_DIR = os.path.join(os.path.dirname(__file__), "..", "tax_docs")
CHROMA_DIR = os.path.join(os.path.dirname(__file__), "chroma_db")
VALID_CATEGORIES = ["Furniture", "Software", "Services", "Food", "Appliances"]

EXTRACTION_TOOL = {
    "name": "record_circular_metadata",
    "description": "Record structured metadata extracted from a tax/regulatory circular.",
    "input_schema": {
        "type": "object",
        "properties": {
            "doc_type": {
                "type": "string",
                "enum": ["GST_RATE", "TDS_RATE", "GST_SPLIT_RULE", "TDS_COMPUTATION_RULE", "UNRELATED_STATE_LEVY"],
                "description": "GST_RATE = sets a GST percentage for a category. TDS_RATE = sets a TDS percentage/section for a category. GST_SPLIT_RULE = the general CGST+SGST vs IGST rule, not category-specific. TDS_COMPUTATION_RULE = a rule about HOW to compute TDS (e.g. GST exclusion), not a rate itself. UNRELATED_STATE_LEVY = a real regulation that is NOT about GST on vendor purchases (e.g. a payroll/professional tax)."
            },
            "categories": {
                "type": "array", "items": {"type": "string", "enum": VALID_CATEGORIES + ["NONE", "OTHER"]},
                "description": "Which of the fixed category list this document's rate/rule applies to. Use NONE if this document is not category-specific (e.g. the general split rule). Use OTHER if it's about a category not in the fixed list at all (e.g. construction materials)."
            },
            "hsn_or_sac_codes": {"type": "array", "items": {"type": "string"}, "description": "Every HSN or SAC code explicitly mentioned as being governed by this document."},
            "state_scope": {"type": "string", "description": "'ALL' if this applies nationally regardless of state, otherwise the specific state name it is scoped to."},
            "effective_from": {"type": "string", "description": "ISO date YYYY-MM-DD this document's rule takes effect."},
            "effective_to": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD this rule stopped applying, or null if still in force."},
            "rate_pct": {"type": ["number", "null"], "description": "The GST or TDS percentage rate this document sets, or null if not applicable."},
            "tds_section": {"type": ["string", "null"], "description": "The Income Tax Act section number (e.g. '194J'), if this is a TDS document."},
            "key_clause": {"type": "string", "description": "The exact verbatim sentence(s), copied word-for-word from the document, that states the core rule -- for citation purposes. Do not paraphrase."},
        },
        "required": ["doc_type", "categories", "hsn_or_sac_codes", "state_scope", "effective_from", "effective_to", "key_clause"],
    },
}


def extract_metadata(client: Anthropic, doc_text: str, filename: str) -> dict:
    resp = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        tools=[EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "record_circular_metadata"},
        messages=[{
            "role": "user",
            "content": (
                f"Extract structured metadata from this tax/regulatory circular "
                f"(filename: {filename}). Read the full prose carefully -- some "
                f"documents state their effective date and category scope in a "
                f"clean header, others bury it in the middle of a sentence or "
                f"express it only by reference to an earlier document.\n\n"
                f"---\n{doc_text}\n---"
            ),
        }],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError(f"No tool_use block returned for {filename}")


def main():
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    print("Loading local embedding model (first run downloads it once)...")
    embedder = SentenceTransformer("all-MiniLM-L6-v2")

    chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
    try:
        chroma_client.delete_collection("tax_docs")
    except Exception:
        pass
    collection = chroma_client.create_collection("tax_docs")

    files = sorted(glob.glob(os.path.join(TAX_DOCS_DIR, "*.md")))
    files = [f for f in files if not os.path.basename(f).startswith("README")]

    ids, docs, embeddings, metadatas = [], [], [], []
    extracted_summary = {}

    for path in files:
        filename = os.path.basename(path)
        with open(path) as f:
            text = f.read()
        print(f"Extracting metadata: {filename} ...")
        meta = extract_metadata(client, text, filename)
        extracted_summary[filename] = meta

        categories = [c for c in meta.get("categories", []) if c not in ("NONE", "OTHER")]
        hsn_codes = meta.get("hsn_or_sac_codes") or []
        emb = embedder.encode(text).tolist()

        ids.append(filename)
        docs.append(text)
        embeddings.append(emb)
        metadatas.append({
            "filename": filename,
            "doc_type": meta["doc_type"],
            "categories": ",".join(categories) if categories else "",
            "hsn_or_sac_codes": ",".join(hsn_codes) if hsn_codes else "",
            "state_scope": meta["state_scope"],
            "effective_from": meta["effective_from"],
            "effective_to": meta.get("effective_to") or "",
            "rate_pct": meta.get("rate_pct") if meta.get("rate_pct") is not None else -1.0,
            "tds_section": meta.get("tds_section") or "",
            "key_clause": meta["key_clause"],
        })

    collection.add(ids=ids, documents=docs, embeddings=embeddings, metadatas=metadatas)
    print(f"\nIngested {len(ids)} documents into Chroma at {CHROMA_DIR}")

    out_path = os.path.join(os.path.dirname(__file__), "extracted_metadata.json")
    with open(out_path, "w") as f:
        json.dump(extracted_summary, f, indent=2)
    print(f"Wrote extracted metadata to {out_path}")


if __name__ == "__main__":
    main()
