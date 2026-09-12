# DealSieve — Updated Hackathon Build Plan

## One-line pitch

**DealSieve is a persistent acquisition agent that rejects opportunities, remembers exactly why they failed, watches the conditions that could change that decision, and interrupts the investor only when a deal becomes investable.**

The product is not an AI underwriting assistant. Underwriting is one internal capability. The product maintains an evidence-backed opinion about opportunities over time.

---

## 1. Core product idea

Traditional acquisition workflow:

```text
Find deal -> analyze -> decide -> forget/reject
```

DealSieve workflow:

```text
Encounter opportunity
-> form an evidence-backed opinion
-> reject if appropriate
-> remember why
-> calculate what would change the decision
-> monitor those conditions
-> re-evaluate automatically
-> interrupt human only if the decision changes
```

Core principles:

- Help professionals examine fewer opportunities, not more.
- A rejected opportunity is not necessarily a dead opportunity.
- Store not just `NO`, but `NO because X; YES if Y changes`.
- AI handles ambiguity; code controls financial truth; humans own irreversible decisions.

---

## 2. What makes this differentiated

Do not position DealSieve as:

- AI reads Offering Memorandums
- AI extracts rent rolls
- AI calculates cap rates
- AI summarizes CRE listings
- AI underwriting copilot

Those are increasingly commodity features.

Position DealSieve as:

> **Persistent opportunity monitoring with counterfactual decision thresholds.**

Distinctive lifecycle:

```text
Broker email arrives
-> DealSieve rejects deal
-> computes exact viability frontier
-> remembers opportunity
-> conditions change later
-> recognizes same opportunity
-> re-evaluates automatically
-> decision flips
-> human gets interrupted
```

---

## 3. Hackathon vertical slice

Build exactly one excellent story.

### Event A — Initial broker email

Broker sends:

```text
Off-market Sacramento small-bay industrial
Asking $1.55M
$126k stated NOI
OM attached
```

DealSieve automatically:

1. ingests email and attachments;
2. identifies the property;
3. extracts claims with provenance;
4. links to an existing opportunity if present;
5. reconstructs normalized economics;
6. applies immutable investment policy;
7. determines whether failure is structural or fixable;
8. computes the viability frontier.

Example result:

```text
WATCH

Broker cap                  8.13%
Normalized cap              6.65%
DSCR                         1.19x

Current asking price        $1,550,000
Maximum viable price        $1,270,000
Distance to viability       18.1%

Binding constraints
- normalized cap >= 8.0%
- DSCR >= 1.35x

Human attention required: NONE
```

### Event B — Price-drop email

Later broker sends:

```text
Seller reduced this to $1.25M. Any interest?
```

DealSieve:

1. recognizes the same opportunity;
2. creates a new immutable event;
3. detects the asking-price change;
4. re-runs underwriting;
5. observes policy now passes;
6. invokes independent skeptic review;
7. alerts investor via Telegram.

Example Telegram alert:

```text
DEAL #184 JUST BECAME INVESTABLE

Price
$1.55M -> $1.25M

Normalized cap
6.65% -> 8.24% PASS

DSCR
1.19x -> 1.41x PASS

Largest tenant
19% PASS

This property previously failed solely on valuation.

Still unresolved:
- roof age
- Phase I
- CAM reconciliation

[Review] [Draft Broker Questions] [Ignore]
```

The investor may approve a broker follow-up draft. No outbound message is sent without explicit human approval.

---

## 4. Product surfaces

### Email — ambient funnel

This is the default inbound channel. Brokers should keep behaving exactly as they do today.

```text
Broker -> email -> DealSieve
```

### Telegram — intentional input + interruption

Use Telegram for:

- pasting URLs;
- forwarding text;
- uploading PDFs;
- threshold-crossing alerts;
- approving broker questions;
- checking deal status.

Normal usage should not require commands, though `/status` and `/deal <id>` can exist.

### Dashboard — inspection only

Dashboard is secondary. Users open it after something becomes worth inspecting.

Show:

- current state;
- broker claims vs normalized economics;
- evidence;
- underwriting history;
- viability frontier;
- event timeline;
- skeptic concerns;
- broker correspondence.

Do not overbuild generic CRM/SaaS workflow features.

---

