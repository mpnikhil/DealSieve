# Fixtures

| File | Purpose | Expected |
|---|---|---|
| `emails/01_initial_offer.eml` | Broker intro for the demo property, OM attached | WATCH, valuation-only failure, max viable price in [$1.25M, $1.47M) |
| `emails/02_price_drop.eml` | Same broker, same thread: "Seller reduced to $1.25M" | same opportunity, REVIEW, one alert |
| `emails/03_structural_single_tenant.eml` | Different property, one tenant = ~78% of rent, cheap | DEAD, silent, no viable price |
| `emails/04_obvious_economic_failure.eml` | Overpriced, far from viable | WATCH, distance well above NEAR band |
| `om/*.md` | Offering memorandum text attached to the emails | |
| `expected/claims_<name>.json` | Golden `ExtractedClaims` for each email (what a correct extraction yields) | used by scripted model + tests |
| `expected/<name>.json` | Expected status and key metrics with tolerances | used by tests |
| `scripted/<name>.json` | Scripted model turns for the offline E2E test | |
