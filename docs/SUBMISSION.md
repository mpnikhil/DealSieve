# DealSieve — Devpost submission text

## Tagline

Rejects deals, remembers why, and interrupts you when the decision changes.

## Elevator pitch

DealSieve is a persistent acquisition agent for commercial real estate buyers. It reads incoming broker emails and underwrites deals against a frozen investment policy. When a deal is rejected, it computes the price at which the decision would change and monitors the thread for updates. If a subsequent email meets those conditions, DealSieve re-underwrites the property, runs an independent skeptic pass, and alerts a human. Most deals fail the policy and remain silent.

## Inspiration

Most AI tools in commercial real estate focus on reading documents or extracting rent rolls faster to increase the volume of deals a human can review. We focused instead on attention allocation and state persistence. A deal correctly rejected today is often forgotten, even if the price drops six weeks later to a viable level. We built an agent that holds a standing, evidence-backed opinion on every opportunity and requests attention when that opinion changes.

## What it does

DealSieve ingests broker emails (with offering-memorandum attachments), Telegram messages, and pasted URLs. A Strands Acquisition Agent extracts claims with per-fact provenance. It hands off to deterministic tools that resolve the property to one canonical opportunity, reconcile the claims into working values, and run a `Decimal`-precision underwriting engine under an immutable investment policy (property-tax reset, vacancy floor, management fee, capex reserve, then six hard gates: tenant count, tenant concentration, absolute price cap, LTV, normalized cap rate, DSCR). The engine classifies the result DEAD, WATCH, NEAR, or REVIEW. For anything short of REVIEW, it solves a bisection to find the viability frontier: the maximum price at which every economic gate would pass, noting which gate is binding.

On the demo property (8330 Power Inn Road, Sacramento, an 8-unit small-bay industrial site of 20,000 sf), the initial broker email at $1,550,000 (broker-stated 8.13% cap) normalizes to a 6.42% cap and 1.02x DSCR. The status is WATCH, viable below $1,285,946, and no human is notified. Later the broker thread announces a price drop to $1,250,000. DealSieve recognizes the opportunity and re-underwrites automatically yielding an 8.27% cap, 1.43x DSCR, and 68% LTV. The status crosses WATCH → REVIEW. An independent Skeptic Agent (a second Strands agent with no tools and structured output only) is invoked because the deal looks clean on paper. It flags unsupported items in the record such as roof age, a Phase I environmental report, and CAM reconciliation. It drafts an information request and prompts a human on Telegram for approval.

Once approved, the request is delivered to the broker. The broker replies with a Property Condition Report containing photographs. A third Strands agent, the Inspector Agent, reads the text and visually inspects embedded photos showing ponding water and blistering on the 2001 built-up membrane. It answers the open roof question and extracts $90,000 of immediate roof capex. DealSieve automatically re-underwrites on the all-in basis. The purchase price plus capex pushes the total basis to $1,340,000, the cap rate drops to 7.71%, the DSCR to 1.30x, and LTV to 75.2%. The deal moves REVIEW → NEAR (viable below $1,208,108). A second human alert fires with a drafted $42,000 price-credit request. Unanswered requests are followed up on a policy cadence. If the broker goes dark, the loop stalls and alerts the human.

## How we built it

Workstreams against a shared contracts document (`docs/CONTRACTS.md`), so the finance engine, persistence layer, Strands agent layer, diligence loop, API/notifications, and dashboard could be built concurrently against fixed Pydantic schemas (`dealsieve/schemas/core.py`) without stepping on each other:

