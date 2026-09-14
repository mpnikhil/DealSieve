# DealSieve demo video: script and shot list (under 4:00)

Nine narration blocks, product first, the deal as evidence. Figures are rounded on screen and in the voice-over; the
exact numbers live in the product UI that is on camera. Terms judges may not know are said in plain words: "return"
for cap rate, "the environmental report" for Phase I, "the expense true-up" for CAM reconciliation, "the property-tax
jump at sale" for the Prop 13 reset.

| # | Screen | Voice-over |
|---|---|---|
| 1 | Aerial of a small-bay building (generated) with the wordmark, then the watchlist | I buy long concrete buildings with a row of roll-up doors rented to electricians and cabinet shops. Brokers send a package almost daily. We buy maybe one or two a year. Before passing, I have to fix the broker's numbers to include real vacancy, management costs, and the property-tax jump at sale. I run this tedious math fifty times a quarter. |
| 2 | Title card: "The acquisitions agent for small-bay industrial buyers." with the three jobs | DealSieve handles three jobs. It reads every arriving package and runs the numbers my way. It keeps a price on every pass and recalculates when facts change. It also works the deals that clear by chasing down missing reports. The system only interrupts me when a decision changes. Any message to a broker and anything about money waits for my single tap. |
| 3 | Watchlist (13 deals), then the broker-vs-DealSieve table and the frontier card for one deal | We got a dozen packages this week. Five died on structure. Six are watching with a stored price. One is near the line. Then this one came in. It advertised one point five five million for an eight percent return. My math showed a six point four percent return. DealSieve logged a pass and stored a yes at about one point two nine million. |
| 4 | Inbox: the one-line price cut; watchlist row flips to REVIEW; phone alert with the email draft; Sent folder | Six weeks pass. A one-line email cuts the price to one point two five million. DealSieve recognizes the building. It reruns the math and the deal clears. My first interruption arrives. The alert displays the email to the broker asking for the roof report and the environmental report. It also requests the expense true-up. One tap sends it. |
| 5 | Correspondence panel with the Approve button | This is our core rule. Any message leaving the office to a broker waits for me. Anything about money waits for my single tap. I always see the actual email before it goes. |
| 6 | Roof B-roll (generated), inbox with the report, Documents panel with the three photos, capex rows, phone alert 2, credit request pending | The forty-page inspection report arrives with three photos. DealSieve reads the text and reviews the images. It spots an original roof that is twenty-five years old and ponding water. The report puts the replacement at about ninety thousand, and DealSieve checks that figure against the document before it goes into the math. The deal drops below my line. My second interruption appears. A credit request sits drafted and waiting. |
| 7 | Sent folder with the two follow-ups; Diligence panel with stalled rows; stall alert | The broker still owes us two documents. Automated follow-ups go out on day three and day six. Then the system stops and tells me once. Three interruptions for this entire transaction. |
| 8 | Architecture diagram, slow pan | Under the hood, agents read the emails and reports. They also review the photos. Plain code does every dollar of math against my rules and decides what may go out. We built this on the Strands Agents SDK and deployed on Amazon Bedrock AgentCore. |
| 9 | Impact card (13 / 5 / 6 / 2 / 0 / 3), then the closing shot | I used to spend my evenings reading long PDFs and redoing the math. Now I just make the decisions. It feels good to go home on time. |

## Reproducing the story behind the shots

Offline and deterministic: `make demo-offline`, approve the pending information request in the dashboard (Correspondence
panel) or from the Telegram alert, `python scripts/inject_email.py fixtures/emails/05_inspection_report.eml`, then
`dealsieve followup --as-of 2026-09-17`, `--as-of 2026-09-21`, `--as-of 2026-09-25`. The same story runs live with a
real model (`make demo` with `DEALSIEVE_MODEL_BACKEND=cli`) and with Telegram as the notifier (`DEALSIEVE_NOTIFIER=telegram`
plus a bot token and chat id, and `dealsieve telegram-bot` running for the buttons).
