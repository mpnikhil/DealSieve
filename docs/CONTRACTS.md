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

---
---

# Phase 2: the autonomous diligence loop (added 2026-09-12 evening)

Read `docs/DEALSIEVE_PLAN.md` section 36 for the product story and the autonomy boundary. The executable spec is
`tests/e2e/test_diligence_loop.py` plus the updated tail of `tests/e2e/test_watch_to_review.py`. Both fail today;
they define done. Everything in Phase 1 above still applies (ownership rules, no commits, Decimal, tests).

## What changed in the shared contracts (already done by the orchestrator)

- `schemas/core.py`: `DiligenceRequest`, `DocumentAnalysis` (+ `DocumentFinding`, `RequestAnswer`), `CapexItem`,
  `OutboundKind`; `OutboundDraft` gained `kind`, `requires_approval`, `request_ids`, `in_reply_to_message_id`,
  `delivery_ref`; `WorkingValues` gained `immediate_capex` and `capex_items` and now requires `asking_price > 0`;
  `FinancingResult` gained `immediate_capex` and `all_in_basis`; `Attachment.image_paths`;
  `Notification.dedupe_key` and new kinds `fell_below_threshold`, `diligence_stalled`; new `EventType`s
  (`DILIGENCE_*`, `DOCUMENT_ANALYZED`, `CAPEX_ADJUSTED`, `OUTBOUND_BLOCKED`); `ModelPurpose.DOCUMENT`;
  `OpportunityDetail` gained `diligence_requests`, `document_analyses`, `inbound_messages`;
  `DashboardStats.open_diligence_requests`.
- `config/investment_policy.yaml` + `policy/loader.py`: `outreach` (auto_send_information_requests,
  follow_up_after_days=3, max_follow_ups=2, always_require_approval, from_name/from_email/signature) and `capex`
  (count_as_immediate=[immediate, near_term], use_midpoint). `fixtures/policies/no_autosend_policy.yaml` is the
  same policy with auto-send off.
- `pyproject.toml`: `fpdf2`, `pillow` added.

## Ownership

| WS | Owner | Owns (may edit) | Tests |
|---|---|---|---|
| W9 Finance | Codex | `dealsieve/underwriting/**` | `tests/underwriting/**` |
| W10 Persistence, diligence engine, outbound, identity/reconcile fixes, API, notifications formats | Sonnet | `dealsieve/persistence/**`, `dealsieve/diligence/**` (new), `dealsieve/outbound/**` (new), `dealsieve/identity/**`, `dealsieve/evidence/**`, `dealsieve/ingestion/email.py`, `dealsieve/api/**`, `dealsieve/notifications/**`, `dealsieve/cli.py` | `tests/persistence/**`, `tests/identity/**`, `tests/diligence/**` (new), `tests/api/**` |
| W11 Fixtures | Sonnet (web access) | `fixtures/photos/**`, `fixtures/om/05_*`, `fixtures/emails/05_*`, `fixtures/expected/*05*`, `fixtures/ATTRIBUTION.md`, `scripts/build_fixture_pdfs.py` | verifies by loading with the schemas |
| W12 Agents | Opus | `dealsieve/agents/**`, `dealsieve/models/**`, `dealsieve/pipeline.py`, `fixtures/scripted/**` | `tests/agents/**` |
| W13 Dashboard | agy | `frontend/**` | build |

## Codex adversarial review findings, assigned

Confirmed defects from the review of Phase 1 (`/codex:result review-mtyxed74-f9wc6a`), each must be fixed with a regression test:

