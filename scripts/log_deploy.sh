#!/usr/bin/env bash
# Append one immutable row to deploys/LEDGER.tsv. ts_utc and git_sha are filled automatically;
# you pass only the deploy-specific facts.
#
# Usage:
#   scripts/log_deploy.sh <target> <endpoint> <image_digest> <bedrock_model> <result> [notes...]
# Example:
#   scripts/log_deploy.sh agentcore-runtime \
#     arn:aws:bedrock-agentcore:us-west-2:123:runtime/dealsieve-abc \
#     sha256:deadbeef global.anthropic.claude-sonnet-4-6 ok "0.18 USD for the 2-email story"
#
# Use "-" for any field you don't have yet (e.g. digest before the build finishes).
set -euo pipefail

if [ "$#" -lt 5 ]; then
  sed -n '2,12p' "$0"   # print the usage comment above
  exit 2
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
ledger="$repo_root/deploys/LEDGER.tsv"
[ -f "$ledger" ] || { echo "no ledger at $ledger" >&2; exit 1; }

target="$1"; endpoint="$2"; digest="$3"; model="$4"; result="$5"; shift 5
notes="${*:-}"
ts_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
git_sha="$(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null || echo '-')"

# Tabs are literal so the row lines up with the header; notes is last so stray tabs there can't shift columns.
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$ts_utc" "$target" "$endpoint" "$digest" "$git_sha" "$model" "$result" "$notes" >> "$ledger"

echo "logged: $ts_utc  $target  $result"
