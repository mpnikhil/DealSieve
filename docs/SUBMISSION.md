# DealSieve — Devpost submission text

## Tagline

Rejects deals, remembers exactly why, and interrupts you only when its mind should change.

## Elevator pitch

DealSieve is a persistent acquisition agent for commercial real estate buyers: it reads every broker email that arrives, underwrites it against a frozen investment policy, and — instead of forgetting a rejected deal — computes the exact price at which it would flip that verdict and keeps watching for it. When a later email changes the one condition that mattered, DealSieve re-underwrites automatically, runs an independent skeptic pass, and interrupts a human exactly once. Everything else, which is almost everything, stays silent.

## Inspiration

Acquisition professionals don't have a document-reading problem; they have an attention-allocation problem. Every AI tool aimed at this space competes on the same axis — read the OM faster, extract the rent roll faster — which only lets a human look at *more* deals per hour. That's the wrong lever. The real cost is that a deal correctly rejected today is usually rejected forever, even after the one fact that killed it (usually price) changes six weeks later. We wanted an agent that holds a standing, evidence-backed opinion on every opportunity it has ever seen, and only asks for attention when that opinion should change.

## What it does

DealSieve ingests broker emails (with offering-memorandum attachments), Telegram messages, and pasted URLs. A Strands Acquisition Agent extracts claims with per-fact provenance, then hands off to deterministic tools that resolve the property to one canonical opportunity, reconcile the claims into working values, and run a `Decimal`-precision underwriting engine under an immutable investment policy — property-tax reset, vacancy floor, management fee, capex reserve, then six hard gates (tenant count, tenant concentration, absolute price cap, LTV, normalized cap rate, DSCR). The engine classifies the result DEAD / WATCH / NEAR / REVIEW and, for anything short of REVIEW, solves a bisection to find the exact "viability frontier" — the maximum price at which every economic gate would pass, and which gate is binding.

On the demo property (8330 Power Inn Road, Sacramento — 8-unit small-bay industrial, 20,000 sf), the initial broker email at $1,550,000 (broker-stated 8.13% cap) normalizes to a 6.42% cap and 1.02x DSCR: WATCH, viable below $1,285,946, no human notified. Later the same broker thread announces a price drop to $1,250,000. DealSieve recognizes the same opportunity, re-underwrites automatically — 8.27% cap, 1.43x DSCR, 68% LTV — and the status crosses WATCH → REVIEW. An independent Skeptic Agent (a second Strands agent, no tools, structured output only) is invoked specifically because the deal now looks clean on paper, and flags what the record still doesn't support: roof age, a Phase I environmental report, CAM reconciliation. It drafts an information request and a human is interrupted on Telegram with an approve action.

Once approved, the request is delivered to the broker. In Act 3, the broker replies with an attached Property Condition Report containing real photographs. A third Strands agent, the Inspector Agent, reads the document text and visually inspects embedded photos (ponding water, blistering on the 2001 built-up membrane). It answers the open roof question and extracts $90,000 of immediate roof capex. DealSieve automatically re-underwrites on the all-in basis: purchase price plus capex pushes total basis to $1,340,000, cap rate drops to 7.71%, DSCR to 1.30x, and LTV to 75.2%. The deal moves REVIEW → NEAR (viable below $1,208,108). A second human alert fires with an automatically drafted $42,000 price-credit request. Unanswered requests are followed up on a policy cadence; if the broker goes dark, the loop stalls and alerts the human.

## How we built it

Workstreams against a shared contracts document (`docs/CONTRACTS.md`), so the finance engine, persistence layer, Strands agent layer, diligence loop, API/notifications, and dashboard could be built concurrently against fixed Pydantic schemas (`dealsieve/schemas/core.py`) without stepping on each other:

- **Finance** (`dealsieve/underwriting/`): pure, tested functions on `Decimal` — all-in basis underwriting (`total_acquisition_cost = price + closing_costs + immediate_capex`), normalization, financing, stress testing, gates, and bisection viability solver.
- **Persistence** (`dealsieve/persistence/`): SQLite in WAL mode; events, underwriting runs, and document analyses are immutable; concurrency-safe deal/event sequence allocation with `BEGIN IMMEDIATE` (R10); message claiming (R4); deduplication constraints.
- **Agents** (`dealsieve/agents/`, `dealsieve/models/`): three specialized Strands agents — (1) Acquisition Agent with deterministic code-gated tools, (2) Skeptic Agent for second-opinion risk audit, and (3) Inspector Agent with multimodal vision analyzing inspection PDFs and photos. A custom `CLIModel` Strands provider supports local CLIs (`claude`, `codex`, `agy`), and `ScriptedModel` replays recorded turns for deterministic offline demos and tests.
- **Diligence & Delivery** (`dealsieve/diligence/`, `dealsieve/outbound/`): deterministic screen blocking unauthorized offers or money language, follow-up cadence advancement, keyword-family answer matching, stall detection, and RFC 822 `.eml` delivery backends.
- **API/notifications** (`dealsieve/api/`, `dealsieve/notifications/`): FastAPI surface, console and Telegram notifiers with inline-keyboard approval, and the `dealsieve/agentcore_app.py` AWS Bedrock AgentCore entrypoint.
- **Dashboard** (`frontend/`): Vite + React + TypeScript + Tailwind — overview watchlist sorted by distance to viability, and a deal-detail view with the broker-vs-DealSieve comparison table, viability frontier, diligence tracker, unified correspondence thread, document analysis with photo lightbox, and pending drafts.

## Challenges

Keeping the model out of the arithmetic while still letting it drive the workflow was the central design problem: it's easy to ask an LLM for a cap rate and get something plausible-looking and wrong. The fix was making every deterministic step a tool call with its own preconditions enforced in code (`notify_human` returns `{"skipped": "no threshold crossing"}` unless the immediately preceding underwriting run just flipped status; `request_price_adjustment` calculates the exact mathematical credit needed from the viability frontier), plus a pipeline-level safety net that performs any step (as a `SYSTEM` actor) the model forgot, so the demo never silently depends on the model remembering a procedure. A second challenge was making the demo bulletproof against network flakiness during a 5-minute recording: the `ScriptedModel` replays exact recorded turns with zero network calls, so the whole 3-act story reproduces byte-for-byte offline, while the same code path runs against a real CLI or Bedrock with one environment variable changed.

## Accomplishments

- A viability solver that turns "no" into a specific, monitorable number, not just a rejection.
- A tool-gated multi-agent system where the properties that make a hackathon demo scary — the model hallucinating a pass, doing unchecked arithmetic, or double-notifying a human — are structurally impossible.
- An end-to-end 3-act story (WATCH -> REVIEW -> Diligence & Condition Report -> Capex adjustment & price-credit negotiation) fully tested (`tests/e2e/test_watch_to_review.py` and `tests/e2e/test_diligence_loop.py`) and reproducible offline in 3 seconds.
- 304 passing tests covering gate boundaries, the amortization schedule, the property-tax reset, bisection convergence on the viability frontier, stress scenarios, identity resolution, evidence-conflict preservation, multimodal document inspection, and the diligence follow-up loop.

## What we learned

Deterministic-first agent design pays off immediately in testability: because every dollar of arithmetic and every workflow gate is a plain Python function, the entire underwriting engine and tool-gating logic is unit-testable with zero mocking of a model, and the model layer itself is swappable to a fully scripted replay for CI and offline demos. We also learned that a custom Strands `Model` provider is a genuinely small surface (render a request to a prompt, parse a schema-constrained JSON response back into `StreamEvent`s) — cheap enough that building one against local CLI tools, rather than requiring API keys from day one, was clearly worth it for iteration speed.

## What's next

Broker-email monitoring at mailbox scale rather than one fixture at a time; listing monitoring from crawl/API sources instead of forwarded email; an off-market owner universe built from assessor data; loan-rate and seller-financing monitoring as additional axes on the same viability-frontier machinery that today only varies price; market-rent and transaction comps to sanity-check normalized NOI against something other than the stated rent roll; and, longer term, the same primitive — persistent opportunity state, deterministic policy, monitored counterfactual thresholds — applied outside CRE to small-business, franchise, private-credit, and equipment acquisitions.

**On AgentCore**: the entrypoint (`dealsieve/agentcore_app.py`) is written and verified locally (`python -m dealsieve.agentcore_app` + `curl localhost:8080/invocations`), but has not been deployed to AWS — there were no AWS credentials available on the build machine during the hackathon. The deployment steps (`agentcore configure --entrypoint dealsieve/agentcore_app.py` && `agentcore launch` via the `bedrock-agentcore-starter-toolkit`) are documented in the README and are the next thing we'd run given credentials.

## Built with

Python 3.12 · Strands Agents SDK · Pydantic · SQLite · FastAPI · Vite · React · TypeScript · Tailwind CSS · `claude`/`codex`/`agy` CLIs (local model backend) · AWS Bedrock AgentCore SDK (entrypoint written, not yet deployed) · Telegram Bot API
