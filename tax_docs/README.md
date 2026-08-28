# Tax & Regulatory Documents — Source C (synthetic)

Every document in this folder is **synthetic** — written for the DevRev Applied AI
Strategist assignment to look and read like a real Indian GST/TDS regulatory
circular, but not a real government issuance. The one genuine real-world fact
they're built around is the actual GST Council rate rationalization that took
effect **22 September 2025** — that date, and the general shape of "some
categories got cheaper, most stayed the same," is real; the specific rates,
notification numbers, and circular text here are invented for this project.

This corpus deliberately contains near-duplicate and hard-to-parse documents.
Since tax-rate *selection* in this system is fully deterministic (exact
HSN/SAC + state + effective-date match, never chosen by semantic similarity —
see the compute contract), these documents don't test retrieval precision so
much as they test **ingestion**: can the extraction pipeline correctly pull
the right category, state scope, and effective dates out of ordinary prose,
including cases where that information isn't sitting in a clean header field?

| File | Purpose |
|---|---|
| `gst_place_of_supply.md` | The one rule that applies across all categories — intra-state (CGST+SGST) vs. inter-state (IGST) |
| `gst_furniture.md` | Furniture rate (HSN 9403), unaffected by the reform |
| `gst_appliances.md` | Appliances rate (HSN 8516), unaffected — deliberately similar boilerplate to furniture (adjacent-category distractor) |
| `gst_services.md` | Services rate (SAC 998311), unaffected |
| `gst_software_pre_sep2025.md` | Software rate before the reform (18%) — now superseded |
| `gst_software_post_sep2025.md` | Software rate after the reform (12%) — current |
| `gst_food_pre_sep2025.md` | Food/catering rate before the reform (18%) — now superseded |
| `gst_food_post_sep2025_amendment.md` | Food/catering rate after the reform (5%) — **hard to ingest**: written as a cross-referencing amendment with scope/date buried in prose |
| `tds_professional_technical.md` | TDS for Software & Services (§194J) |
| `tds_catering_contracts.md` | TDS for Food/catering (§194C) |
| `tds_gst_exclusion_rule.md` | The real CBDT Circular 23/2017 rule: TDS excludes the GST component |
| `state_professional_tax_karnataka.md` | A genuinely state-specific levy, unrelated to GST rate — out-of-scope distractor that merely *sounds* relevant |
| `gst_construction_materials.md` | A different category entirely (not one this org purchases) — out-of-scope distractor |

`_ground_truth_metadata.json` in this folder is **not** part of the system —
it's a hand-authored answer key used to verify the ingestion pipeline
extracts the right metadata from each document once it's built (Day 2).
