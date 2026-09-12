# DealSieve

**A persistent acquisition agent that rejects deals, remembers exactly why, computes what would change its mind, and interrupts you only when that happens.**

Built for the AWS *Agents for Humans* hackathon with the Strands Agents SDK.

> Status: under construction. See [STATUS.md](STATUS.md). Full plan: [docs/DEALSIEVE_PLAN.md](docs/DEALSIEVE_PLAN.md). Module contracts for contributors and coding agents: [docs/CONTRACTS.md](docs/CONTRACTS.md).

## Quick start

```bash
make setup          # python 3.12 venv + deps (uses uv)
cp .env.example .env
make test           # deterministic tests, no model calls
make demo           # inject broker email -> WATCH, inject price drop -> REVIEW + alert
make api            # http://localhost:8000
```