| # | Finding | Owner |
|---|---|---|
| R1 | Rates are quantized to 6 dp BEFORE gate evaluation (`normalize.py`, `financing.py`); raw cap 0.0799996 passes the 8% gate. Evaluate gates and run the bisection on full-precision Decimals; quantize only the emitted fields. Test just below each threshold by less than half the display quantum. | W9 |
| R2 | Nonpositive price/NOI: DSCR sentinel 999 and zero LTV can produce a false REVIEW; price 0 divides by zero. Fail closed: `run_underwriting` raises `InvalidInputs` on price <= 0; DSCR sentinel only when loan == 0 AND NOI > 0; NOI <= 0 fails cap and DSCR gates. | W9 |
| R3 | `notify_human` delivers before persisting; a storage failure after delivery makes the safety net send again. Persist the `Notification` with `dedupe_key = f"{opportunity_id}:{run_id}:{kind}"` and `delivered=False` FIRST (repo enforces UNIQUE on dedupe_key and raises `DuplicateNotification`), then deliver, then mark delivered. A duplicate key means "already handled": skip. | W12 (+ W10 for the constraint) |
| R4 | Message existence is treated as completion. Add processing state to `inbound_messages` (`status`: received, processing, completed, failed; `error`). `repo.claim_message(message)` atomically inserts or re-claims a failed/abandoned row and returns False only for completed ones. Pipeline returns the duplicate outcome only for completed messages and marks completed/failed at the end. | W10 (repo) + W12 (pipeline) |
| R5 | Calling `underwrite` twice clears `threshold_crossed`. Enforce a tool phase machine per message in `ProcessingSession`: `record_claims` once -> `analyze_document` any number of times -> `underwrite` once (a second call returns the first result, no new run) -> then, by outcome, `request_skeptic_review` once, `request_diligence` once, `request_price_adjustment` once -> `notify_human` once. Out-of-order or repeated calls return `{"skipped": reason}` and mutate nothing. Latches (`threshold_crossed`, `threshold_lost`) are set once and never cleared within a message. | W12 |
| R6 | The safety net is not exception-isolated. Wrap each safety-net action independently; record failures as NOTE events and in `ProcessingOutcome.summary`; a failing skeptic must not prevent the notification. `process_inbound` never raises. | W12 |
| R7 | Reconciled values can contradict the recorded winning evidence. Derive each reconciled field from the winning `Evidence.value` when evidence for that field exists (coerce to the field type); if the top-level claim disagrees with the winner, record a conflict and use the winner. | W10 |
| R8 | Thread match wins before contradiction checks; conflicting exact keys resolve by insertion order at confidence 1.0. Collect candidates from ALL strong identifiers first; if they disagree (thread says A, address/APN says B) return `needs_human=True`, `opportunity_id=None`, both candidates listed; thread-only matching is accepted only when no explicit identifier contradicts it. Fuzzy scores in [80, 90) become `needs_human=True` candidates instead of silent no-match. | W10 |
| R9 | Untrusted broker text reaches a permission-skipping coding-agent CLI. Run every CLI with a minimal environment allowlist (PATH, HOME, USER, LANG, TMPDIR, TERM, CODEX_HOME, CLAUDE_CONFIG_DIR, and provider auth vars), keep `claude --tools ""` and `codex -s read-only -C <empty tmpdir>`, and for agy use `--sandbox`; verify whether agy still answers in print mode without `--dangerously-skip-permissions` and drop the flag if it does (report the result). Document the trust boundary in the module docstring: production input should use `bedrock`/`anthropic`, which have native role separation. | W12 |
| R10 | deal_number / event seq allocation is only safe within one Repo instance. Allocate inside `BEGIN IMMEDIATE` transactions with bounded retry on `SQLITE_BUSY`; add a test with two Repo instances on the same file. | W10 |

## W9: finance on the all-in basis

- `all_in_basis = price + v.immediate_capex`. `normalized_cap_rate = NOI / all_in_basis` (unchanged when capex is 0).
- `compute_financing(noi, price, P, immediate_capex)`: `total_acquisition_cost = price + closing_costs + immediate_capex`;
  loan and equity as before from `total_acquisition_cost`; `ltv = loan / price`; populate `FinancingResult.immediate_capex`
  and `all_in_basis`.
- `broker_vs_dealsieve`: when capex > 0 add rows "Immediate capex" (broker "—", DealSieve "$90,000") and
  "All-in basis" (broker = price, DealSieve = all-in). Otherwise unchanged.