## 5. Opportunity state model

```text
NEW -> SCREENING -> DEAD | WATCH | NEAR | REVIEW
```

### DEAD

Structural failure that ordinary repricing does not fix.

Examples:

- unacceptable environmental issue;
- incompatible zoning;
- catastrophic physical issue;
- unacceptable tenant concentration;
- title/easement issue;
- functionally obsolete configuration.

Do not continuously monitor DEAD deals.

### WATCH

Economically unacceptable today but potentially investable later.

Examples:

- overpriced;
- insufficient DSCR;
- financing too expensive;
- NOI too low;
- seller terms unattractive.

Store a viability frontier.

### NEAR

Within configurable distance of passing. Initial rule: within 5% of viable purchase price.

### REVIEW

Passes all hard gates. Human attention is justified. This is not equivalent to BUY.

---

## 6. Viability frontier

Every WATCH opportunity must answer:

- Why did it fail?
- What is the binding constraint?
- What exact change would make it pass?

Initial hackathon implementation solves for purchase price only.

Example:

```json
{
  "current_price": 1550000,
  "max_viable_price": 1270000,
  "distance_pct": 0.1806,
  "binding_constraints": ["normalized_cap_rate", "dscr"],
  "possible_paths_to_viability": [
    {"variable": "purchase_price", "required_value": 1270000}
  ]
}
```

Future extensions may solve for:

- required NOI;
- required rent;
- maximum interest rate;
- seller-financing terms;
- occupancy improvement;
- equity contribution.

---

## 7. Event model

All opportunity changes are immutable events.

Examples:

```text
DEAL_DISCOVERED
EMAIL_RECEIVED
DOCUMENT_ADDED
ASKING_PRICE_CHANGED
NOI_CHANGED
RENT_ROLL_UPDATED
FINANCING_CHANGED
SELLER_FINANCING_ADDED
POLICY_CHANGED
UNDERWRITING_COMPLETED
STATUS_CHANGED
BROKER_MESSAGE_RECEIVED
HUMAN_APPROVED_REPLY
```

Never overwrite history.

Core model:

```text
Property
  -> Opportunity
      -> Immutable event stream
          -> Current derived state
```

---

## 8. Canonical opportunity identity

Multiple inputs may refer to the same property:

- broker email;
- Crexi/listing URL;
- OM PDF;
- updated OM;
- price-drop email;
- Telegram URL.

Resolve into one canonical opportunity using:

- normalized address;
- APN if available;
- broker property ID;
- canonical URL;
- attachment fingerprint;
- fuzzy address similarity;
- building name;
- city + square footage.

Use deterministic matching where confidence is high. Use model-assisted reconciliation for ambiguous cases. Never silently merge low-confidence candidates.

---

## 9. Immutable investment policy

Create `config/investment_policy.yaml`:

```yaml
capital:
  acquisition_equity: 500000
  reserve_min: 50000
  reserve_target: 75000

purchase:
  preferred_min: 1100000
  preferred_max: 1700000
  absolute_max: 2000000

property:
  target_type: small_bay_industrial
  tenant_count_min: 5
  largest_tenant_pct_max: 0.25
  preferred_suite_sqft:
    min: 1000
    max: 5000

underwriting:
  min_normalized_cap_rate: 0.08
  min_base_dscr: 1.35

financing:
  assumed_interest_rate: 0.07
  amortization_years: 25

normalization:
  management_fee_pct: 0.05
  normalized_vacancy_pct: 0.05
  require_property_tax_reset: true
  require_capex_reserve: true

stress:
  vacancy_pct: 0.20
  rent_haircut_pct: 0.10
  appreciation_pct: 0
  tax_benefit_assumption: 0

classification:
  near_threshold_pct: 0.05
```

Rules:

- agents may read policy;
- agents may not modify policy;
- human policy changes are explicit and versioned;
- every underwriting run stores the policy version used.

---

## 10. Architecture philosophy

```text
MESSY WORLD
  -> LLM / agents
  -> STRUCTURED EVIDENCE
  -> DETERMINISTIC FINANCE
  -> IMMUTABLE POLICY
  -> PERSISTENT OPINION
  -> EVENT CHANGE
  -> RE-EVALUATION
  -> HUMAN INTERRUPTION ONLY IF WARRANTED
```

