# DealSieve

**The acquisitions agent for small-bay industrial buyers. It underwrites every broker OM on your numbers, keeps a clearing price on every pass and re-underwrites when the facts change, and works the deals that clear through diligence. You keep the money decisions.**

Built with the [Strands Agents SDK](https://strandsagents.com/) for the AWS *Agents for Humans* hackathon (Professional Agents track). Deployed to Bedrock AgentCore Runtime; runs fully offline too. 493 tests.

## The story, with real numbers

One property, 8330 Power Inn Road, Sacramento (8-unit small-bay industrial, 20,000 sf), under a frozen policy: $500k equity, 8.0% minimum normalized cap, 1.35x minimum DSCR (debt service coverage, income divided by loan payments), 75% maximum LTV, no tenant above 25% of rent.

| What arrives | What DealSieve does | You |
|---|---|---|
| Broker email: asking **$1,550,000**, "8.13% cap" | Resets property tax, adds vacancy, management and reserves the broker left out: cap **6.42%**, DSCR **1.02x**. **WATCH**. Solves the frontier: viable below **$1,285,946**, 17% under the ask. Remembers that. | Not interrupted |
| Reply in the same thread: "Seller reduced this to $1.25M" | Same deal via the thread. Re-underwrites: cap **8.27%**, DSCR **1.43x**. **REVIEW**. The Skeptic finds nothing supporting roof age, Phase I or CAM and drafts the questions. | **Alert 1.** One tap approves the request; it goes out |
| Broker attaches a property condition report (PDF, 3 photos) | The Inspector reads the text and the photos: roof original to 2001, ponding, blistering, $85k to $95k to replace. **$90,000** of verified capex enters the basis: cap **7.71%**, DSCR **1.30x**, LTV **75.2%**. **NEAR**, viable below **$1,208,108**. Drafts a **$42,000** credit request. | **Alert 2.** The credit request waits for you |
| Silence from the broker | Follows up on Phase I and CAM only, at day 3 and day 6. Stops at day 9. | **Alert 3**, the last one |

Three interruptions across the life of a deal, each because the decision changed. Everything above reproduces offline in about three seconds (`make demo-offline`), and live through Claude, Gemini or Codex as the model.

Terms, for readers outside real estate: the cap rate is the annual return on the purchase price; DSCR is income divided by loan payments; a Phase I is the environmental site assessment; a CAM reconciliation is the yearly true-up of shared building expenses billed to tenants; the Prop 13 reset is California reassessing property tax to the sale price.

## Why this is not another document-reading copilot

- **A standing opinion.** Every opportunity is a permanent record with an immutable event history and immutable underwriting runs.
- **A calculated counterfactual.** For every rejection, DealSieve solves for the price at which every gate would pass and names the binding constraint.
- **No emotion in the answer.** The policy is frozen and versioned by content hash. A building you have fallen for has to clear the same numbers as every other one, so the system never talks you into a bad buy.
- **Minimal notifications.** Rejecting 71 of 84 deals produces zero notifications. You hear from it when a monitored condition changes the answer, when diligence alters the result, or when the broker stops answering.

## How it works

![DealSieve at a glance: an ambient intake watches the inbox; Strands agents read and judge; a deterministic engine does the math and holds the guardrails; a human approves every first message and anything about money; an append-only ledger and AgentCore Memory underneath, all on Amazon Bedrock AgentCore Runtime](architecture/architecture.png)

*Amber: the agents judge. Blue: code decides. Green: you approve. The animated version is [architecture/overview.html](architecture/overview.html); open it in a browser. A node-level flowchart is in [architecture/diagram.html](architecture/diagram.html).*

**Model discretion vs. deterministic code.** The Strands agents perform tasks that require judgment, such as reading a messy email, a 40-page OM, or a condition report with photos. They identify claims, cite the page or photo for each, and determine which claims are unsupported and material. All subsequent steps are executed by code. The tool order is fixed, every gate evaluates within the tool body, and a safety net runs any step the model skips. The credit amount is determined by frontier arithmetic, and diligence questions are reconciled against the Skeptic's list. If the model called no tools after extraction, the outcome would be identical. The system acts as the agent, with the LLM serving as a perception and judgment component.

- **Acquisition Agent** (`dealsieve/agents/acquisition.py`): a Strands `Agent` whose tools are the deterministic steps: `record_claims`, `analyze_document`, `underwrite`, `request_skeptic_review`, `request_diligence`, `request_price_adjustment`, `notify_human`.
- **Skeptic Agent** (`agents/skeptic.py`): independent, no tools, `structured_output_model`. Argues that the numbers rest on unverified claims; never recomputes finance.
- **Inspector Agent** (`agents/inspector.py`): multimodal. Text and embedded photos in, findings with page/photo provenance and capex proposals out.
- **Model providers** (`dealsieve/models/`): `CLIModel`, a custom Strands provider that runs `claude`, `codex` or `agy` as the LLM on existing subscriptions; `ScriptedModel` for deterministic offline runs and tests; Strands' `BedrockModel` and `AnthropicModel` by environment variable.

## What it remembers about you

Every decision you make is recorded as a memory and read back by the agents on the next deal: an approval, a rejection with the reason you typed, an alert you ignored, and how each broker behaves (who answers in three days, who goes silent after two follow-ups). The Skeptic sees "what this investor decided before" when it chooses what to chase, a credit request's rationale notes the last similar ask you approved or rejected, and alerts end with a "You previously:" line when a strong match exists. Memory never changes a number: prices, frontiers, capex and gate results come from the ledger and the engine.

The store is `dealsieve/memory/`. `DEALSIEVE_MEMORY=local` (default) keeps it in SQLite with deterministic recall; `DEALSIEVE_MEMORY=agentcore` writes through to Amazon Bedrock AgentCore Memory (`AGENTCORE_MEMORY_ID` or a memory created on first use) and falls back to local if AWS is unreachable. Browse it at `/memory` in the dashboard, `GET /api/memory`, or `dealsieve memory --q "roof credit"`.

## Guarantees enforced in code

| Guarantee | Mechanism |
|---|---|
| The model never performs arithmetic that reaches a gate | `dealsieve/underwriting/`: pure `Decimal` functions; gates and the bisection run on full precision; nonpositive inputs fail closed |
| Policy is immutable at runtime | Agents read `config/investment_policy.yaml`; every run records the policy's content hash |
| Nothing involving money or terms leaves without a human | Every outbound message passes a deterministic screen (price, credit, deposit, contingency, financing, LOI, PSA); approval is loopback-or-token gated, compare-and-set, one Message-ID per draft |
| The first diligence message waits for one-tap approval; follow-ups on an approved thread are automatic and capped | `outreach` policy; requests are reserved atomically before transport and re-checked for answers |
| Capex from a document is verified by text evidence | Both ends of a proposed range must appear in the document's own text; verified items are aggregated across documents; rejects are recorded |
| Diligence questions originate from the Skeptic | Server-side reconciliation of the model's items against the Skeptic's questions; unmatched items dropped |
| One delivered alert per deal, run and kind, and alerts report delivery truthfully | Notification intent persisted with a dedupe key before delivery; resumed on retry if delivery failed |
| History is append-only; messages are processed once | Immutable events and runs; message claim/complete/fail states; duplicates are no-ops |
| A "Re:" subject line can never re-set a price | Reconciliation ignores an asking price without body or attachment evidence |

The state machine, its invariants and the trace explorer that checks them are in [`docs/STATE_MACHINE.md`](docs/STATE_MACHINE.md) and `tests/model/`.

## Run it

```bash
make setup            # Python 3.12 venv via uv
cp .env.example .env
make test             # 491 deterministic tests, no model calls
make demo-offline     # acts 1 and 2 with the scripted model, ~3 s; ends with the request awaiting approval
make frontend && PORT=8010 make api   # dashboard at http://localhost:8010
```

Then, for acts 3 and 4: approve the request in the dashboard (or `curl -X POST localhost:8010/api/drafts/<id>/approve`), `python scripts/inject_email.py fixtures/emails/05_inspection_report.eml`, and `dealsieve followup --as-of 2026-09-17`, `--as-of 2026-09-21`, `--as-of 2026-09-25`. `make demo` runs the same emails through a real model. `dealsieve --help` lists every command; `docs/DEMO_SCRIPT.md` is the timed walkthrough.

| `DEALSIEVE_MODEL_BACKEND` | Needs | Notes |
|---|---|---|
| `cli` (default) | `claude`, `codex` or `agy` on PATH | `DEALSIEVE_CLI_PROVIDER`, `DEALSIEVE_CLI_MODEL`. Sonnet ~3 min per email, Gemini Flash ~1 to 2 min. Codex reads the photos |
| `bedrock` | AWS credentials | `AWS_REGION`, `DEALSIEVE_BEDROCK_MODEL` (default `global.anthropic.claude-sonnet-4-6`) |
| `anthropic` / `openai` | API key | `DEALSIEVE_ANTHROPIC_MODEL` / `DEALSIEVE_OPENAI_MODEL` |
| `scripted` | nothing | Replays `fixtures/scripted/*.json`; used by tests and the offline demo |

Other settings, all in `.env.example`: database path, policy path, notifier (`console` or `telegram` with `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`), outbox (`file`, `smtp`), `DEALSIEVE_APPROVER_TOKEN` for approvals from outside loopback.

## Deployment

**AgentCore.** `dealsieve/agentcore_app.py` wraps the same pipeline in a `BedrockAgentCoreApp`; it is successfully deployed to Bedrock AgentCore Runtime in us-west-2, where a status invocation passed (see the immutable [`deploys/LEDGER.tsv`](deploys/LEDGER.tsv)). The full Strands `BedrockModel` path is implemented, but the selected model currently has zero on-demand token throughput on this new AWS account; that account quota affects model inference, not the successful AgentCore deployment. Redeploy with `agentcore configure --entrypoint dealsieve/agentcore_app.py --requirements-file requirements-agentcore.txt && agentcore launch`, then `scripts/log_deploy.sh`. Local check: `python -m dealsieve.agentcore_app` and `curl -X POST localhost:8080/invocations -d '{"type":"status"}'`.

**Telegram.** Set the notifier to `telegram` with a bot token and chat id; alerts arrive with inline Approve / Reject buttons, and `dealsieve telegram-bot` long-polls for replies, documents and button presses. Without a token, the console notifier prints the same alert.

## Repository map

```text
dealsieve/agents        acquisition, skeptic, inspector, tools (the gated steps)
dealsieve/underwriting  normalize, financing, gates, viability, classify, engine (pure Decimal)
dealsieve/diligence     requests, money screen, dispatch, approvals, follow-ups
dealsieve/evidence      reconcile claims into working values, keep conflicts
dealsieve/identity      same-property resolution across emails
dealsieve/ingestion     email (.eml, PDF text and images), text, telegram
dealsieve/models        backend selection, CLIModel, ScriptedModel
dealsieve/outbound      file / SMTP outbox
dealsieve/persistence   SQLite: immutable events and runs, derived opportunity
dealsieve/api           FastAPI under /api, serves the dashboard
frontend/               React dashboard: watchlist, deal detail, diligence, correspondence, documents
fixtures/               the demo emails, OM, condition report with photos, golden extractions, scripts
tests/                  underwriting, identity, persistence, agents, diligence, api, model, e2e
docs/                   DEALSIEVE_PLAN.md (product), CONTRACTS.md (module contracts), STATE_MACHINE.md, DEMO_SCRIPT.md, SUBMISSION.md
```

Roadmap and the reusable primitive (persistent opportunity state, deterministic policy, monitored counterfactual thresholds) are in the plan, section 32. Photo attribution in `fixtures/ATTRIBUTION.md`. [MIT](LICENSE).
