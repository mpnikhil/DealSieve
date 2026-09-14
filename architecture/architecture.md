# DealSieve architecture

Image: [`architecture.png`](architecture.png), exported from [`overview.html`](overview.html), the system at a glance.
Open the HTML in a browser to watch one deal move through the system; add `?static=1` for the still.
[`diagram.html`](diagram.html) is the node-level flowchart of the same system.
Export again with Playwright: `python architecture/render_diagram.py --page architecture/overview.html --static architecture/architecture.png` (needs Playwright with Chromium).

The overview has four blocks, a memory band, and three promises underneath. The colours are the
boundaries that matter:

- **Amber, reads and judges (Strands agents).** The Acquisition Agent turns a broker email, an
  offering memo or a rent roll into claims, each cited to a page or a photo. The Skeptic Agent argues
  what those claims do not support. The Inspector Agent reads a condition report and its photographs
  and names the capex it finds. All three are Strands `Agent`s over a swappable model provider
  (`CLIModel` for Claude, Gemini or Codex on the command line, `BedrockModel` in AWS, `ScriptedModel`
  for the offline demo). They have tools and judgment. They never do arithmetic that reaches a
  decision, and they cannot send anything.
- **Blue, rules (plain Python, `Decimal`, tested).** Normalize the claims, apply the frozen policy
  (its version is the content hash of the YAML), solve for the price at which every gate would pass,
  classify DEAD, WATCH, NEAR or REVIEW. The Inspector's capex figure is checked against the document
  text before it enters the basis. Every outbound draft passes the same screen: about money, always
  a human; first message in a thread, a human approves; a follow-up on an approved thread, automatic.
  The model can only call these tools and read back what they decided.
- **Green, the human.** The alert carries the actual email. One tap approves it. Nothing about money
  moves without that tap, and nothing reaches a broker without it. Follow-ups go out at day 3 and
  day 6, then the system stops and tells the human once.
- **Ledger and memory.** Events, underwriting runs, evidence and policy versions are append-only in
  SQLite (the `Opportunity` row is the only mutable, derived projection). The investor's past
  decisions live in Amazon Bedrock AgentCore Memory with a local store as the fallback; the alert
  quotes them back ("You previously: passed at $1.29M").

Replies, price cuts and reports return through the inbox and re-run the whole path, which is the
WATCH -> REVIEW spine that `tests/e2e/test_watch_to_review.py` specifies.

Platform: Strands Agents SDK; Amazon Bedrock AgentCore Runtime hosts the same `process_inbound`
pipeline (deployed, us-west-2); Amazon Bedrock is the model backend in AWS; AgentCore Memory and
Amazon SES are adapters behind interfaces, so everything also runs offline.