- Viability: bisection unchanged (capex is a constant; all price-dependent gates stay monotone). `failure_summary`
  for a NEAR/WATCH with capex: `"Fails on valuation after $90,000 immediate capex: cap 7.71% < 8.00%, DSCR 1.30x < 1.35x, LTV 75.2% > 75.0%"`.
- R1 and R2 above. Keep every existing test passing; extend `tests/underwriting/test_engine_fixtures.py` with the
  Act 3 case: fixture 01 working values at price $1,250,000 with `immediate_capex = 90000` must give status NEAR,
  cap in [7.6%, 7.8%], DSCR in [1.28, 1.32], LTV in [0.75, 0.76], `max_viable_price` in [$1,190,000, $1,225,000].

## W10: persistence, diligence engine, outbound, fixes, API

### persistence
New tables `diligence_requests(request_id PK, opportunity_id FK, status, topic, created_at, json)`,
`document_analyses(analysis_id PK, opportunity_id FK, message_id, filename, created_at, json)`; `inbound_messages`
gains `status`, `error`, `updated_at`; `notifications` gains `dedupe_key UNIQUE` (nullable). Methods:
```
claim_message(message: InboundMessage) -> bool      # R4; stores the message on first claim
mark_message_completed(message_id) / mark_message_failed(message_id, error)
list_inbound_messages(opportunity_id) -> list[InboundMessage]
store_diligence_request / update_diligence_request / get_diligence_request
list_diligence_requests(opportunity_id=None, status=None) -> list[DiligenceRequest]   # ordered by created_at
store_document_analysis / list_document_analyses(opportunity_id)
store_notification raises DuplicateNotification on a dedupe_key clash; update_notification(notification)
open_diligence_count() -> int   (status in sent, overdue) ; dashboard_stats fills open_diligence_requests
opportunity_detail includes diligence_requests, document_analyses, inbound_messages
```
R10 for sequences. R7, R8 in `evidence/reconcile.py` and `identity/resolver.py`.

### ingestion/email.py
Extract embedded images from PDF attachments with `pypdf` (`page.images`), keep those >= 200x200 px, convert to
PNG with Pillow, store under `data/documents/<sha256>/img_<n>.png` (index from 1, page order), and set
`Attachment.image_paths`. Image attachments (`image/*`) are stored the same way as a single image. The store root
is `DEALSIEVE_DOCS_DIR` (default `data/documents`).

### outbound/ (new package)
```
class OutboxError(RuntimeError)
class Outbox(Protocol):  name: str;  def send(self, message: OutboundDraft) -> str  # delivery_ref
class RecordingOutbox:   sent: list[OutboundDraft]; send appends and returns "recorded-N"
class FileOutbox:        writes RFC 822 .eml (From from policy.outreach, To, Subject, In-Reply-To/References,
                         Date, text/plain body) to DEALSIEVE_OUTBOX_DIR (default data/outbox)/<UTC ts>_<draft_id>.eml,
                         prints one line "-> sent to <to>: <subject>"; returns the path
class SmtpOutbox:        SMTP_HOST/PORT/USER/PASSWORD/STARTTLS env; optional
def get_outbox() -> Outbox   # DEALSIEVE_OUTBOX = file (default) | smtp | recording
```