---

## 11. Strands architecture

Avoid multi-agent theater.

Use one primary lifecycle agent plus one independent reviewer.

```text
Email / Telegram
      |
      v
Acquisition Agent
      |
      +-> extraction tools
      +-> reconciliation tools
      +-> evidence tools
      |
      v
Deterministic Underwriter
      |
      v
Policy Evaluator
      |
      v
Viability Solver
      |
      v
Opportunity Memory
      |
      +-> DEAD/WATCH
      |
      +-> REVIEW candidate -> Skeptic Agent -> Human Escalation
```

### Acquisition Agent responsibilities

- understand incoming message;
- identify property;
- find existing opportunity;
- extract new evidence;
- reconcile contradictions;
- determine what changed;
- invoke deterministic tools;
- update opportunity state;
- decide whether human interruption is warranted.

It may not:

- override policy;
- invent missing inputs;
- alter deterministic calculations;
- send broker email without approval.

### Skeptic Agent

Runs only when an opportunity enters REVIEW.

Mission:

> Find reasons this attractive-looking acquisition should still be rejected.

Focus on:

- unsupported assumptions;
- lease rollover;
- tenant concentration;
- environmental risk;
- roof/HVAC/paving;
- zoning;
- easements/access;
- seller-related tenants;
- suspicious expenses;
- financing risk;
- claims without evidence.

It does not recalculate authoritative finance.

---

## 12. Evidence model

Every extracted claim retains provenance.

```json
{
  "field": "stated_noi",
  "value": 126000,
  "source_document": "offering_memorandum.pdf",
  "location": "page 7",
  "source_timestamp": "2026-09-12T...",
  "confidence": 0.97
}
```

Contradictory observations are preserved. Reconciliation chooses a working value without deleting conflicting source evidence.

---

## 13. Deterministic underwriting engine

Directory: `dealsieve/underwriting/`

Required functions:

```python
normalize_income(...)
normalize_expenses(...)
estimate_reassessed_property_tax(...)
calculate_noi(...)
calculate_cap_rate(...)
calculate_monthly_debt_service(...)
calculate_annual_debt_service(...)
calculate_dscr(...)
calculate_ltv(...)
calculate_cash_on_cash(...)
calculate_first_year_principal_paydown(...)
stress_test(...)
solve_max_viable_price(...)
evaluate_policy(...)
```

Models never own authoritative arithmetic.

---

## 14. Broker math vs buyer math

Primary visual:

| Metric | Broker | DealSieve |
|---|---:|---:|
| NOI | $126,000 | $103,000 |
| Cap rate | 8.13% | 6.65% |
| Vacancy | 0% | 5% |
| Management | $0 | Included |
| Property-tax reset | Ignored | Included |
| CapEx reserve | $0 | Included |
| DSCR | — | 1.19x |

Expose exactly why DealSieve disagrees. No black-box score.

---

## 15. Email ingestion

### AWS/demo path

```text
Broker
 -> SES inbound
 -> S3 raw MIME
 -> Lambda/EventBridge
 -> DealSieve ingestion endpoint
 -> Acquisition Agent
```

Preserve raw MIME and attachments.

### Local path

Fixtures:

```text
fixtures/email/initial_bad_deal.eml
fixtures/email/price_drop.eml
fixtures/email/structural_failure.eml
```

Commands:

```bash
python scripts/inject_email.py fixtures/email/initial_bad_deal.eml
python scripts/inject_email.py fixtures/email/price_drop.eml
```

Local and SES paths must hit the same domain ingestion interface.

---

## 16. Telegram interface

Keep minimal.

Accept:

- plain URL;
- PDF;
- forwarded text;
- `/status`;
- `/deal <id>`.

Primary notification:

```text
Deal #184 crossed your investment threshold.
```

For outbound broker questions:

```text
[Approve] [Edit] [Ignore]
```

No autonomous send.

---

## 17. Dashboard

Keep intentionally small.

### Opportunity intelligence

```text
84 opportunities encountered
71 rejected/dead
8 watching
4 near threshold
1 review

Conditions changed this week: 3
Deals crossing threshold: 1
Human interruptions: 1
```

### Watchlist

Sort by distance to viability.

### Deal detail

Show:

- current state;
- broker vs DealSieve;
- viability frontier;
- binding constraints;
- evidence;
- event timeline;
- underwriting history;
- skeptic concerns;
- broker correspondence.

---

## 18. Persistence model

Recommended tables:

```text
properties
opportunities
opportunity_events
source_documents
evidence_observations
broker_contacts
underwriting_runs
policy_versions
viability_frontiers
outbound_drafts
notifications
```

`underwriting_runs` are immutable.

---

## 19. Model-provider abstraction

Do not burn AWS credits during development.

Support:

```text
MODEL_BACKEND=cli
MODEL_BACKEND=openai_compatible
MODEL_BACKEND=bedrock
```

Interface:

```python
class ModelBackend(Protocol):
    def create_model(self, purpose: str): ...
```

Purposes:

```text
extraction
reconciliation
skeptic
escalation
```

---

## 20. Local CLI proxy

Create `tools/cli_model_proxy/` exposing:

```text
POST /v1/chat/completions
```

Configuration:

```yaml
providers:
  claude:
    command: "${CLAUDE_CMD}"
  codex:
    command: "${CODEX_CMD}"
  agy:
    command: "${AGY_CMD}"
```

Example local env:

```bash
CLAUDE_CMD='claude -p'
CODEX_CMD='codex -p ...'
AGY_CMD='agy ...'
```

The adapter owns CLI-specific syntax. Do not spread assumptions through the codebase.

Proxy behavior:

1. accepts OpenAI-like request;
2. serializes context;
3. invokes configured CLI;
4. captures stdout;
5. validates structured output;
6. returns OpenAI-compatible response.

Do not recreate complex tool-calling semantics in the proxy. The application controls deterministic tools.

---

## 21. Structured outputs

Use Pydantic on every model boundary.

```python
class Evidence(BaseModel):
    field: str
    value: Any
    source: str
    confidence: float

class ExtractedDeal(BaseModel):
    address: str | None
    asking_price: Decimal | None
    stated_noi: Decimal | None
    building_sqft: int | None
    tenant_count: int | None
    occupancy_pct: Decimal | None
    evidence: list[Evidence]
    missing_fields: list[str]
```

On schema failure: retry once with validation feedback, then fail gracefully.

---

## 22. Fixtures

### Fixture A — obvious economic failure

Validates rejection.

### Fixture B — price-fixable failure

Primary demo property.

Initial:

```text
Price             $1.55M
Normalized cap    ~6.6%
DSCR              ~1.19x
Status            WATCH
Viable around     ~$1.27M
```

After price-drop event:

```text
Price             $1.25M
Normalized cap    >8%
DSCR              >1.35x
Status            REVIEW
```

### Fixture C — structural failure

Example: single tenant = 78% of revenue.

Even after a large price reduction expected state remains DEAD.

This proves DealSieve does not confuse cheap with investable.

---

## 23. Tests

Mandatory deterministic boundary tests:

```text
cap = 8.000%     PASS
cap = 7.999%     FAIL

DSCR = 1.350     PASS
DSCR = 1.349     FAIL
```

Test:

- NOI;
- cap rate;
- debt service;
- amortization;
- DSCR;
- LTV;
- cash-on-cash;
- viability frontier;
- stress tests;
- property-tax normalization.

Identity tests must ensure original email + price-drop email resolve to the same opportunity.

Critical E2E test:

```text
inject initial email
-> deal created
-> underwrite
-> WATCH
-> viability frontier stored

inject price-drop email
-> same deal found
-> price-change event created
-> new underwriting run
-> REVIEW
-> Telegram alert generated
```

This is the highest-priority test in the repository.

---

## 24. Repository layout

