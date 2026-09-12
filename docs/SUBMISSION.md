# DealSieve — Devpost submission text

## Tagline

Rejects deals, remembers exactly why, and interrupts you only when its mind should change.

## Elevator pitch

DealSieve is a persistent acquisition agent for commercial real estate buyers: it reads every broker email that arrives, underwrites it against a frozen investment policy, and — instead of forgetting a rejected deal — computes the exact price at which it would flip that verdict and keeps watching for it. When a later email changes the one condition that mattered, DealSieve re-underwrites automatically, runs an independent skeptic pass, and interrupts a human exactly once. Everything else, which is almost everything, stays silent.

## Inspiration

Acquisition professionals don't have a document-reading problem; they have an attention-allocation problem. Every AI tool aimed at this space competes on the same axis — read the OM faster, extract the rent roll faster — which only lets a human look at *more* deals per hour. That's the wrong lever. The real cost is that a deal correctly rejected today is usually rejected forever, even after the one fact that killed it (usually price) changes six weeks later. We wanted an agent that holds a standing, evidence-backed opinion on every opportunity it has ever seen, and only asks for attention when that opinion should change.

## What it does

DealSieve ingests broker emails (with offering-memorandum attachments), Telegram messages, and pasted URLs. A Strands Acquisition Agent extracts claims with per-fact provenance, then hands off to deterministic tools that resolve the property to one canonical opportunity, reconcile the claims into working values, and run a `Decimal`-precision underwriting engine under an immutable investment policy — property-tax reset, vacancy floor, management fee, capex reserve, then six hard gates (tenant count, tenant concentration, absolute price cap, LTV, normalized cap rate, DSCR). The engine classifies the result DEAD / WATCH / NEAR / REVIEW and, for anything short of REVIEW, solves a bisection to find the exact "viability frontier" — the maximum price at which every economic gate would pass, and which gate is binding.

On the demo property (8330 Power Inn Road, Sacramento — 8-unit small-bay industrial, 20,000 sf), the initial broker email at $1,550,000 (broker-stated 8.13% cap) normalizes to a 6.42% cap and 1.02x DSCR: WATCH, viable below $1,285,953, no human notified. Later the same broker thread announces a price drop to $1,250,000. DealSieve recognizes the same opportunity, re-underwrites automatically — 8.27% cap, 1.43x DSCR, 68% LTV — and the status crosses WATCH → REVIEW. An independent Skeptic Agent (a second Strands agent, no tools, structured output only) is invoked specifically because the deal now looks clean on paper, and flags what the record still doesn't support: roof age, a Phase I environmental report, CAM reconciliation. It drafts three broker questions and a human is interrupted exactly once, on Telegram, with a message showing what changed and what's still unresolved. Nothing is ever sent to a broker without explicit human approval.

## How we built it

Five parallel workstreams against a shared contracts document (`docs/CONTRACTS.md`), so the finance engine, persistence layer, Strands agent layer, API/notifications, and dashboard could be built concurrently against fixed Pydantic schemas (`dealsieve/schemas/core.py`) without stepping on each other.

- **Finance** (`dealsieve/underwriting/`): pure, tested functions on `Decimal` — no I/O, no randomness, no model calls anywhere near a dollar figure.
- **Persistence** (`dealsieve/persistence/`): SQLite in WAL mode; events and underwriting runs are append-only; the `Opportunity` row is the only mutable, derived projection.
- **Agents** (`dealsieve/agents/`, `dealsieve/models/`): a Strands Acquisition Agent with five `@tool` functions that do all the deterministic work — every gate that decides what happens next lives in the tool body, not the prompt, so the model literally cannot invent a status or notify a human without a real threshold crossing. A custom `CLIModel` Strands model provider runs `claude -p` / `codex exec` / `agy -p` as the LLM for day-to-day development on existing subscriptions instead of API keys; a `ScriptedModel` replays fixed turns for deterministic tests and an offline demo; `BedrockModel`/`AnthropicModel` are one environment variable away.
- **API/notifications** (`dealsieve/api/`, `dealsieve/notifications/`): FastAPI surface, a console notifier and a Telegram notifier with an inline-keyboard approve/reject flow, and the `dealsieve/agentcore_app.py` AWS Bedrock AgentCore entrypoint.
- **Dashboard** (`frontend/`): Vite + React + TypeScript + Tailwind — an overview watchlist sorted by distance to viability, and a deal-detail view with the broker-vs-DealSieve comparison table, the viability frontier, gate results, the immutable event timeline, skeptic concerns, and pending drafts.