### diligence/ (new package): the deterministic loop
```
def classify_outbound_text(text: str) -> OutboundKind
    # "offer" if it mentions LOI / letter of intent / purchase agreement / "our offer"; "credit_request" if it has a
    # $ amount, "credit", "reduce the price", "price reduction", "would the seller accept", "we would pay", "discount",
    # "concession", "terms"; else "information_request". Case-insensitive, word-boundary regexes. Money wins over info.
def build_requests(opportunity_id, items: list[dict], *, source: str | None) -> list[DiligenceRequest]
    # items: {topic, question, category?}; dedupe by normalized topic; status "draft"
def compose_information_request(opp, requests, policy, *, in_reply_to, original_subject) -> OutboundDraft
    # kind information_request, requires_approval = not policy.outreach.auto_send_information_requests,
    # subject "Re: <original subject>" (no double Re:), body: greeting by broker first name, one sentence of context,
    # numbered questions, policy signature. questions = [r.question ...], request_ids = [...]
def compose_follow_up(opp, requests, policy, *, follow_up_number, in_reply_to) -> OutboundDraft   # kind follow_up
def compose_credit_request(opp, amount: Decimal, rationale: str, policy, *, in_reply_to) -> OutboundDraft
    # kind credit_request, requires_approval True always
def dispatch(draft, *, repo, policy, outbox, actor=Actor.AGENT) -> OutboundDraft
    # 1. screen: kind_seen = classify_outbound_text(body + questions). If draft.kind in (information_request,
    #    follow_up) and kind_seen != information_request: store as pending with kind=kind_seen, requires_approval
    #    True, append OUTBOUND_BLOCKED event, return. 2. If requires_approval or kind in
    #    policy.outreach.always_require_approval: store pending, append BROKER_DRAFT_CREATED, return. 3. Else
    #    outbox.send -> status sent, sent_at, delivery_ref; store; append DILIGENCE_REQUEST_SENT /
    #    DILIGENCE_FOLLOW_UP_SENT / BROKER_MESSAGE_SENT; for carried requests set status sent, sent_at, due_at =
    #    sent_at + follow_up_after_days.
def approve_and_send(draft_id, *, repo, policy, outbox) -> OutboundDraft
    # human approval: status approved (HUMAN_APPROVED_DRAFT, actor HUMAN) then outbox.send -> sent (BROKER_MESSAGE_SENT)
def match_answers(requests, analysis) -> list[tuple[DiligenceRequest, RequestAnswer]]
    # deterministic: an answer matches a request when normalized topics share a keyword family
    # (roof; hvac/mechanical; phase i/environmental/esa; cam/reconciliation/opex; lease/rollover/estoppel;
    # rent roll; survey/title/easement; zoning; parking/paving) or token-set overlap >= 0.5
def apply_answers(matches, analysis, *, repo, evidence_ids_by_topic) -> list[DiligenceRequest]
    # resolves=True -> status answered, answered_at, answer_summary, answered_by_document, answer_evidence_ids;
    # resolves=False -> leave sent but append answer_summary "partial: ..."; append DILIGENCE_ANSWERED per request
def immediate_capex_from(items: list[CapexItem], policy) -> Decimal
    # sum of midpoint (or high if not use_midpoint) for items whose urgency is in policy.capex.count_as_immediate
@dataclass class FollowUpReport: as_of, follow_ups_sent, stalled, requests_followed_up: list[DiligenceRequest], notifications: list[str]
def run_follow_ups(*, repo, policy, outbox, notifier, as_of: datetime | None = None) -> FollowUpReport
    # For each opportunity with requests in status sent and due_at <= as_of:
    #   if follow_up_count < max_follow_ups: one follow-up message covering all overdue requests of that opportunity
    #     (compose_follow_up + dispatch); each request: follow_up_count += 1, last_follow_up_at, due_at = as_of + days
    #   else: status stalled for each, DILIGENCE_STALLED event, ONE notification kind diligence_stalled per
    #     opportunity (dedupe_key f"{opportunity_id}:diligence_stalled:{sorted request ids joined}"), via notifier
    # Idempotent: a second call with the same as_of changes nothing.
```

### notifications/format.py additions
`format_fell_below_alert(opp, previous_run, new_run, analysis: DocumentAnalysis | None, credit_draft: OutboundDraft | None) -> Notification`
kind `fell_below_threshold`, title `DEAL #N FELL BACK BELOW THRESHOLD`, body: what the document established (top
findings by severity, e.g. "Roof: original 2001 built-up membrane, ponding, replacement $85k-$95k"), the capex added,
before -> after lines for price basis / normalized cap / DSCR / LTV with PASS/FAIL, new status and frontier
("Viable below $1,208,108, 3.4% under the ask"), and if a credit draft exists: "Drafted for your approval: request a
$42,000 credit." Actions review / approve / reject.
`format_stalled_alert(opp, requests) -> Notification` kind `diligence_stalled`: "No reply on N requests after M
follow-ups: <topics>. The loop has stopped; your move." Actions review / ignore.

