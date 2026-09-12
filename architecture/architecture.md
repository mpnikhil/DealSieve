# DealSieve architecture

Rendered image: [`architecture.png`](architecture.png) (generated from `architecture.mmd` via
`npx -y @mermaid-js/mermaid-cli -i architecture/architecture.mmd -o architecture/architecture.png -w 1600 -b white`).

Three boundaries are color-coded in the diagram:

- **LLM territory (purple)** — the Strands Acquisition Agent and the independent Skeptic Agent. This
  is where ambiguity lives: reading a messy email or OM and turning it into structured claims, or
  arguing the other side of a deal that already passed every gate. The model provider underneath
  either agent is swappable by environment variable with no code change.
- **Deterministic code (blue)** — the five tools the Acquisition Agent calls. Every dollar of
  arithmetic, every gate comparison, and every guard on *when* a tool is even allowed to act
  (`notify_human` only fires on a real threshold crossing; `draft_broker_questions` only after a
  skeptic report exists) lives here, in tested Python. The model cannot bypass a gate — it can only
  call the tool and read back what the tool decided.
- **Human (orange)** — the only actor who can approve a draft, reject it, or send anything to a
  broker. Nothing crosses this line automatically.

SQLite sits underneath as the immutable record (events and underwriting runs are append-only; the
`Opportunity` row is the only mutable, derived projection of that history). The dashboard and the
Telegram alert are both read/notify surfaces over that same store — neither one is a second source of
truth.

```mermaid
flowchart TD
    subgraph CH["Channels"]
        EM["Email\n(.eml / SES)"]
        TG["Telegram"]
        URLIN["URL paste"]
    end

    IM["InboundMessage\nchannel-agnostic"]
    EM --> IM
    TG --> IM
    URLIN --> IM

    subgraph LLM["LLM territory — Strands Agents SDK"]
        MP["Model provider\nCLIModel · BedrockModel · AnthropicModel · ScriptedModel\n(swapped by env var, zero code change)"]
        AA["Acquisition Agent\nextracts claims with provenance,\nfollows a fixed tool-call procedure"]
        SK["Skeptic Agent\nindependent second opinion,\nstructured_output_model=SkepticOutput"]
        AA -.uses.- MP
        SK -.uses.- MP
    end

    IM --> AA

    subgraph DET["Deterministic code — Python, tested, enforces every gate"]
        RC["record_claims\nidentity resolve + evidence store + reconcile"]
        UW["underwrite\ndeterministic finance engine\n+ immutable policy + viability solver"]
        RSR["request_skeptic_review\n(only when status = REVIEW)"]
        DBQ["draft_broker_questions\n(only after a skeptic report)"]
        NH["notify_human\n(only on a threshold crossing, once)"]
    end

    AA -->|"tool call"| RC --> UW --> RSR
    RSR -->|invokes| SK
    SK -->|verdict + concerns| DBQ --> NH

    DB[("SQLite\nimmutable events · immutable underwriting runs\nderived opportunity state")]
    RC --> DB
    UW --> DB
    RSR --> DB
    DBQ --> DB
    NH --> DB

    subgraph OUT["Interfaces"]
        DASH["Dashboard\nFastAPI + React"]
        ALERT["Telegram alert\n(or console notifier)"]
    end
    DB --> DASH
    NH --> ALERT

    subgraph HUM["Human"]
        H["Investor\nreviews the alert,\napproves or rejects the draft"]
    end
    DASH --> H
    ALERT --> H
    H -.approve / reject.-> DBQ

    classDef llm fill:#efe4ff,stroke:#7c4dff,color:#3d1a8f;
    classDef det fill:#e0f2ff,stroke:#2f7ed8,color:#0d3a66;
    classDef human fill:#fff1d6,stroke:#e08a00,color:#6b4400;
    classDef store fill:#eeeeee,stroke:#666666,color:#222222;

    class MP,AA,SK llm;
    class RC,UW,RSR,DBQ,NH det;
    class H human;
    class DB store;
```
