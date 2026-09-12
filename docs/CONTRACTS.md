# DealSieve module contracts and workstream ownership

Read this before writing code. `dealsieve/schemas/core.py` is the source of truth for every type named here.

## Architecture in one paragraph

Messy inputs (broker emails, Telegram pastes, OM attachments) become an `InboundMessage`. A **Strands Acquisition
Agent** reads it and extracts `ExtractedClaims` with provenance, then calls deterministic tools. The tools resolve
the property to one canonical `Opportunity`, reconcile claims into `WorkingValues`, run the **deterministic
underwriting engine** under an **immutable policy**, store an immutable `UnderwritingResult`, append immutable
`OpportunityEvent`s, and classify the opportunity as DEAD / WATCH / NEAR / REVIEW with a **viability frontier**
(the exact price at which it would pass). When a later message changes a condition and the status crosses into
REVIEW, an independent **Skeptic Agent** looks for reasons to still say no, broker questions are drafted for
human approval, and the human is interrupted exactly once. Everything else stays silent.

```
InboundMessage -> Acquisition Agent (Strands) -> tools -> [identity] -> [reconcile] -> [underwrite] -> [classify]
                                                         -> events + runs (immutable) -> Opportunity (derived)
                                                         -> REVIEW? -> Skeptic Agent -> draft -> notify human
```

## Ownership

| WS | Owner model | Owns (may edit) | Tests |
|---|---|---|---|
| W1 Finance + fixtures | Codex | `dealsieve/underwriting/**`, `fixtures/om/**`, `fixtures/emails/**`, `fixtures/expected/**` | `tests/underwriting/**` |
| W2 Persistence, identity, ingestion, reconcile | Sonnet | `dealsieve/persistence/**`, `dealsieve/identity/**`, `dealsieve/ingestion/email.py`, `dealsieve/ingestion/text.py`, `dealsieve/evidence/**` | `tests/persistence/**`, `tests/identity/**` |
| W3 Agents, model providers, pipeline | Opus | `dealsieve/agents/**`, `dealsieve/models/**`, `dealsieve/pipeline.py`, `fixtures/scripted/**` | `tests/agents/**` (+ makes `tests/e2e` pass at integration) |
| W4 API, notifications, channels, scripts | Sonnet | `dealsieve/api/**`, `dealsieve/notifications/**`, `dealsieve/ingestion/telegram.py`, `dealsieve/agentcore_app.py`, `dealsieve/cli.py`, `scripts/**` | `tests/api/**` |
| W5 Dashboard | agy (Gemini) | `frontend/**` | frontend's own |

Shared, edit only via the orchestrator: `dealsieve/schemas/**`, `dealsieve/policy/**`, `config/**`, `pyproject.toml`, `tests/e2e/**`, `tests/conftest.py`.
If you need a schema field, add it to your final report; do not fork the type.

Conventions: Python 3.12, `Decimal` for all money and rates, `from __future__ import annotations`, type hints
everywhere, `ruff` clean. Run only your own tests: `python -m pytest tests/<area> -q`. No `git commit`.

---

## W1: deterministic underwriting engine

Pure functions. Same inputs, same outputs. No I/O, no randomness, no model calls. Quantize money to cents
(`Decimal("0.01")`) and rates to 6 dp only at the output boundary; compare and compute with full precision.

Inputs: `WorkingValues v`, `InvestmentPolicy P` (see `dealsieve/policy/loader.py`), `price` (defaults to
`v.asking_price`).