### api
```
GET  /api/diligence?status=                       -> list[DiligenceRequest]
POST /api/diligence/tick   {"as_of": iso | null}  -> FollowUpReport as JSON
GET  /api/correspondence/{opportunity_id}         -> {"inbound": [InboundMessage...], "outbound": [OutboundDraft...]}
GET  /api/documents/{analysis_id}/images/{index}  -> image bytes (index from 1) from DocumentAnalysis.image_paths
POST /api/drafts/{id}/approve                     -> now calls diligence.approve_and_send (it sends!)
```
`dealsieve followup [--as-of YYYY-MM-DD]` and `dealsieve outbox` (lists sent messages) in cli.py.
`tests/diligence/`: classify screen, compose (no double Re:, all questions present), dispatch gating (money blocked,
auto-send on/off), match_answers families, run_follow_ups cadence + stall + idempotence, FileOutbox writes valid
.eml; `tests/persistence/`: claim_message states, dedupe_key uniqueness, two-Repo sequence allocation (R10);
`tests/identity/`: R8 cases; reconcile: R7 case.

## W11: fixtures for Act 3

1. `fixtures/photos/roof_ponding.jpg`, `roof_membrane.jpg`, `rooftop_hvac.jpg`: real photographs (not renders) of a
   flat commercial roof with standing water, an aged/blistered single-ply or built-up membrane, and an aged rooftop
   packaged HVAC unit. Source from Unsplash (Unsplash License) or Wikimedia Commons (CC0 / CC BY / CC BY-SA), resize
   to <= 1600 px wide and <= 400 KB each, and record source URL, author, license per file in
   `fixtures/ATTRIBUTION.md`. Open each image and confirm it shows what the caption will claim.