```text
dealsieve/
├── README.md
├── LICENSE
├── STATUS.md
├── architecture/
│   └── architecture.png
├── config/
│   └── investment_policy.yaml
├── dealsieve/
│   ├── api/
│   ├── agents/
│   │   ├── acquisition.py
│   │   └── skeptic.py
│   ├── graph/
│   │   └── opportunity_graph.py
│   ├── ingestion/
│   │   ├── email.py
│   │   ├── telegram.py
│   │   └── url.py
│   ├── identity/
│   │   └── resolver.py
│   ├── evidence/
│   │   ├── extraction.py
│   │   └── reconciliation.py
│   ├── underwriting/
│   │   ├── normalize.py
│   │   ├── financing.py
│   │   ├── policy.py
│   │   └── viability.py
│   ├── models/
│   │   ├── backend.py
│   │   ├── proxy.py
│   │   └── bedrock.py
│   ├── persistence/
│   ├── events/
│   ├── notifications/
│   └── schemas/
├── frontend/
├── fixtures/
│   ├── emails/
│   ├── om/
│   ├── rent_roll/
│   └── expected/
├── scripts/
│   ├── inject_email.py
│   ├── inject_price_drop.py
│   └── seed_demo.py
├── tools/
│   └── cli_model_proxy/
├── tests/
│   ├── underwriting/
│   ├── identity/
│   ├── agents/
│   └── e2e/
└── infra/
```

---

## 25. Build order

1. Repository skeleton, README, license, STATUS.md, CI.
2. Deterministic finance engine.
3. Viability-frontier solver.
4. Persistent opportunity + event model.
5. Local `.eml` ingestion.
6. Identity resolution + price-change detection.
7. Complete local WATCH -> REVIEW loop.
8. Strands Acquisition Agent.
9. Local CLI proxy for Claude/Codex/agy.
10. Telegram input + alert + approval.
11. Skeptic Agent.
12. Minimal dashboard.
13. SES inbound email.
14. AgentCore Runtime deployment.
15. Optional AgentCore Browser / richer OM parsing / Memory only if core demo is stable.

The full WATCH -> changed condition -> REVIEW loop must work locally before cloud deployment.

---

## 26. Parallel workstreams

### A — Finance Engine

Normalization, financing, policy, viability.

### B — Opportunity/Event Model

Canonical identity, immutable events, derived current state.

### C — Agent Layer

Strands, extraction, reconciliation, skeptic, model adapters.

### D — Channels

Local email, SES, Telegram, outbound approval.

### E — UI

Build against seeded local DB; do not wait for backend.

### F — AWS + Submission

AgentCore, AWS budget, deployment, README, architecture diagram, video, Devpost copy.

---

## 27. AWS budget policy

Approximate credits: $100.

Develop locally. Do not use Bedrock for prompt iteration.

Budget warnings:

```text
$25
$50
$75
$90
```

Policy:

```text
Unit tests                LOCAL
Finance tests             LOCAL
Prompt iteration          CLI proxy
Email fixture tests       LOCAL
Telegram logic            LOCAL
UI                        LOCAL
E2E fixtures              LOCAL

AWS only for:
SES integration
AgentCore deployment
Bedrock smoke tests
final rehearsals
final demo
```

---

## 28. Failure-tolerant demo

### Level 1 — local

`.eml` fixtures + CLI model proxy + SQLite + local frontend + fake Telegram adapter.

### Level 2 — real Telegram

Same backend plus actual Telegram bot.

### Level 3 — full cloud

SES + Strands + Bedrock + AgentCore + Telegram + dashboard.

No external dependency may be able to sink the demo.

---

## 29. Demo script

### 0:00–0:30 — problem

> Acquisition professionals don't need another AI that analyzes opportunities they've already chosen to inspect. They need an agent that decides which opportunities deserve their attention in the first place.

### 0:30–0:55 — immutable policy

Show:

```text
$500k equity
$75k reserves
8% minimum normalized cap
1.35x minimum DSCR
<=25% tenant concentration
```

### 0:55–1:45 — ambient broker email

Email arrives. DealSieve extracts, normalizes, underwrites and returns:

```text
WATCH
Broker: 8.13%
DealSieve: 6.65%
Viable below ~$1.27M
```

Explain that the agent remembers the counterfactual.

### 1:45–2:20 — persistent opinion

Show history:

```text
Sep 12
$1.55M
WATCH
Reason: valuation
```

No human notification.

### 2:20–3:10 — world changes

New broker email:

```text
Seller reduced to $1.25M
```

DealSieve recognizes same opportunity, detects change and re-underwrites:

```text
REVIEW
8.24% normalized cap
1.41x DSCR
```

### 3:10–3:40 — human interruption

Telegram:

```text
This deal just became investable.
```

Show what changed and what remains unknown.

### 3:40–4:10 — skeptic