### normalize.py: `normalize_economics(v, P, price) -> NormalizedEconomics`
```
GPR            = v.gross_scheduled_income
vacancy_pct    = max(P.normalization.normalized_vacancy_pct, v.stated_vacancy_pct)
vacancy_loss   = GPR * vacancy_pct
EGI            = GPR - vacancy_loss + v.other_income
property_tax   = price * P.normalization.property_tax_rate_pct      if require_property_tax_reset else stated (0 if None)
                 basis "1.25% of price (reassessed at sale)"
insurance      = max(stated or 0, sqft * min_insurance_per_sqft)      (floor only when sqft known)
repairs_maint  = max(stated or 0, EGI * min_repairs_pct_of_egi)
utilities      = stated or 0                                          basis "as stated"
management     = max(stated or 0, EGI * management_fee_pct)           basis "5% of EGI"
cam_other      = stated or 0
capex_reserve  = sqft * capex_reserve_per_sqft if require_capex_reserve and sqft else 0   (broker=None, i.e. broker did not include it)
total_expenses = sum of the above
NOI            = EGI - total_expenses
broker_noi     = v.stated_noi ; broker_cap = broker_noi / price ; normalized_cap = NOI / price
price_per_sqft, noi_per_sqft when sqft known
```
Each `ExpenseLine.broker` is the stated figure (None when the source did not state it), `normalized` is ours,
`basis` says how.

### financing.py
```
monthly_payment(principal, annual_rate, years)  standard amortization; rate 0 -> principal / n
year1_principal_paydown(principal, annual_rate, years)  sum of principal portions of payments 1..12
compute_financing(noi, price, P) -> FinancingResult:
  closing_costs      = price * P.financing.closing_cost_pct
  total_cost         = price + closing_costs
  deployable_equity  = P.capital.acquisition_equity - P.capital.reserve_target
  loan_amount        = max(0, total_cost - deployable_equity)
  equity_deployed    = total_cost - loan_amount
  ltv                = loan_amount / price
  annual_ds          = 12 * monthly_payment(loan_amount, assumed_interest_rate, amortization_years)
  dscr               = noi / annual_ds   (loan 0 -> Decimal("999"))
  cash_flow_after_debt = noi - annual_ds
  cash_on_cash       = cash_flow_after_debt / equity_deployed
```
Equity is fixed by policy; price drives the loan. That is what makes DSCR and LTV price-dependent.

### gates.py: `evaluate_gates(v, normalized, financing, P) -> list[GateResult]`
Exactly these gate keys, in this order:

| gate | kind | comparator | threshold | actual | price_dependent |
|---|---|---|---|---|---|
| `tenant_count_min` | structural | >= | P.property.tenant_count_min | v.tenant_count | no |
| `largest_tenant_pct_max` | structural | <= | P.property.largest_tenant_pct_max | v.largest_tenant_pct | no |
| `absolute_max_price` | economic | <= | P.purchase.absolute_max | price | yes |
| `max_ltv` | economic | <= | P.financing.max_ltv | financing.ltv | yes |
| `min_normalized_cap_rate` | economic | >= | P.underwriting.min_normalized_cap_rate | normalized_cap | yes |
| `min_base_dscr` | economic | >= | P.underwriting.min_base_dscr | financing.dscr | yes |

Boundary semantics are inclusive: cap 0.080000 passes, 0.079999 fails; DSCR 1.35 passes, 1.349999 fails.
If a structural input is unknown (None), the gate passes with description "not verifiable: <field> unknown";
the skeptic flags it later. Never invent a value.

### viability.py: `solve_max_viable_price(v, P) -> ViabilityFrontier`
Every economic gate is monotone in price (lower price: higher cap, smaller loan, higher DSCR, lower LTV).
Bisection on price in (0, P.purchase.absolute_max] until the interval is < $1, then quantize down to cents.
`max_viable_price` = largest price where all economic gates pass; None if structural failures exist or if no
price in range passes. `distance_pct = (current - max_viable) / current` (negative when current already passes;
report 0 in that case). `binding_constraints` = economic gates that fail at `max_viable_price + $1000`.
`paths` = one `ViabilityPath(variable="purchase_price", current_value=asking, required_value=max_viable)`.
`structural_failures` = structural gate keys that failed.

### stress.py: `run_stress(v, P, price) -> list[StressResult]`
Scenarios `vacancy_20pct` (vacancy = P.stress.vacancy_pct), `rent_haircut_10pct` (GPR * (1 - haircut)),
`combined`. Recompute NOI and DSCR at `price`; `covers_debt = dscr >= P.stress.min_stress_dscr`.

### classify.py: `classify(gates, viability, price, P) -> OpportunityStatus`
any structural failed -> DEAD; all gates pass -> REVIEW; else distance_pct <= near_threshold_pct -> NEAR; else WATCH.