2. `scripts/build_fixture_pdfs.py` (fpdf2 + Pillow) builds `fixtures/om/05_power_inn_property_condition_report.pdf`:
   cover ("Property Condition Assessment, 8330 Power Inn Road, Sacramento, CA 95826; site visit 2026-06-18; prepared
   for the owner"), executive summary, roof section with photo 1 caption "Photo 1: ponding water, NE corner, approx.
   1/2 inch after 48 dry hours" and photo 2 caption "Photo 2: membrane blistering and open seam near suite 105 HVAC
   curb", statement that the roof is the original built-up membrane installed 2001 with no documented replacement
   and a recommendation to replace within 12 to 24 months, budget $85,000 to $95,000; HVAC section with photo 3, a
   table of 8 packaged units (6 units 2014 to 2019; suites 103 and 106 are 1998 units "beyond typical service life,
   operational at inspection; budget replacement within 3 to 5 years, $14,000 to $18,000 each"); brief electrical,
   plumbing, paving sections (seal coat and restripe $6,000 to $8,000, 3 to 5 years); a findings table with cost
   ranges and timeframes. The report must NOT mention Phase I / environmental or CAM.
3. `fixtures/emails/05_inspection_report.eml`: From Maya Chen, Date 2026-09-15, Subject "Re: Off-market: ..." (same
   as 02), `In-Reply-To` = 02's Message-ID, `References` = 01 then 02, `Message-ID: <pca-2026-0915-power-inn@brokerage.example>`,
   body: "Attached is the property condition report the seller commissioned in June; roof and HVAC are on pages 3-6.
   Still waiting on the Phase I and the CAM reconciliation from the property manager." plus signature, with the PDF
   attached as `application/pdf`, filename `Power_Inn_Property_Condition_Report.pdf`, base64.
4. `fixtures/expected/claims_05_inspection_report.json`: `ExtractedClaims` with address fields only, `is_price_change`
   false, empty evidence, `notes` "Property condition report attached; no new economics."
5. `fixtures/expected/analysis_05_inspection_report.json`: the golden `DocumentAnalysis` body (all fields except
   analysis_id, opportunity_id, message_id, created_at, model_backend): document_type inspection_report; summary;
   findings incl. roof age (page 3, image_ref "image 1"), ponding (image 1), blistering (image 2), HVAC vintages
   (page 5, image 3), paving; `answers`: [{request_topic: "Roof age", answer: "...", resolves: true}]; `capex_items`:
   roof $85,000-$95,000 immediate, HVAC 2 units $28,000-$36,000 deferred, paving $6,000-$8,000 deferred; red_flags:
   ["Roof at end of service life; ponding indicates drainage deficiency"]; images_reviewed 3; image_paths [].
   Validate by `DocumentAnalysis.model_validate({...,"analysis_id":"x","opportunity_id":"o","message_id":"m"})`.
6. `fixtures/expected/05_inspection_report.json`: `{"status": "NEAR", "immediate_capex": 90000, "normalized_cap_rate":
   [0.076, 0.078], "dscr": [1.28, 1.32], "max_viable_price": [1190000, 1225000]}`.
7. Verify `parse_eml` on 05 yields a PDF attachment with text containing "2001" and 3 extracted images (after W10
   lands; if not yet, verify text only and say so).

## W12: agents

- `agents/inspector.py`: `run_inspector(session, attachment, open_requests) -> DocumentAnalysis` using a Strands
  `Agent(model=get_model(ModelPurpose.DOCUMENT), tools=[])` invoked with `structured_output_model=DocumentAnalysisOutput`
  (Pydantic mirror of DocumentAnalysis without ids). The user message has: the open requests (topic + question), the
  document text (<= 40k chars), and one Strands `image` content block per `attachment.image_paths` entry (format from
  extension, bytes), each preceded by a text block "image N". Instruction: cite `image_ref` as "image N" and `page`
  where possible; describe what is visible in each photo; never invent a number not in the text or visible.
- `models/cli_model.py` image support: for `codex` write the image bytes to the `-C` tmpdir and pass `-i <file>` per
  image; for `claude` and `agy` images are not passed (text-only) and the prompt says "images N..M were not available
  to you; do not describe them", unless `DEALSIEVE_CLI_ALLOW_READ=1` in which case `claude` gets `--tools Read` and the
  file paths. Bedrock/Anthropic providers pass images natively (no change). R9 hardening.
- `models/scripted.py`: honour `structured_outputs.DocumentAnalysisOutput`; map `image_ref "image N"` to
  `image_paths[N-1]` when building the DocumentAnalysis (do this in `run_inspector`, so both backends behave the same).
- `agents/tools.py`: new tools `analyze_document(filename: str)`, `request_diligence(items: list[DiligenceItem])`
  (DiligenceItem: topic, question, category), `request_price_adjustment(amount: Decimal | int, rationale: str)`;
  remove `draft_broker_questions`. Behaviour:
  - `analyze_document`: find the attachment (exact filename, else the only PDF); `run_inspector`; store analysis;
    evidence rows from findings (`field=f"doc:{topic}"`, `source_document=filename`, `location="page N" or
    "image N"`, confidence); `match_answers`/`apply_answers` against open requests; `immediate_capex_from` capex
    items -> if > 0 update `working_values.immediate_capex/capex_items` and append `CAPEX_ADJUSTED` (payload from/to);
    append `DOCUMENT_ANALYZED`. Return document_type, findings count, answered topics, still-open topics, capex total,
    and `next_step: "call underwrite"`.
  - `underwrite`: as before, plus `session.threshold_lost = previous == REVIEW and new != REVIEW`; the run must use
    the updated working values (capex). R5 idempotency.
  - `request_diligence`: only when status is REVIEW and once per message; `build_requests` from items (the agent
    passes the skeptic's missing-evidence concerns: topic, question_for_broker); `compose_information_request` with
    `in_reply_to=session.message.message_id` and the original subject; `dispatch`. Return request ids, whether it was
    sent or is awaiting approval, and the outbox ref.
  - `request_price_adjustment`: only when `session.threshold_lost` or the new status is NEAR/WATCH with a frontier,
    once per message. Code computes `suggested = ceil((current_price - max_viable_price) / 1000) * 1000`; if the
    model's amount differs from `suggested` by more than 25%, use `suggested` and say so in the returned dict.
    `compose_credit_request` + `dispatch` (always pending). Return draft id and amount used.
  - `notify_human`: reasons `threshold_crossed` (format_threshold_alert) and `fell_below_threshold`
    (format_fell_below_alert with the latest analysis and credit draft); R3 intent-before-delivery with dedupe_key.
- `agents/acquisition.py` system prompt: the procedure becomes: record_claims -> for each attached PDF/image:
  analyze_document -> underwrite -> if threshold_crossed: request_skeptic_review -> request_diligence(items = skeptic
  concerns with evidence_status "missing" that have a question_for_broker) -> notify_human; if threshold_lost:
  request_price_adjustment(amount = current price minus max viable price, rationale from the analysis) -> notify_human;
  otherwise stop. One-line final summary.
- `pipeline.py`: signature `process_inbound(message, *, repo, policy, notifier, outbox=None, script=None)`; `outbox or
  get_outbox()`; R4 claim/complete/fail; R6 isolation; safety net covers: underwrite after any analysis, skeptic +
  diligence on a crossing, credit draft on a loss with a frontier, notification on crossing or loss.
- `fixtures/scripted/02_price_drop.json`: record_claims -> underwrite -> request_skeptic_review -> request_diligence
  (3 items: Roof age; Phase I environmental; CAM reconciliation) -> notify_human -> final. Skeptic structured output
  keeps 4 concerns with the lease rollover one at evidence_status "weak" (so it is NOT chased; the rule is: chase only
  "missing"). `fixtures/scripted/05_inspection_report.json`: record_claims(claims_05) -> analyze_document
  ("Power_Inn_Property_Condition_Report.pdf") -> underwrite -> request_price_adjustment(42000, rationale) ->
  notify_human -> final; `structured_outputs.DocumentAnalysisOutput` = the golden analysis from W11 (load via
  `input_ref`-style reference `fixtures/expected/analysis_05_inspection_report.json`; extend ScriptedModel to accept
  `structured_output_refs`).
- Tests: phase machine (R5), isolation (R6), dedupe (R3), inspector prompt rendering with images (fake runner
  asserting `-i` files for codex), match/capex plumbing via fakes, and one `@pytest.mark.live` test that runs
  fixture 05 through `codex` with images and asserts `images_reviewed == 3` and a roof capex item.

## W13: dashboard additions

Deal detail: **Diligence** panel (table: topic, status pill draft/sent/overdue/answered/stalled, sent, due, follow-ups,
answer summary with a link to the answering document), **Correspondence** panel (inbound and outbound in one thread
ordered by time; each outbound shows kind badge and either "auto-sent under policy", "awaiting approval" with
Approve/Reject buttons, "approved by <human> and sent", or "blocked by policy screen"), **Documents** panel (per
`DocumentAnalysis`: type badge, summary, findings grouped by severity with page/image refs, capex items with urgency
and whether they count as immediate, red flags, thumbnails from `GET /api/documents/{analysis_id}/images/{n}` with a
lightbox), and the header's "Human attention" line shows the reason (entered review / fell below threshold / diligence
stalled) from the latest notification. Broker-vs-DealSieve table renders the new "Immediate capex" and "All-in basis"
rows when present. Overview: seventh stat tile "Awaiting broker" = `open_diligence_requests`. Mock data covers Act 3.

## Integration order

W9 and W11 are independent and fast; W10 next (everything else calls it); W12 builds against W10's interfaces; W13
against the API shapes. The orchestrator runs `tests/e2e` at the end and owns the fixes across boundaries.
