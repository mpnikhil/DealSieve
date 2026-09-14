# DealSieve — Devpost submission text

## Tagline

The acquisitions agent for small-bay industrial buyers. It works the pipeline; you make the decisions.

## Elevator pitch (200 characters, for the Devpost field)

The acquisitions agent for small-bay industrial buyers. It underwrites every broker package on your numbers, keeps a price on every pass, and works the deals that clear. You keep the money decisions.

## Elevator pitch (98 words, for the description opener)

DealSieve is the acquisitions agent for small-bay light industrial buyers. It underwrites every broker offering memorandum on the buyer's own numbers, with the Prop 13 tax reset (California reassesses property tax to the sale price), real vacancy and reserves. It stores the clearing price on every pass and re-underwrites the day the price or the facts change. It runs the diligence chase for roof condition, Phase I (the environmental site assessment) and CAM reconciliation (the yearly true-up of shared building expenses billed to tenants), reads the inspection report and re-prices the basis. The principal keeps the money decisions.

## Inspiration

We receive dozens of offering memorandums every month for light industrial properties: small commercial buildings rented to electricians, HVAC contractors and cabinet shops. Almost every off-market email pitches an aggressive pro forma. The broker assumes zero vacancy and applies the seller's old tax bill. We spend hours tearing down those numbers to find the actual cap rate (the annual return on the purchase price). Our process requires adding a management fee alongside a realistic capex reserve. We then model the Prop 13 tax reset just to see if the property clears our debt coverage floor. Most deals get passed on within the hour, and a pass is only "not at this price"; nobody has the hours to re-run fifty old passes when a seller cuts a price.

When a building finally clears our underwriting, the diligence phase begins. Chasing down the property condition report to check for ponding on the flat built-up roof takes days. Then we need the Phase I assessment to identify environmental risk from automotive tenants. Drafting emails to verify CAM reconciliations and review lease rollover schedules consumes the rest of the week. We built DealSieve to do the triage, hold a price on every pass, and manage the follow-up questions. Our team now spends its time pricing the basis.

## What it does

DealSieve ingests broker emails (with offering-memorandum attachments), Telegram messages, and pasted URLs. A Strands Acquisition Agent extracts claims with per-fact provenance. It hands off to deterministic tools that resolve the property to one canonical opportunity, reconcile the claims into working values, and run a `Decimal`-precision underwriting engine under an immutable investment policy (property-tax reset, vacancy floor, management fee, capex reserve, then six hard gates: tenant count, tenant concentration, absolute price cap, LTV, normalized cap rate, DSCR). The engine classifies the result DEAD, WATCH, NEAR, or REVIEW. For anything short of REVIEW, it solves a bisection to find the viability frontier: the maximum price at which every economic gate would pass, noting which gate is binding.

On the demo property (8330 Power Inn Road, Sacramento, an 8-unit small-bay industrial site of 20,000 sf), the initial broker email at $1,550,000 (broker-stated 8.13% cap) normalizes to a 6.42% cap and 1.02x DSCR (debt service coverage, income divided by loan payments). The status is WATCH, viable below $1,285,946, and no human is notified. Later the broker thread announces a price drop to $1,250,000. DealSieve recognizes the opportunity and re-underwrites automatically yielding an 8.27% cap, 1.43x DSCR, and 68% LTV. The status crosses WATCH → REVIEW. An independent Skeptic Agent (a second Strands agent with no tools and structured output only) is invoked because the deal looks clean on paper. It flags unsupported items in the record such as roof age, a Phase I environmental report, and CAM reconciliation. It drafts an information request and prompts a human on Telegram for approval.

Once approved, the request is delivered to the broker. The broker replies with a Property Condition Report containing photographs. A third Strands agent, the Inspector Agent, reads the text and visually inspects embedded photos showing ponding water and blistering on the 2001 built-up membrane. It answers the open roof question and extracts $90,000 of immediate roof capex. DealSieve automatically re-underwrites on the all-in basis. The purchase price plus capex pushes the total basis to $1,340,000, the cap rate drops to 7.71%, the DSCR to 1.30x, and LTV to 75.2%. The deal moves REVIEW → NEAR (viable below $1,208,108). A second human alert fires with a drafted $42,000 price-credit request. Unanswered requests are followed up on a policy cadence. If the broker goes dark, the loop stalls and alerts the human.

DealSieve also remembers the buyer's own decisions. Every pass, price and approval is written to a decision memory (Amazon Bedrock AgentCore Memory, with a local store as the fallback), and the next alert on the same building quotes it back: "You previously: passed at $1.29M."

The frozen policy also takes the emotion out of the decision. A building the buyer has fallen for has to clear the same numbers as every other package, under a policy version the engine records with every run, so the system never talks anyone into a bad buy.

## How we built it

Workstreams against a shared contracts document (`docs/CONTRACTS.md`), so the finance engine, persistence layer, Strands agent layer, diligence loop, API/notifications, and dashboard could be built concurrently against fixed Pydantic schemas (`dealsieve/schemas/core.py`) without stepping on each other:

