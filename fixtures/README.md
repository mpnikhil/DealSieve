# Fixtures

| File | Purpose | Expected |
|---|---|---|
| `emails/01_initial_offer.eml` | Broker intro for the demo property, OM attached | WATCH, valuation-only failure, max viable price in [$1.25M, $1.47M) |
| `emails/02_price_drop.eml` | Same broker, same thread: "Seller reduced to $1.25M" | same opportunity, REVIEW, one alert |
| `emails/03_structural_single_tenant.eml` | Different property, one tenant = ~78% of rent, cheap | DEAD, silent, no viable price |
| `emails/04_obvious_economic_failure.eml` | Overpriced, far from viable | WATCH, distance well above NEAR band |
| `emails/05_inspection_report.eml` | Same thread (replies to 02): broker sends the property condition report PDF the seller commissioned | same opportunity, REVIEW -> NEAR, roof capex enters the all-in basis |
| `om/*.md` | Offering memorandum text attached to the emails | |
| `om/05_power_inn_property_condition_report.pdf` | Property condition assessment PDF (built by `scripts/build_fixture_pdfs.py`); 7 pages, 3 embedded photos | attached to `emails/05_inspection_report.eml` |
| `photos/roof_ponding.jpg`, `photos/roof_membrane.jpg`, `photos/rooftop_hvac.jpg` | Real photographs embedded in the PDF above; sources/licenses in `fixtures/ATTRIBUTION.md` | |
| `expected/claims_<name>.json` | Golden `ExtractedClaims` for each email (what a correct extraction yields) | used by scripted model + tests |
| `expected/analysis_05_inspection_report.json` | Golden `DocumentAnalysis` body for the inspection report PDF (findings, answers, capex_items, red_flags) | validated via `DocumentAnalysis.model_validate` |
| `expected/<name>.json` | Expected status and key metrics with tolerances | used by tests |
| `scripted/<name>.json` | Scripted model turns for the offline E2E test | |