- **Finance** (`dealsieve/underwriting/`): pure, tested functions on `Decimal` — all-in basis underwriting (`total_acquisition_cost = price + closing_costs + immediate_capex`), normalization, financing, stress testing, gates, and bisection viability solver.
- **Persistence** (`dealsieve/persistence/`): SQLite in WAL mode; events, underwriting runs, and document analyses are immutable; concurrency-safe deal/event sequence allocation with `BEGIN IMMEDIATE` (R10); message claiming (R4); deduplication constraints.
- **Agents** (`dealsieve/agents/`, `dealsieve/models/`): three specialized Strands agents — (1) Acquisition Agent with deterministic code-gated tools, (2) Skeptic Agent for second-opinion risk audit, and (3) Inspector Agent with multimodal vision analyzing inspection PDFs and photos. A custom `CLIModel` Strands provider supports local CLIs (`claude`, `codex`, `agy`), and `ScriptedModel` replays recorded turns for deterministic offline demos and tests.
- **Diligence & Delivery** (`dealsieve/diligence/`, `dealsieve/outbound/`): deterministic screen blocking unauthorized offers or money language, follow-up cadence advancement, keyword-family answer matching, stall detection, and RFC 822 `.eml` delivery backends.
- **API/notifications** (`dealsieve/api/`, `dealsieve/notifications/`): FastAPI surface, console and Telegram notifiers with inline-keyboard approval, and the `dealsieve/agentcore_app.py` AWS Bedrock AgentCore entrypoint.
- **Dashboard** (`frontend/`): Vite + React + TypeScript + Tailwind — overview watchlist sorted by distance to viability, and a deal-detail view with the broker-vs-DealSieve comparison table, viability frontier, diligence tracker, unified correspondence thread, document analysis with photo lightbox, and pending drafts.

## Challenges

Keeping the model out of the arithmetic while letting it drive the workflow was a primary design challenge. LLMs often return plausible but incorrect calculations for metrics like cap rates. We addressed this by making every deterministic step a tool call with preconditions enforced in code. For example, `notify_human` returns `{"skipped": "no threshold crossing"}` unless the preceding underwriting run changed the status, and `request_price_adjustment` calculates the credit needed from the viability frontier. A pipeline-level safety net performs any step the model skips. A second challenge was ensuring reliability against network flakiness during recording. The `ScriptedModel` replays recorded turns with zero network calls, allowing the story to reproduce offline. The same code path runs against a live CLI or Bedrock by changing an environment variable.

## Accomplishments

- A viability solver that computes a specific, monitorable target price for rejected deals.
- A tool-gated multi-agent system where the properties that make a hackathon demo scary — the model hallucinating a pass, doing unchecked arithmetic, or double-notifying a human — are structurally impossible.
- An end-to-end 3-act story (WATCH -> REVIEW -> Diligence & Condition Report -> Capex adjustment & price-credit negotiation) fully tested (`tests/e2e/test_watch_to_review.py` and `tests/e2e/test_diligence_loop.py`) and reproducible offline in 3 seconds.
- 304 passing tests covering gate boundaries, the amortization schedule, the property-tax reset, bisection convergence on the viability frontier, stress scenarios, identity resolution, evidence-conflict preservation, multimodal document inspection, and the diligence follow-up loop.

## What we learned

Deterministic-first agent design improves testability. Because the arithmetic and workflow gates are plain Python functions, the underwriting engine and tool-gating logic are unit-testable without mocking a model. The model layer is swappable to a scripted replay for CI and offline demos. We found that a custom Strands `Model` provider is a small surface area, handling request rendering and JSON response parsing. Building one against local CLI tools increased iteration speed by removing the need for API keys early in development.

## What's next

Next steps include monitoring broker emails at mailbox scale, pulling listings from crawl and API sources, and building an off-market owner universe from assessor data. We plan to add loan-rate and seller-financing monitoring as additional axes on the viability-frontier machinery. Market-rent and transaction comps will help check normalized NOI against external data. The core primitive of persistent opportunity state, deterministic policy, and monitored thresholds could also be applied outside commercial real estate to small-business, franchise, private-credit, and equipment acquisitions.

**On AgentCore**: the entrypoint (`dealsieve/agentcore_app.py`) is written and verified locally (`python -m dealsieve.agentcore_app` + `curl localhost:8080/invocations`), but has not been deployed to AWS — there were no AWS credentials available on the build machine during the hackathon. The deployment steps (`agentcore configure --entrypoint dealsieve/agentcore_app.py` && `agentcore launch` via the `bedrock-agentcore-starter-toolkit`) are documented in the README and are the next thing we'd run given credentials.

## Built with

Python 3.12 · Strands Agents SDK · Pydantic · SQLite · FastAPI · Vite · React · TypeScript · Tailwind CSS · `claude`/`codex`/`agy` CLIs (local model backend) · AWS Bedrock AgentCore SDK (entrypoint written, not yet deployed) · Telegram Bot API