- **Finance** (`dealsieve/underwriting/`): pure, tested functions on `Decimal` — all-in basis underwriting (`total_acquisition_cost = price + closing_costs + immediate_capex`), normalization, financing, stress testing, gates, and bisection viability solver.
- **Persistence** (`dealsieve/persistence/`): SQLite in WAL mode; events, underwriting runs, and document analyses are immutable; concurrency-safe deal/event sequence allocation with `BEGIN IMMEDIATE` (R10); message claiming (R4); deduplication constraints.
- **Agents** (`dealsieve/agents/`, `dealsieve/models/`): three specialized Strands agents — (1) Acquisition Agent with deterministic code-gated tools, (2) Skeptic Agent for second-opinion risk audit, and (3) Inspector Agent with multimodal vision analyzing inspection PDFs and photos. A custom `CLIModel` Strands provider supports local CLIs (`claude`, `codex`, `agy`), and `ScriptedModel` replays recorded turns for deterministic offline demos and tests.
- **Diligence & Delivery** (`dealsieve/diligence/`, `dealsieve/outbound/`): deterministic screen blocking unauthorized offers or money language, follow-up cadence advancement, keyword-family answer matching, stall detection, and RFC 822 `.eml` delivery backends.
- **Memory** (`dealsieve/memory/`): decision memory behind one interface, Amazon Bedrock AgentCore Memory in AWS and a local store offline, read by the Skeptic and quoted in every alert.
- **API/notifications** (`dealsieve/api/`, `dealsieve/notifications/`): FastAPI surface, console and Telegram notifiers with inline-keyboard approval (the alert carries the actual draft email), and the `dealsieve/agentcore_app.py` AWS Bedrock AgentCore entrypoint.
- **Dashboard** (`frontend/`): Vite + React + TypeScript + Tailwind — overview watchlist sorted by distance to viability, and a deal-detail view with the broker-vs-DealSieve comparison table, viability frontier, diligence tracker, unified correspondence thread, document analysis with photo lightbox, and pending drafts.

## Challenges

Keeping the model out of the arithmetic while letting it drive the workflow was a primary design challenge. LLMs often return plausible but incorrect calculations for metrics like cap rates. We addressed this by making every deterministic step a tool call with preconditions enforced in code. For example, `notify_human` returns `{"skipped": "no threshold crossing"}` unless the preceding underwriting run changed the status, and `request_price_adjustment` calculates the credit needed from the viability frontier. A pipeline-level safety net performs any step the model skips. A second challenge was ensuring reliability against network flakiness during recording. The `ScriptedModel` replays recorded turns with zero network calls, allowing the story to reproduce offline. The same code path runs against a live CLI or Bedrock by changing an environment variable.

## Accomplishments

- A viability solver that computes a specific, monitorable target price for rejected deals.
- A tool-gated multi-agent system where the properties that make a hackathon demo scary — the model hallucinating a pass, doing unchecked arithmetic, or double-notifying a human — are structurally impossible.
- An end-to-end 3-act story (WATCH -> REVIEW -> Diligence & Condition Report -> Capex adjustment & price-credit negotiation) fully tested (`tests/e2e/test_watch_to_review.py` and `tests/e2e/test_diligence_loop.py`) and reproducible offline in 3 seconds.
- Hard policy and guardrails in code, not in prompts: 493 tests cover gate boundaries, the amortization schedule, the property-tax reset, bisection convergence on the viability frontier, stress scenarios, identity resolution, evidence-conflict preservation, multimodal document inspection, and the diligence follow-up loop.

## What we learned

Deterministic-first agent design improves testability. Because the arithmetic and workflow gates are plain Python functions, the underwriting engine and tool-gating logic are unit-testable without mocking a model. The model layer is swappable to a scripted replay for CI and offline demos. We found that a custom Strands `Model` provider is a small surface area, handling request rendering and JSON response parsing. Building one against local CLI tools increased iteration speed by removing the need for API keys early in development.

## What's next

Next steps include monitoring broker emails at mailbox scale, pulling listings from crawl and API sources, and building an off-market owner universe from assessor data. We plan to add loan-rate and seller-financing monitoring as additional axes on the viability-frontier machinery. Market-rent and transaction comps will help check normalized NOI against external data. The core primitive of persistent opportunity state, deterministic policy, and monitored thresholds could also be applied outside commercial real estate to small-business, franchise, private-credit, and equipment acquisitions.

**On AgentCore**: the same application pipeline is wrapped by `dealsieve/agentcore_app.py` and successfully deployed to Amazon Bedrock AgentCore Runtime in `us-west-2`. The deployed runtime passed a status invocation, recorded in the repository's immutable deployment ledger. The full Strands `BedrockModel` inference path is implemented; the selected model currently has zero on-demand token throughput on this new AWS account, so cloud model inference awaits account quota availability. That limit does not affect the successful AgentCore deployment or the fully reproducible scripted test and demo path.

## Built with

Python 3.12 · Strands Agents SDK · Pydantic · SQLite · FastAPI · Vite · React · TypeScript · Tailwind CSS · `claude`/`codex`/`agy` CLIs (local model backend) · Amazon Bedrock AgentCore Runtime (deployed) · Amazon Bedrock AgentCore Memory · Amazon Bedrock model backend · Telegram Bot API
