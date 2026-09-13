# DealSieve demo script (≤ 5:00)

Adapted from `docs/DEALSIEVE_PLAN.md` section 36, with the real numbers the working system produces on
deal #113, 8330 Power Inn Road, Sacramento. Screens: terminal, dashboard overview, deal detail,
console/Telegram alert, one architecture slide. For a byte-for-byte reproducible recording, run the
ingest commands with `DEALSIEVE_MODEL_BACKEND=scripted` (see the caution at the end) — the scripted
model replays the exact recorded turns with no network calls, so nothing in this script depends on a
live model call working on the day of the recording.

---

### 0:00–0:20 — problem (terminal or title card)

**Screen:** title card or empty terminal.

**Voice-over:**
> "Acquisition professionals don't need another AI that reads offering memorandums faster. They need an agent that decides which opportunities deserve their attention in the first place — and remembers the ones it turned down."

---

### 0:20–0:40 — the immutable policy (terminal: `cat config/investment_policy.yaml`, or a slide of the same)

**Screen:** the policy file, or a slide with the same numbers.

```text
$500,000 equity, $75,000 reserve target
8.0% minimum normalized cap rate
1.35x minimum DSCR
≤25% largest-tenant concentration, minimum 5 tenants
≤75% LTV, 7% / 25-year debt
```

**Voice-over:**
> "This policy is frozen. The agent can read it. It can never change it. Every underwriting run records exactly which version of it produced the result."

---

### 0:40–1:35 — Act 1: ambient broker email (terminal: ingest fixture 01, then dashboard overview → deal detail)

**Command:**
```bash
DEALSIEVE_MODEL_BACKEND=scripted python scripts/inject_email.py fixtures/emails/01_initial_offer.eml
```

**Terminal output to show (real):**
```text
Deal #:         113
Status:         (new) -> WATCH
Normalized cap: 6.42%
DSCR:           1.02x
Max viable:     $1,285,946
Distance:       17.04%
Human notified: no
```

**Then switch to the dashboard:** overview page — deal #113 sitting in the WATCH row, distance-to-viability bar; click through to the deal detail page and show the Broker vs. DealSieve table and the Viability Frontier card.

**Voice-over:**
> "A broker email arrives — asking $1.55 million, an 8.13% cap on paper. DealSieve extracts the numbers, resets the property tax, adds a management fee and a capex reserve the broker's pro forma left out, and gets a 6.42% normalized cap, a 1.02 DSCR. WATCH. And it computes something the broker's spreadsheet never will: the exact price where this would pass — $1,285,946, seventeen percent below asking. No one is notified. That opinion doesn't evaporate; it sits quietly, waiting for a fact to change."

---

### 1:35–2:45 — Act 2: price drop (terminal: ingest fixture 02, then dashboard)

**Command:**
```bash
DEALSIEVE_MODEL_BACKEND=scripted python scripts/inject_email.py fixtures/emails/02_price_drop.eml
```

**Terminal output to show (real):**
```text
Status:         WATCH -> REVIEW
Normalized cap: 8.27%
DSCR:           1.43x
Human notified: yes
```

**Screen:** the console alert box (or a Telegram screenshot).

```text
DEAL #113 JUST BECAME INVESTABLE
8330 Power Inn Road, Sacramento, CA

Price            $1,550,000 -> $1,250,000
Normalized cap   6.42% -> 8.27%   PASS
DSCR             1.02x -> 1.43x   PASS
Largest tenant   19%              PASS

Previously failed solely on valuation.

Still unresolved:
- roof age
- Phase I environmental
- CAM reconciliation
- lease rollover concentration

Awaiting your approval: information request to the broker (3 questions)

[Review] [Approve broker questions] [Ignore]
```

**Then switch to the dashboard:** the Correspondence panel. Shows the pending information request "Awaiting your approval". Click **Approve**.
Run `dealsieve outbox` in the terminal to show `information_request  maya.chen@brokerage.example  Re: Off-market: ...`. Diligence requests Roof age, Phase I environmental, CAM reconciliation move to status sent, due in 3 days.

**Voice-over:**
> "Later, the seller reduces the price to $1.25 million. DealSieve re-underwrites automatically. 8.27% cap. 1.43x DSCR. Every gate passes. Status flips from WATCH to REVIEW. This is the first interruption — one message, because the decision changed. Before anyone gets excited, an independent skeptic agent drafts questions for what the numbers are built on: roof age, Phase I, CAM. Nothing goes out until a human approves it in the Correspondence panel. I tap Approve, and it hits the outbox."