Skeptic identifies roof age, Phase I and CAM reconciliation as missing. Draft broker questions; human approves.

### 4:10–4:35 — architecture

Explain:

```text
Email + Telegram
 -> Strands Acquisition Agent
 -> Deterministic finance
 -> Immutable policy
 -> Persistent opportunity memory
 -> Skeptic
 -> Human
```

### 4:35–5:00 — impact

Show:

```text
84 opportunities encountered
71 rejected
8 watching
4 near threshold
1 crossed threshold
Human interruptions: 1
```

Close:

> DealSieve doesn't just remember your decisions. It remembers what would cause those decisions to change.

---

## 30. Product metrics

Primary: **human interruptions per useful opportunity**.

Secondary: **human attention saved**.

Also track:

- opportunities encountered;
- automatic rejections;
- WATCH opportunities;
- threshold crossings;
- false-positive escalations;
- time from condition change to re-evaluation.

Avoid vanity metric: `documents analyzed`.

---

## 31. Explicitly cut from hackathon scope

Do not build:

- nationwide assessor ingestion;
- Sacramento parcel universe;
- owner cold outreach;
- broker CRM;
- auth/billing;
- arbitrary lease OCR;
- tax optimization;
- 1031 logic;
- production-grade market-rent comp engine;
- automated LOIs;
- autonomous broker negotiation;
- property management;
- portfolio accounting;
- mobile app;
- arbitrary messaging platforms;
- full OpenClaw integration.

---

## 32. Post-hackathon roadmap

1. Broker-email monitoring.
2. Listing monitoring.
3. Off-market owner universe.
4. Loan-rate and seller-financing monitoring.
5. Market-rent and transaction comps.
6. Automated owner outreach.
7. Extend primitive to small businesses, franchises, private credit and equipment acquisitions.

Reusable primitive:

> **Persistent opportunity state + deterministic investment policy + monitored counterfactual thresholds.**

---

## 33. Coding-agent operating instructions

1. Work autonomously and choose sensible defaults.
2. Optimize for a reliable hackathon demo, not production completeness.
3. Preserve the boundary between probabilistic extraction, deterministic finance, immutable policy and human irreversible decisions.
4. Never weaken thresholds to make a fixture pass.
5. Never let an LLM own authoritative financial arithmetic.
6. Maintain immutable event and underwriting history.
7. Treat WATCH -> condition change -> REVIEW as the highest-priority end-to-end path.
8. Default model development to local CLI proxies.
9. Keep AWS provider logic behind interfaces.
10. Never embed credentials.
11. Write tests before optional features.
12. Keep `STATUS.md` current.
13. Commit at coherent milestones.
14. Cut optional features if they threaten the core demo.
15. Ensure full local fallback.
16. A deal producing no human interruption is a successful result.
17. Zero qualifying properties is a legitimate output.
18. Preserve evidence provenance.
19. Never silently resolve contradictory source information.
20. Human approval is mandatory before outbound broker actions.

---

## 34. STATUS.md format

```markdown
# Current Status

## Completed
- ...

## Working Now
- ...

## Blockers
- ...

## Next Three Tasks
1. ...
2. ...
3. ...

## Demo Health
Local WATCH -> REVIEW: PASS/FAIL
Telegram: PASS/FAIL
SES: PASS/FAIL
AgentCore: PASS/FAIL
```

---

## 35. Definition of done

```text
public repository
MIT or Apache license
README
architecture diagram

Strands materially used

immutable investment policy
deterministic underwriting tested
viability frontier implemented
immutable event history implemented

initial broker email -> WATCH
price-drop email -> same deal
automatic re-underwrite
WATCH -> REVIEW
Telegram threshold alert

independent skeptic review
human approval before outbound broker action

local CLI provider abstraction
local demo fallback
minimal dashboard

real SES if feasible
AgentCore deployment if feasible

<=5 minute demo
submission copy
reproducible setup instructions
```

---

## Final product statement

**DealSieve is not an AI that reads real-estate documents.**

It is an agent that develops and maintains an opinion about every acquisition opportunity it encounters.

It can say:

> Not now.

It knows:

> Why not.

It calculates:

> What would change my mind.

And it watches quietly until that happens.

Only then does it ask for a human's attention.