## Challenges

Keeping the model out of the arithmetic while still letting it drive the workflow was the central design problem: it's easy to ask an LLM for a cap rate and get something plausible-looking and wrong. The fix was making every deterministic step a tool call with its own preconditions enforced in code (`notify_human` returns `{"skipped": "no threshold crossing"}` unless the immediately preceding underwriting run just flipped the status into REVIEW; `draft_broker_questions` refuses without an existing skeptic report), plus a pipeline-level safety net that performs any step (as a `SYSTEM` actor) the model forgot, so the demo never silently depends on the model remembering a procedure. A second challenge was making the demo bulletproof against network flakiness during a 5-minute recording: the `ScriptedModel` replays exact recorded turns with zero network calls, so the whole WATCH → REVIEW story reproduces byte-for-byte offline, while the same code path runs against a real CLI or Bedrock with one environment variable changed.

## Accomplishments

- A viability solver that turns "no" into a specific, monitorable number, not just a rejection.
- A tool-gated agent loop where the properties that make a hackathon demo scary — the model hallucinating a pass, or double-notifying a human — are structurally impossible, not just prompted against.
- An end-to-end WATCH → price-change → REVIEW path that is fully tested (`tests/e2e/test_watch_to_review.py`) and reproducible offline.
- 215 passing tests covering gate boundaries, the amortization schedule, the property-tax reset, bisection convergence on the viability frontier, stress scenarios, identity resolution, evidence-conflict preservation, and the custom Strands model provider's request/response cycle.

## What we learned

Deterministic-first agent design pays off immediately in testability: because every dollar of arithmetic and every workflow gate is a plain Python function, the entire underwriting engine and tool-gating logic is unit-testable with zero mocking of a model, and the model layer itself is swappable to a fully scripted replay for CI and offline demos. We also learned that a custom Strands `Model` provider is a genuinely small surface (render a request to a prompt, parse a schema-constrained JSON response back into `StreamEvent`s) — cheap enough that building one against local CLI tools, rather than requiring API keys from day one, was clearly worth it for iteration speed.

## What's next

Broker-email monitoring at mailbox scale rather than one fixture at a time; listing monitoring from crawl/API sources instead of forwarded email; an off-market owner universe built from assessor data; loan-rate and seller-financing monitoring as additional axes on the same viability-frontier machinery that today only varies price; market-rent and transaction comps to sanity-check normalized NOI against something other than the stated rent roll; and, longer term, the same primitive — persistent opportunity state, deterministic policy, monitored counterfactual thresholds — applied outside CRE to small-business, franchise, private-credit, and equipment acquisitions.

**On AgentCore**: the entrypoint (`dealsieve/agentcore_app.py`) is written and verified locally (`python -m dealsieve.agentcore_app` + `curl localhost:8080/invocations`), but has not been deployed to AWS — there were no AWS credentials available on the build machine during the hackathon. The deployment steps (`agentcore configure --entrypoint dealsieve/agentcore_app.py` && `agentcore launch` via the `bedrock-agentcore-starter-toolkit`) are documented in the README and are the next thing we'd run given credentials.

## Built with

Python 3.12 · Strands Agents SDK · Pydantic · SQLite · FastAPI · Vite · React · TypeScript · Tailwind CSS · `claude`/`codex`/`agy` CLIs (local model backend) · AWS Bedrock AgentCore SDK (entrypoint written, not yet deployed) · Telegram Bot API
