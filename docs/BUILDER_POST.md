# Agents for Humans: Building DealSieve, an Acquisition Agent That Knows When Not to Interrupt

Commercial real-estate acquisitions teams receive a steady stream of broker emails, offering
memorandums, price updates, inspection reports, and unanswered diligence threads. The work is not
hard because any one document is impossible to read. It is hard because the decision has to remain
current as the facts change.

A deal that fails today may become attractive after a price cut. A deal that passes on paper may
fall apart when a property-condition report adds $90,000 of immediate roof work. Meanwhile, the
same team is tracking roof age, environmental reports, expense reconciliations, lease rollover, and
follow-ups across dozens of opportunities.

For the AWS Agents for Humans Hackathon, we built **DealSieve**: an acquisitions agent for small-bay
industrial buyers. It underwrites every opportunity using the buyer's own policy, remembers the
price at which a rejected deal would work, re-underwrites when a material fact changes, and manages
the diligence chase. The human keeps the decisions involving money.

The source is available in the [public DealSieve repository](https://github.com/mpnikhil/DealSieve).

## The product principle: interrupt only when the decision changes

Most deal-screening software produces more things to look at. We wanted the opposite.

DealSieve classifies each opportunity as DEAD, WATCH, NEAR, or REVIEW. When a deal fails only on
economic gates, it solves for the maximum viable price rather than merely returning “no.” That
turns a rejected opportunity into a monitored counterfactual: *this works at $1,285,946, so tell me
if the seller gets there.*

The demo follows one synthetic Sacramento property from start to finish:

1. A broker asks **$1,550,000** and advertises an 8.13% cap rate. After resetting property taxes and
   adding vacancy, management, and reserves, DealSieve calculates a **6.42% normalized cap rate**
   and **1.02x DSCR**. The deal is WATCH and viable below **$1,285,946**. No alert is sent.
2. The seller drops the price to **$1,250,000** in the same email thread. DealSieve resolves it to
   the existing opportunity and re-underwrites it at an **8.27% cap rate** and **1.43x DSCR**. Every
   gate passes, so the status becomes REVIEW and the first alert is sent.
3. A Skeptic Agent identifies unsupported assumptions—roof age, Phase I environmental status, and
   CAM reconciliation—and drafts a broker request. A human approves it from Telegram before it is
   sent.
4. The broker replies with a condition report containing text and photographs. An Inspector Agent
   finds ponding and membrane deterioration on the original 2001 roof and verifies an
   **$85,000–$95,000** replacement range. DealSieve adds **$90,000** of immediate capex to the basis
   and runs the numbers again.
5. The cap rate falls to **7.71%**, DSCR to **1.30x**, and LTV rises to **75.2%**. The opportunity
   moves back to NEAR, viable below **$1,208,108**, and a **$42,000 credit request** is drafted. It
   remains unsent because money always waits for a person.
6. Follow-ups continue only for the unanswered Phase I and CAM questions. After two follow-ups and
   no response, the thread is marked stalled and the third and final human alert fires.

Three interruptions across the entire life of the deal: when it became investable, when new facts
made it miss the policy again, and when diligence stalled.

## Why Strands Agents SDK fit the problem

This workflow needs both judgment and control. A model is useful for turning messy emails and PDFs
into structured claims, inspecting document photographs, and identifying which unsupported facts
are material. It should not be trusted to improvise a cap rate, change an investment policy, or
decide that a credit request may leave without approval.

The Strands Agents SDK gave us a clean way to express that boundary. DealSieve uses three specialized
agents:

- The **Acquisition Agent** extracts claims with provenance and follows a fixed tool-call procedure.
- The **Skeptic Agent** provides an independent second opinion through structured output. It has no
  tools and cannot modify the opportunity.
- The **Inspector Agent** reads report text and embedded images, then returns findings with page and
  photograph provenance.

The Acquisition Agent can call tools such as `record_claims`, `underwrite`,
`request_skeptic_review`, `request_diligence`, `request_price_adjustment`, and `notify_human`.
However, the important rules live inside those tools. The model can request an action; deterministic
Python decides whether the action is legal and what the resulting numbers are.

That separation made the system easier to reason about. Tool order is enforced. Each underwriting
run records the immutable policy hash. Notifications require a real state transition. Monetary
language passes through an outbound policy screen. Message processing, draft approval, and alert
delivery use idempotency keys and compare-and-set transitions so retries do not double-send.

The model handles ambiguity. The tools protect invariants.

## Deploying the same pipeline with Amazon Bedrock AgentCore

We did not want a separate “cloud demo” implementation. The same `process_inbound` pipeline used by
the local CLI and FastAPI dashboard is wrapped by a small `BedrockAgentCoreApp` entrypoint. It accepts
email, text, status, and deal-detail payloads and returns the same typed JSON objects as the local
application.

We deployed that entrypoint to **Amazon Bedrock AgentCore Runtime in `us-west-2`** using a direct code
deployment. The deployed runtime passed its status invocation. Keeping AgentCore-specific code at
the application boundary made the deployment simple without coupling the domain logic to the
hosting environment.

The model provider is also selected by configuration. The production path supports the Strands
`BedrockModel`; local development can use another provider; and a deterministic `ScriptedModel`
replays captured agent turns for tests and offline demonstrations. This was especially useful during
the hackathon: we could iterate quickly without consuming cloud inference for every test, then move
the same agent and tool graph into AgentCore Runtime.

The AgentCore deployment itself is successful. The selected Bedrock model currently has zero
on-demand token throughput on this new AWS account, so the full cloud inference path is waiting on
account quota availability. AWS documents that Bedrock inference quotas are account- and
Region-specific and that new accounts may receive reduced quotas. We chose to document that boundary
rather than imply a cloud model invocation that did not occur. The project remains fully reproducible
through its scripted backend, and switching to Bedrock requires an environment change rather than an
application rewrite.

DealSieve also includes optional integration points for **Amazon Bedrock AgentCore Memory** and
**Amazon SES**. AgentCore Memory can store approvals, rejections, ignored alerts, and broker-response
patterns while deterministic finance remains the source of truth. SES can serve as an inbound and
outbound email transport behind the same channel-neutral message and outbox contracts.

## What the AWS agent platform gave us

The greatest benefit was architectural leverage rather than a single API call.

**Strands made the agent loop explicit.** Model requests, tool specifications, tool results, and
structured outputs all have visible contracts. That let us make the agent useful without hiding
business logic inside a prompt.

**AgentCore gave the application a production-shaped runtime boundary.** The local and hosted paths
share one pipeline, so deployment does not create a second behavior that has to be tested and
maintained independently.

**Bedrock keeps the model layer swappable.** DealSieve's agents depend on the Strands model interface,
not on provider-specific calls scattered throughout the codebase. That makes the choice of model a
deployment concern while the tools, policy, and event history remain stable.

**AWS services compose around the workflow.** AgentCore Runtime, AgentCore Memory, Bedrock models,
and SES each map to a distinct responsibility. We can adopt them independently instead of turning
the entire application into one opaque agent.

## Reliability was part of the feature

Acquisition work is financially consequential, so “the demo looked right once” was not enough. The
repository currently passes **493 tests** covering underwriting boundaries, amortization, property-
tax reset, viability bisection, identity resolution, evidence conflicts, multimodal inspection,
workflow invariants, approvals, notifications, and follow-up behavior.

The event ledger and underwriting runs are append-only. The current opportunity is a derived
projection. That design gives every conclusion an audit trail: what arrived, which claims were
accepted, which document supported them, what policy version ran, why a gate failed, and whether a
human approved an outbound message.

The result is an agent that can act persistently without being allowed to rewrite the truth.

## What we learned

The most useful agents are not necessarily the ones with the most autonomy. They are the ones with
the clearest authority.

DealSieve is free to read, reconcile, monitor, draft, and follow up. It is not free to invent
arithmetic, silently change policy, or send terms involving money. Those constraints did not make
the product feel less agentic. They made it possible to trust the agent with a workflow that lasts
for weeks.

For us, that is the promise of the AWS agent platform: Strands provides a disciplined way to connect
reasoning with tools, and AgentCore provides a path from that loop to an operational service. The
combination let us build something that does real work for a professional user while preserving the
human decision at exactly the point where it matters.

DealSieve works the pipeline. The investor prices the basis.

---

**Suggested Builder Center tags:** Agents for Humans, Strands Agents, Amazon Bedrock AgentCore,
Amazon Bedrock, Generative AI, Python

**Assets to include when publishing:**

- `architecture/architecture.png`
- Public demo-video URL once uploaded to YouTube or Vimeo
- Repository: https://github.com/mpnikhil/DealSieve
