# Current Status

Last updated: 2026-09-13 14:35 PDT. Submission deadline: 2026-09-14 17:00 PDT.

## Completed
- Phase 1: deterministic underwriting engine, immutable policy, SQLite event store, identity resolution, .eml ingestion, Strands Acquisition and Skeptic agents, `CLIModel` (claude/codex/agy) and `ScriptedModel` providers, FastAPI + React dashboard, console/Telegram notifiers, CLI, AgentCore entrypoint (deployed; see deploys/LEDGER.tsv).
- Phase 2: diligence loop with humans in the loop. Skeptic concerns become tracked requests; the first broker message waits for one-tap approval (API or Telegram, both gated); follow-ups on approved threads are automatic (3 days, max 2) and stall with one alert; the Inspector agent reads inspection reports and photos; capex is verified against the document text and aggregated across documents; the deal is re-underwritten on the all-in basis; money talk always waits for approval.
- Phase 3: decision memory. Approvals, rejections with reasons, alert acknowledgements and broker behaviour are recorded (local SQLite by default, AgentCore Memory with write-through when configured), recalled on the deal page and `/memory`, and fed to the skeptic, the credit rationale and the alerts.
- Assurance: two Codex adversarial reviews and a TLA-style audit (docs/STATE_MACHINE.md: state variables, transitions, S1-S12 safety, L1-L5 liveness, 26 findings, all fixed) plus a seeded trace explorer (tests/model; 2000 traces pass). 467 offline tests green; ruff clean.
- Live model runs: acts 1-2 through Claude Sonnet, Gemini Flash and Codex; act 3 with photos through Codex.

## Working Now
- Agent-side memory consumption (skeptic prompt block, credit rationale sentence, "You previously:" alert line).
- Demo video production (kept out of the repo).

## Blockers
- Bedrock tokens-per-day quota on the new account (0, auto-lifts): the deployed runtime answers status invocations; the model path waits.
- No Telegram bot token on this machine: Telegram delivery and inline approval untested live (console notifier and dashboard approval verified).

## Next Three Tasks
1. Finish and review the demo video; submit on Devpost with docs/SUBMISSION.md.
2. When the Bedrock quota lifts, run the two-email story against the deployed runtime and log it in deploys/LEDGER.tsv.
3. If a Telegram token arrives, approve the information request from the phone and capture it for the video.

## Demo Health
Offline three-act story (+ follow-ups, memory): PASS
Live acts 1-2 (claude / agy / codex): PASS
Live act 3 with photos (codex): PASS
Dashboard against live data (incl. memory panels): PASS
Trace explorer 2000 traces: PASS
Telegram: UNTESTED (no token)
AgentCore: DEPLOYED, status path PASS, model path PENDING Bedrock quota
