# Current Status

Last updated: 2026-09-13 (morning PDT). Submission deadline: 2026-09-14 17:00 PDT.

## Completed
- Phase 1: deterministic underwriting engine, immutable policy, SQLite event store, identity resolution, .eml ingestion, Strands Acquisition and Skeptic agents, `CLIModel` (claude/codex/agy) and `ScriptedModel` providers, FastAPI + React dashboard, console/Telegram notifiers, CLI, AgentCore entrypoint.
- Phase 2: autonomous diligence loop with humans in the loop. Skeptic concerns become tracked requests; the first broker message waits for one-tap approval; follow-ups on approved threads are automatic (3 days, max 2) and stall with a single alert; the Inspector agent reads inspection reports and their photos; verified capex enters the all-in basis; the deal is re-underwritten; money talk is always drafted for approval. Dashboard: Diligence, Correspondence, Documents panels.
- Two Codex adversarial reviews (18 findings) fixed with regression tests: full-precision gates, fail-closed inputs, tool phase machine and latches, message processing states, notification dedupe and delivery truth, contradiction-aware identity, evidence-derived reconciliation, approver gate, atomic approval, broad money screen, reserved follow-ups, verified/aggregated capex, image containment.
- Live-model robustness: subject-line prices never re-set the ask; any skeptic concern with a broker question is chased; Codex receives the prompt on stdin when images are attached.
- 366 offline tests green; both end-to-end specs green; ruff clean.

## Verified end to end
- Offline (`make demo-offline` + approve + inject 05 + three `followup` ticks): WATCH -> REVIEW (one alert, request awaiting approval) -> approved and sent -> inspection report read (3 photos) -> $90,000 verified capex -> NEAR at $1,208,108 (second alert, $42,000 credit request pending) -> two follow-ups -> stall (third alert). Replayed approval does not resend.
- Live: acts 1 and 2 through Claude Sonnet, Gemini Flash (agy) and Codex; act 3 through Codex with the three photos attached (12 findings, 3 capex items, REVIEW -> NEAR, credit draft pending).

## Blockers
- AgentCore Runtime deployed from another session (see deploys/LEDGER.tsv); the model path waits on a new account's Bedrock tokens-per-day quota (0, auto-lifts). No AWS credentials on this build machine.
- No Telegram bot token: Telegram delivery and inline approval untested (console notifier and dashboard approval verified).

## Next Three Tasks
1. Record the demo video from docs/DEMO_SCRIPT.md (three acts, <= 5:00) and submit on Devpost with docs/SUBMISSION.md.
2. When the Bedrock quota lifts: run the two-email story against the deployed runtime, log the row in deploys/LEDGER.tsv, add the runtime to the submission.
3. If a Telegram token arrives: run `dealsieve telegram-bot`, approve the information request from the phone, screenshot for the video.

## Demo Health
Offline three-act story: PASS
Live acts 1-2 (claude / agy / codex): PASS
Live act 3 with photos (codex): PASS
Dashboard against live data: PASS
Telegram: UNTESTED (no token)
SES: NOT BUILT (file outbox stands in; SMTP/SES adapters behind the same interface)
AgentCore: DEPLOYED, status path PASS, model path PENDING Bedrock quota