---

### 2:45–3:50 — Act 3: inspection report arrives (terminal: ingest fixture 05, then dashboard)

**Command:**
```bash
DEALSIEVE_MODEL_BACKEND=scripted python scripts/inject_email.py fixtures/emails/05_inspection_report.eml
```

**Terminal output to show (real):**
```text
Deal #:         113
Status:         REVIEW -> NEAR
Normalized cap: 7.71%
DSCR:           1.30x
Max viable:     $1,208,108
Distance:       3.35%
Human notified: yes
```

**Screen:** the second console alert box.

```text
DEAL #113 FELL BACK BELOW THRESHOLD
8330 Power Inn Road, Sacramento, CA

Diligence established:
- Roof age: original built-up membrane installed 2001, no documented replacement
- Roof ponding: NE corner, approx. 1/2 inch after 48 dry hours
- Roof membrane: blistering and an open seam near the suite 105 HVAC curb
Immediate capex added: $90,000

Price basis      $1,250,000 -> $1,340,000
Normalized cap   8.27% -> 7.71%   FAIL
DSCR             1.43x -> 1.30x   FAIL
LTV              68.00% -> 75.20% FAIL

New status: NEAR
Viable below $1,208,108, 3.4% under the ask

Drafted for your approval: request a $42,000 credit.

[Review] [Approve] [Reject]
```

**Then switch to the dashboard:** the Documents panel showing the 3 photo thumbnails and findings, and the $42k credit request awaiting approval. (Do NOT approve it).

**Voice-over:**
> "The broker replies with a 7-page PDF inspection report. An Inspector agent reads the text and three photos: roof ponding, membrane blistering. It extracts $90,000 of immediate capex. DealSieve re-underwrites. Total basis rises. The cap rate drops to 7.71%. The deal falls back below the threshold to NEAR. A second alert fires, drafting a $42,000 credit request. We don't approve it on camera — asking questions is cheap, but money always waits for a human."

---

### 3:50–4:20 — follow-ups and stall (terminal: followup commands, then dashboard)

**Command:**
```bash
dealsieve followup --as-of 2026-09-17
dealsieve followup --as-of 2026-09-21
dealsieve followup --as-of 2026-09-25
```

**Screen:** Diligence panel statuses and the third human alert ("diligence stalled").

**Voice-over:**
> "Meanwhile, the other questions are still out there. We run the followup commands over a few weeks. It sends follow-up 1 for Phase I and CAM. The roof was answered, so it's not re-asked. It sends follow-up 2. By the end of the month with no reply, it marks them stalled and emits the third and final alert. Three human interruptions in the whole story: became investable, fell back below threshold, and diligence stalled."

---

### 4:20–4:40 — architecture (slide: `architecture/architecture.png`)

**Screen:** the architecture diagram.

**Voice-over:**
> "Email and Telegram feed the Acquisition agent and Inspector agent. They extract text and photos; they never calculate. Every dollar of arithmetic, the diligence loop, and the follow-up scheduler lives in deterministic Python underneath it. A skeptic gets the final say before a draft is made. And a human approves every outbound message."

---

### 4:40–5:00 — impact and close (dashboard overview: stat strip)

**Screen:** the overview page's stat strip — 13 encountered, 5 dead, 6 watch, 1 near, 0 review, 3 interruptions this week.

**Voice-over:**
> "Thirteen opportunities encountered. Five dead on structure. Six still watched. One near the line. Zero under review. Three total interruptions."

**Close card / final line:**
> "DealSieve doesn't just remember your decisions. It remembers what would cause those decisions to change. It can say: not now. It knows why not. It calculates what would change its mind. And it watches quietly until that happens — only then does it ask for a human's attention."

---

## Caution for whoever records this

Rehearse with `make demo-offline`, then approve in the dashboard (or via `curl -X POST localhost:8000/api/drafts/<id>/approve`), then the inject and followup commands above, all with `DEALSIEVE_MODEL_BACKEND=scripted` (scripted model, deterministic numbers). Record with `make demo` and a real model backend; `DEALSIEVE_CLI_PROVIDER=agy DEALSIEVE_CLI_MODEL=gemini-3.8-flash-low` is the fastest verified option (1 to 2 minutes per email), Claude Sonnet the most thorough (about 3 minutes per email). Cut the wait in the edit; the agent's tool calls print as they happen. Both targets reset `data/dealsieve.db` first, which is what you want for a clean take.