### compare.py: `broker_vs_dealsieve(v, normalized, financing) -> list[ComparisonRow]`
Rows: NOI, Cap rate, Vacancy, Management, Property tax, CapEx reserve, DSCR, Price / sf. Format money as
`$126,000`, rates as `8.13%`, DSCR as `1.19x`, broker column `None` where the source gave nothing ("—" in UI).

### engine.py: `run_underwriting(v, P, *, opportunity_id, trigger_event_id=None) -> UnderwritingResult`
Compose the above at `v.asking_price`. `failure_summary` examples:
`"Fails on valuation: normalized cap 6.42% < 8.00%, DSCR 1.05x < 1.35x"`,
`"Structural: largest tenant 78% > 25%, 2 tenants < 5"`, `"Passes all gates at $1,250,000"`.

### Fixtures (W1 owns the numbers; prose can be plain)
Policy is frozen. Tune property inputs, never thresholds. Verify with the engine before writing the expected files.

**Fixture 01/02, the demo property** (Sacramento, CA small-bay industrial, e.g. "8 unit multi-tenant industrial on
Power Inn Rd, Sacramento CA 95826", ~20,000 sf, 8 tenants, largest tenant ~19% of rent). Starting point that
lands in range: GPR $180,000; stated expenses total $54,000 (current property tax $15,500, insurance $8,400,
R&M $12,000, utilities $11,000, CAM/other $7,100); stated NOI $126,000; asking $1,550,000 (broker cap 8.13%).
Targets at $1,550,000: normalized cap in [6.3%, 6.9%], DSCR in [1.00, 1.25], all structural gates pass,
`max_viable_price` in [$1,270,000, $1,330,000] (so the drop to $1,250,000 flips it and it is not NEAR today).
Targets at $1,250,000: normalized cap >= 8.2%, DSCR >= 1.40, status REVIEW.
- `fixtures/om/01_power_inn_om.md`: an offering memorandum in markdown: summary, rent roll table (tenant, suite,
  sf, annual rent, lease end), stated expenses, stated NOI/cap, physical description (roof age NOT stated, Phase I
  NOT mentioned, CAM reconciliation NOT provided; the skeptic should notice these gaps).
- `fixtures/emails/01_initial_offer.eml`: RFC 822, `Message-ID: <om-2026-0912-power-inn@brokerage.example>`,
  From a broker, subject "Off-market: 8-unit small-bay industrial, Sacramento — $1.55M / 8.13% cap", plain-text body
  stating asking $1,550,000 and NOI $126,000, with the OM attached as `text/markdown` (filename `Power_Inn_OM.md`).
- `fixtures/emails/02_price_drop.eml`: same From, subject "Re: Off-market: 8-unit ...", `In-Reply-To` and
  `References` pointing at message 01, body: "Seller reduced this to $1.25M. Any interest?" and nothing else
  about the property. No attachment.
- `fixtures/emails/03_structural_single_tenant.eml` + `fixtures/om/03_*_om.md`: a different address, ~12,000 sf,
  2 tenants, largest = ~78% of rent, asking $950,000, stated NOI ~$92,000 (looks cheap: 9.7% cap). Expected DEAD
  with `structural_failures` containing `largest_tenant_pct_max` and `tenant_count_min`.
- `fixtures/emails/04_obvious_economic_failure.eml`: another address, asking $2,400,000, NOI ~$120,000; WATCH far
  from viable. Optional but cheap.
- `fixtures/expected/claims_<name>.json`: the golden `ExtractedClaims` for each email (JSON via
  `model_dump(mode="json")`), including `evidence` entries with `source_document` = the email Message-ID for body
  facts and the attachment filename for OM facts, `location` like "OM: Rent Roll table" and a short `quote`. For
  02: only `asking_price`, `is_price_change: true`, one evidence item. These are what a correct extractor yields.
- `fixtures/expected/<name>.json`: `{"status": "...", "normalized_cap_rate": [lo, hi], "dscr": [lo, hi],
  "max_viable_price": [lo, hi] | null, "structural_failures": [...]}`.
- `tests/underwriting/`: boundary tests for every gate, amortization against a known payment table, tax reset,
  viability solver (bisection finds the frontier, frontier price passes, frontier + $1000 fails), stress,
  classification, and a test that runs the engine on each `claims_*.json` (reconciled by hand into WorkingValues
  inside the test) and asserts the `expected/*.json` ranges.

---

## W2: persistence, identity, ingestion, reconciliation

### persistence/db.py + repo.py
stdlib `sqlite3`, WAL mode, `PRAGMA foreign_keys=ON`. Nested contracts stored as JSON TEXT produced by
`json.dumps(model.model_dump(mode="python"), default=str)` so `Decimal` and `datetime` round-trip exactly
(the API uses `model_dump_json()` instead, which emits numbers). Read back with `Model.model_validate(json.loads(...))`.
Equality after round trip must hold: `repo.get_underwriting_run(id) == run` for the object you stored.

Tables (scalar columns for what we filter/sort on, `json` for the rest):
```
inbound_messages(message_id PK, channel, received_at, sender, subject, thread_id, opportunity_id NULL, json)
properties(property_id PK, normalized_address UNIQUE, apn, json)
opportunities(opportunity_id PK, deal_number UNIQUE, property_id FK, status, broker_property_ref, listing_url, updated_at, json)
opportunity_events(event_id PK, opportunity_id FK, seq, type, occurred_at, source_message_id, json, UNIQUE(opportunity_id, seq))
source_documents(id PK, opportunity_id FK, message_id, filename, sha256, text)
evidence_observations(evidence_id PK, opportunity_id FK, field, json)
underwriting_runs(run_id PK, opportunity_id FK, policy_version, created_at, status, json)
policy_versions(policy_version PK, name, raw_yaml, first_seen_at)
skeptic_reports(report_id PK, opportunity_id FK, run_id, created_at, json)
outbound_drafts(draft_id PK, opportunity_id FK, status, created_at, json)
notifications(notification_id PK, opportunity_id FK, kind, created_at, json)
```
Implement every method in the `Repo` stub, plus these lookups used by identity:
```
link_message_to_opportunity(message_id, opportunity_id)
find_opportunity_id_by_message_id(message_id) -> str | None
find_opportunity_id_by_thread_id(thread_id) -> str | None
find_opportunity_ids_by_keys(*, normalized_address=None, apn=None, listing_url=None, broker_property_ref=None) -> dict[str, str]  # key name -> opportunity_id
find_opportunity_id_by_attachment_sha(sha256) -> str | None
```
`append_event` assigns `seq = max(seq)+1` per opportunity inside a transaction and returns the stored event.
`create_opportunity` assigns `deal_number = max+1` (start at 101 so the demo reads "Deal #101").
`watchlist()`: all non-DEAD opportunities ordered REVIEW, NEAR, WATCH, then by `distance_pct` ascending (None last).
`dashboard_stats(policy_version)`: `encountered` = count(opportunities); per-status counts; 7-day counters from
events: `conditions_changed_7d` counts ASKING_PRICE_CHANGED/NOI_CHANGED/RENT_ROLL_UPDATED/FINANCING_CHANGED,
`threshold_crossings_7d` counts STATUS_CHANGED events whose `payload["to"] == "REVIEW"`, `human_interruptions_7d`
counts HUMAN_NOTIFIED.
`opportunity_detail(id)` accepts an opportunity_id or a deal number string.

### identity/resolver.py
See the stub docstring for normalization and confidence tiers. `extract_identity_keys(message, claims)` pulls
address parts from claims, `thread_id`/`in_reply_to` and sender from the message, attachment sha256s, listing
URLs from `message.urls` and `claims.listing_url`. `resolve(keys, repo)` checks, in order: message thread
(via `find_opportunity_id_by_thread_id` on `keys.thread_id`, and `find_opportunity_id_by_message_id` on the
in-reply-to id), exact keys, attachment sha, then fuzzy address among all properties (rapidfuzz). Returns the
best candidate with confidence; `needs_human=True` when the best candidate is in [0.5, 0.8).
Tests: same address spelled differently resolves; different street numbers do not; reply-thread resolves with no
address in the body.

### ingestion/email.py, ingestion/text.py
Per the stub docstrings. `thread_id` = first id in `References`, else `In-Reply-To`, else own `Message-ID`.
`Attachment.text` for `text/*` and `.md/.txt/.csv` decoded, for `application/pdf` via `pypdf`.
`from_text` builds a message with `message_id = "txt_" + sha256(text)[:16]` unless given.

### evidence/reconcile.py
Per the stub docstring. Required for a first reconciliation: `asking_price` and `gross_scheduled_income`
(derive GPR from `tenants[].annual_rent` sum when present; else from `stated_noi + stated_expenses.total` when
both present; else raise `MissingInputs`). Derive `largest_tenant_pct` and `tenant_count` from `tenants` when
not stated. Changes: price differs -> `ASKING_PRICE_CHANGED` payload `{"from": .., "to": .., "pct": ..}`,
summary `"Asking price $1,550,000 -> $1,250,000 (-19.4%)"`.

---

## W3: Strands agents, model providers, pipeline

### models/cli_model.py: `CLIModel(Model)`
A Strands custom model provider that runs a local coding-agent CLI as the LLM, so development uses existing
subscriptions and no API keys. Verified working command lines (each returns JSON conforming to a schema):
```
claude:  echo "$PROMPT" | claude -p --model $MODEL --no-session-persistence --output-format json \
           --json-schema "$SCHEMA_JSON" --tools ""            # stdout JSON: .structured_output ; run with CLAUDECODE unset
codex:   codex exec --skip-git-repo-check -s read-only -C $TMPDIR --output-schema schema.json -o out.json "$PROMPT" < /dev/null
                                                              # out.json is the JSON object
agy:     agy --output-format json --json-schema schema.json --model $MODEL --dangerously-skip-permissions -p "$PROMPT" < /dev/null
                                                              # stdout JSON: .structured_output
```
Defaults: `DEALSIEVE_CLI_PROVIDER=claude`, `DEALSIEVE_CLI_MODEL=sonnet` (agy: `gemini-3.8-flash-low` or
`claude-sonnet-4-6`; codex: config default). Timeout 240 s. Always pass the prompt on stdin or as an argument
with stdin redirected from /dev/null, or the CLI blocks.

`stream()` renders one prompt from `system_prompt`, `tool_specs` (name, description, inputSchema.json) and the
`messages` transcript (text, toolUse, toolResult blocks rendered readably), asks for a response in this schema:
```
{"type":"object","properties":{
  "tool_calls":{"type":"array","items":{"type":"object","properties":{
      "name":{"type":"string"},"input_json":{"type":"string","description":"JSON object encoded as a string"}},
      "required":["name","input_json"],"additionalProperties":false}},
  "final_text":{"type":"string"}},
 "required":["tool_calls","final_text"],"additionalProperties":false}
```
then yields Strands events: `messageStart`; per tool call `contentBlockStart{toolUse{name,toolUseId}}`,
`contentBlockDelta{toolUse{input: <json string>}}`, `contentBlockStop`; text block if `final_text` non-empty;
`messageStop{stopReason: "tool_use" | "end_turn"}`; `metadata{usage,metrics}`. Honour `tool_choice` ("any" or a
named tool means at least one / that tool call is mandatory; say so in the prompt). If `input_json` fails to parse
or validate against the tool's schema, retry once with the error appended; then surface an `end_turn` text
explaining the failure. Implement `structured_output()` by calling the CLI with `output_model.model_json_schema()`
directly and yielding `{"output": output_model.model_validate(obj)}` last (mirror `strands/models/openai.py`).
Unit-test rendering and parsing with an injected fake runner; mark real-CLI tests `@pytest.mark.live`.

### models/scripted.py: `ScriptedModel(Model)`
Deterministic replay for tests and the offline demo. Loads `fixtures/scripted/<name>.json`:
```
{"turns": [
   {"tool_calls": [{"name": "record_claims", "input_ref": "fixtures/expected/claims_01_initial_offer.json"}]},
   {"tool_calls": [{"name": "underwrite", "input": {}}]},
   {"final_text": "Recorded, underwrote: WATCH. No human attention required."}],
 "structured_outputs": {"SkepticOutput": {...json matching the skeptic model...}}}
```
Each `stream()` call consumes the next turn. When `tool_specs` contains a structured-output tool (Strands adds one
named after the Pydantic model) or `tool_choice` names a tool that is not in the turn, respond with that tool call
using `structured_outputs[<model name>]`. `input_ref` paths are relative to the repo root.

### models/backend.py
`get_model(purpose, script=None)` by `DEALSIEVE_MODEL_BACKEND`: `cli` -> CLIModel; `anthropic` -> `AnthropicModel`
(`DEALSIEVE_ANTHROPIC_MODEL`, default `claude-sonnet-5`); `bedrock` -> `BedrockModel` (`DEALSIEVE_BEDROCK_MODEL`,
default `global.anthropic.claude-sonnet-4-6`, region from `AWS_REGION`); `openai` -> `OpenAIModel`;
`scripted` -> `ScriptedModel(script)`. `backend_name()` returns e.g. `cli:claude:sonnet`.

### agents/tools.py: `ProcessingSession` + tools
`ProcessingSession` (dataclass): `repo, policy, notifier, message, model_backend`, mutable state
`opportunity_id, created, status_before, run_before, run_after, threshold_crossed, skeptic_report, notification,
draft, events_created: list[str], claims_recorded: bool, errors: list[str]`.
`make_tools(session) -> list` returns these `@tool` functions (closures over the session). Tools do all
deterministic work and enforce every gate in code; the model cannot bypass them:

- `record_claims(claims: ExtractedClaims) -> dict` (if Strands cannot schema a Pydantic parameter, accept
  `claims_json: str` and validate): store message; store evidence; `keys = extract_identity_keys`; `resolve`;
  create `Property` + `Opportunity` when no confident match (display_name from address, else subject); when
  `needs_human`, still create, plus a `NOTE` event "possible duplicate of #N (confidence x)". Events, in order:
  `MESSAGE_RECEIVED`, `DOCUMENT_ADDED` per attachment, `DEAL_DISCOVERED` if created, `CLAIMS_EXTRACTED`, then one
  event per `DetectedChange` from `reconcile`. Update opportunity (`working_values`, `current_asking_price`,
  broker fields) and `link_message_to_opportunity`. Return `{opportunity_id, deal_number, created, status,
  changes: [summaries], conflicts, missing_fields}`. On `MissingInputs`, return `{error, missing}` and record a NOTE.
- `underwrite() -> dict`: `run_underwriting(opp.working_values, policy, opportunity_id=..., trigger_event_id=last)`;
  store run; `record_policy_version`; events `UNDERWRITING_COMPLETED` (payload: run_id, status, normalized_cap,
  dscr, max_viable_price, failure_summary) and `STATUS_CHANGED` (payload `{"from","to"}`) when it changed; update
  opportunity (`status`, `previous_status`, `latest_run_id`, `viability`, `reason_summary`,
  `human_attention_required`). `session.threshold_crossed = new == REVIEW and previous != REVIEW`. Return a
  compact summary including `threshold_crossed` and `next_step` text.
- `request_skeptic_review() -> dict`: only when status is REVIEW, else return `{"skipped": reason}`. Runs
  `agents/skeptic.py`, stores report, event `SKEPTIC_REVIEW_COMPLETED`, returns verdict, concerns, suggested questions.
- `draft_broker_questions(questions: list[str]) -> dict`: only when a skeptic report exists for the current run.
  Creates a pending `OutboundDraft` to `opp.broker_email`, subject `Re: <original subject>`, event
  `BROKER_DRAFT_CREATED`. Nothing is sent.
- `notify_human(note: str) -> dict`: only when `session.threshold_crossed` and not already notified. Builds the
  alert with `format_threshold_alert(opp, run_before, run_after, skeptic)`, `notifier.send`, stores notification
  with `delivered`/`delivery_ref`, event `HUMAN_NOTIFIED`. Otherwise `{"skipped": "no threshold crossing"}`.

### agents/acquisition.py: `build_acquisition_agent(session) -> Agent`
`Agent(model=get_model(ACQUISITION, script=...), tools=make_tools(session), system_prompt=..., callback_handler=None)`.
System prompt: role (acquisition analyst for a specific small-bay industrial buyer), the rules (never estimate
missing numbers, cite provenance, `is_price_change` for reply emails that only restate price, leave unknown fields
null), and the fixed procedure: `record_claims` -> `underwrite` -> if `threshold_crossed`: `request_skeptic_review`
-> `draft_broker_questions` (use the skeptic's questions) -> `notify_human` -> one-line summary. Otherwise stop
with a one-line summary. The user prompt is the rendered message: headers, body, each attachment's text
(cap 25k chars each).

### agents/skeptic.py: `run_skeptic(session) -> SkepticReport`
Second Strands `Agent` with `get_model(SKEPTIC)`, no tools, invoked with `structured_output_model=SkepticOutput`
(Pydantic: verdict, summary, concerns list matching `SkepticConcern`). Prompt: policy summary, latest run
(gates, broker vs normalized rows, stress), evidence list, `missing_fields`, `conflicts`, OM text excerpt.
Mission: find reasons this attractive-looking deal should still be rejected; flag claims without evidence
(roof age, Phase I, CAM reconciliation, lease rollover, seller-related tenants). Never recompute finance.

### pipeline.py: `process_inbound`
Per the stub docstring, including the SYSTEM-actor safety net. Runs `agent(prompt)` synchronously. Catch all
exceptions from the model layer: record a `NOTE` event when an opportunity exists, put the error in `summary`,
and still apply the safety net where possible.

### Tests and integration
`tests/agents/`: CLIModel render/parse with fake runner; ScriptedModel; tool gating (notify refused without a
crossing; draft refused without a skeptic report) using a real `Repo` on a tmp path once W2 lands (write them
now against the interface). Write `fixtures/scripted/01_initial_offer.json`, `02_price_drop.json`,
`03_structural_single_tenant.json` so `tests/e2e/test_watch_to_review.py` passes at integration. One
`@pytest.mark.live` test runs fixture 01 through the real `cli` backend and asserts WATCH.

---

## W4: API, notifications, channels, scripts, AgentCore

### notifications/
`console.py` `ConsoleNotifier`: prints the alert in a box to stdout (this is what the local demo shows).
`telegram.py` `TelegramNotifier`: `httpx` POST `https://api.telegram.org/bot<token>/sendMessage` with
`chat_id`, `text`, and an inline keyboard from `actions` (callback_data `"<action>:<opportunity_id>"`);
returns the message id. `get_notifier()`: `DEALSIEVE_NOTIFIER=telegram` and token+chat id present -> Telegram,
else console. `format.py` `format_threshold_alert`: plain text, exactly this shape (values from the runs):
```
DEAL #101 JUST BECAME INVESTABLE
8-unit small-bay industrial, Power Inn Rd, Sacramento

Price            $1,550,000 -> $1,250,000
Normalized cap   6.42% -> 8.27%   PASS
DSCR             1.05x -> 1.43x   PASS
Largest tenant   19%              PASS

Previously failed solely on valuation.

Still unresolved:
- roof age
- Phase I environmental
- CAM reconciliation

[Review] [Draft broker questions] [Ignore]
```
"Previously failed solely on valuation" only when the previous run had no structural failures. "Still unresolved"
lists skeptic concerns with `evidence_status` in {missing, weak}; omit the section when there is no report.
`kind="threshold_crossed"`, actions review / draft_questions / ignore.

### api/app.py (FastAPI)
JSON bodies are `model_dump_json()` of the contracts (numbers, ISO datetimes, enum strings). Dependency-inject
one `Repo`, the loaded policy and the notifier (created at startup from env).
```
GET  /api/health                      -> {"status":"ok","backend":..,"policy_version":..}
GET  /api/stats                       -> DashboardStats
GET  /api/policy                      -> policy as JSON (Decimals as numbers)
GET  /api/opportunities?include_dead= -> list[WatchlistItem]   (default excludes DEAD)
GET  /api/opportunities/{id}          -> OpportunityDetail     (opportunity_id or deal number)
GET  /api/drafts?status=              -> list[OutboundDraft]
POST /api/drafts/{id}/approve         -> OutboundDraft (status approved, HUMAN_APPROVED_DRAFT event; no sending)
POST /api/drafts/{id}/reject          -> OutboundDraft
GET  /api/notifications               -> list[Notification]
POST /api/ingest/email  (multipart `file`, or raw body with Content-Type message/rfc822) -> ProcessingOutcome
POST /api/ingest/text   {"text": .., "sender": .., "channel": "telegram"|"manual"}     -> ProcessingOutcome
GET  /  and any non-/api path         -> frontend/dist/index.html (SPA fallback); static under /assets
```
CORS open for localhost dev. `tests/api/` with FastAPI TestClient against a tmp Repo; ingestion tests can use
`DEALSIEVE_MODEL_BACKEND=scripted` once W3 lands (write them now; mark `@pytest.mark.skip` until then if needed).

### ingestion/telegram.py + cli.py + scripts/
`dealsieve` CLI (`dealsieve/cli.py`, argparse): `ingest <file.eml|.txt>`, `seed`, `reset`, `serve [--port]`,
`telegram-bot`, `status [deal#]`. `telegram-bot` long-polls `getUpdates`: text -> `from_text(channel=TELEGRAM)`,
documents -> download and attach (pdf text via pypdf), `/status`, `/deal <n>`, and `callback_query` for
approve/reject drafts and ignore. `scripts/inject_email.py <eml>` = `dealsieve ingest` and prints a readable
outcome (status before/after, cap, DSCR, max viable, whether a human was notified). `scripts/seed_demo.py`: reset
DB, run fixtures 03 and 04 with the scripted backend, then create ~10 additional synthetic opportunities directly
via `Repo` + `run_underwriting` (varied Sacramento-area addresses, mostly DEAD/WATCH, one NEAR) so the dashboard
has texture; never seed a REVIEW (the demo creates the only one). `scripts/reset_db.py`.

### agentcore_app.py
`BedrockAgentCoreApp` entrypoint (`bedrock_agentcore.runtime`). Payloads: `{"type":"email","eml_base64":..}`,
`{"type":"text","text":..,"sender":..}`, `{"type":"status"}`, `{"type":"deal","id":..}`. Returns
`ProcessingOutcome`/detail JSON. Runs the same `process_inbound`. Local check: `python -m dealsieve.agentcore_app`
then `curl -X POST localhost:8080/invocations -d '{"type":"status"}'`.

---

## W5: dashboard (`frontend/`)

Vite + React + TypeScript + Tailwind. No component library. Build to `frontend/dist` (served by the API). Dev
proxy `/api` -> `http://localhost:8000`. `VITE_MOCK=1` serves fixture JSON from `frontend/src/mock/` shaped
exactly like the API responses above (derive shapes from `dealsieve/schemas/core.py`).

Pages:
1. `/` **Overview**: stat strip (encountered, rejected, watching, near threshold, review, human interruptions
   this week), then the **watchlist** sorted by distance to viability: deal #, name, status pill, asking, max
   viable, distance (small bar), binding constraints, updated. Row click -> detail.
2. `/deals/:id` **Deal detail**: header (deal #, name, address, status pill, one-line reason, asking price,
   "Human attention: none | required"); **Broker vs DealSieve** table (`comparison` rows with `note`); **Viability
   frontier** card (current price, max viable price, distance, binding constraints, "viable below $X");
   **Gates** (pass/fail, threshold vs actual, structural vs economic); **Timeline** (events, newest last, with
   actor and source); **Underwriting history** (runs: date, price, normalized cap, DSCR, status); **Skeptic**
   concerns grouped by severity; **Drafts** (pending: show questions; Approve / Reject buttons POST to the API);
   **Evidence** table (field, value, source document, location, confidence).

Design: restrained and editorial, high information density, tabular-nums monospace for numbers, one accent
color, status colors DEAD slate / WATCH amber / NEAR orange / REVIEW green, no gradients or decorative
illustrations, fast. Empty and loading states for every panel. Must look finished on a 1440px-wide recording.

---

## Reporting

When done, report: files created, tests run and their result, anything you could not do, schema fields you need,
and any dependency you installed with `uv pip install` that must be added to `pyproject.toml`.
