# DealSieve demo script (≤ 5:00)

Adapted from `docs/DEALSIEVE_PLAN.md` section 29, with the real numbers the working system produces on
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

### 0:20–0:45 — the immutable policy (terminal: `cat config/investment_policy.yaml`, or a slide of the same)

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

### 0:45–1:40 — ambient broker email (terminal: ingest fixture 01, then dashboard overview → deal detail)

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

**Then switch to the dashboard:** overview page — deal #113 sitting in the WATCH row, distance-to-viability bar; click through to the deal detail page and show the Broker vs. DealSieve table (broker 8.13% cap / $126,000 NOI vs. DealSieve 6.42% cap / $99,575 NOI after the tax reset, vacancy floor, management fee and capex reserve) and the Viability Frontier card.

**Voice-over:**
> "A broker email arrives — asking $1.55 million, an 8.13% cap on paper. DealSieve extracts the numbers, resets the property tax, adds a management fee and a capex reserve the broker's pro forma left out, and gets a 6.42% normalized cap, a 1.02 DSCR. WATCH. And it computes something the broker's spreadsheet never will: the exact price where this would pass — $1,285,946, seventeen percent below asking. No one is notified. That's not a bug. That's the product working."

---

### 1:40–2:10 — persistent opinion (dashboard: deal detail, Timeline panel)

**Screen:** the deal-detail Timeline panel, scrolled to the `DEAL_DISCOVERED` / `UNDERWRITING_COMPLETED` events.

**Voice-over:**
> "That opinion doesn't evaporate when the tab closes. It's an immutable event history — the same deal, the same reasoning, sitting quietly, waiting for a fact to change."

---

### 2:10–3:05 — the world changes (terminal: ingest fixture 02, then dashboard)

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

**Voice-over:**
> "Later, same broker thread: 'Seller reduced this to $1.25 million.' DealSieve recognizes it as the same opportunity, records the price change as its own immutable event, and re-underwrites automatically — no one asked it to. 8.27% cap. 1.43x DSCR. Sixty-eight percent LTV. Every gate that was failing now passes. Status flips from WATCH to REVIEW."

---

### 3:05–3:35 — human interruption (terminal / Telegram screenshot)

**Screen:** the console alert box (or a Telegram screenshot if the bot is configured).

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

[Review] [Draft broker questions] [Ignore]
```

**Voice-over:**
> "This is the only interruption in the entire demo. Not a digest, not a daily summary — one message, sent exactly once, because the decision actually changed."

---

### 3:35–4:10 — the skeptic, and a pending draft (dashboard: deal detail, Skeptic + Drafts panels)

**Screen:** the Skeptic panel (concerns: roof age, Phase I environmental, CAM reconciliation, lease rollover concentration — each with severity and evidence status) and the Drafts panel showing the three pending broker questions; click **Approve**.

**Voice-over:**
> "Before anyone gets excited, an independent skeptic agent looks at the same file — specifically because it just passed every gate — and asks what the numbers are built on. No roof age. No Phase I. No CAM reconciliation. It drafts three questions for the broker. Nothing goes out until a human approves it."

---

### 4:10–4:30 — architecture (slide: `architecture/architecture.png`)

**Screen:** the architecture diagram.

**Voice-over:**
> "Email and Telegram feed a Strands acquisition agent. The agent extracts; it never calculates. Every dollar of arithmetic and every workflow gate lives in deterministic Python underneath it, under a policy the agent can't edit. A skeptic agent gets the final say before a human ever sees a request. That's the whole system."

---

### 4:30–5:00 — impact and close (dashboard overview: stat strip)

**Screen:** the overview page's stat strip — encountered / dead / watch / near / review / human interruptions this week (the seeded demo data plus deal #113: 13 opportunities encountered, 5 dead, 6 watch, 1 near, 1 review, 1 human interruption this week).

**Voice-over:**
> "Thirteen opportunities encountered. Five dead on structure. Six still watched. One near the line. One crossed it — and that's the one time anyone was interrupted."

**Close card / final line:**
> "DealSieve doesn't just remember your decisions. It remembers what would cause those decisions to change. It can say: not now. It knows why not. It calculates what would change its mind. And it watches quietly until that happens — only then does it ask for a human's attention."

---

## Caution for whoever records this

Rehearse with `make demo-offline` (scripted model, about 3 seconds, deterministic numbers). Record with `make demo` and a real model backend; `DEALSIEVE_CLI_PROVIDER=agy DEALSIEVE_CLI_MODEL=gemini-3.8-flash-low` is the fastest verified option (1 to 2 minutes per email), Claude Sonnet the most thorough (about 3 minutes per email). Cut the wait in the edit; the agent's tool calls print as they happen. Both targets reset `data/dealsieve.db` first, which is what you want for a clean take.
