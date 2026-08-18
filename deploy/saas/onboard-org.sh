#!/bin/bash
# SaaS Phase 2 - onboard an organization into the SHARED deployment.
# Usage:
#   ./onboard-org.sh <org> <backend_url> <google_api_key> [phone_number_id]
# Example:
#   ./onboard-org.sh alnoor https://api.alnoor.com AIza... 123456789012345
#
# Appends the org to tenants.json (generating a fresh agw_ token), reloads
# the running agent, and prints what the org's .NET appsettings needs.
set -euo pipefail
cd "$(dirname "$0")"

ORG=$(echo "${1:?org key required}" | tr '[:upper:]' '[:lower:]')
BACKEND=${2:?backend_url required}
GKEY=${3:?google_api_key required}
PNID=${4:-}

command -v jq >/dev/null || { echo "jq is required (apt install jq)"; exit 1; }
[ "$ORG" = "webchat" ] && { echo "org key 'webchat' is reserved"; exit 1; }
[ -f tenants.json ] || echo '{}' > tenants.json
jq -e --arg o "$ORG" 'has($o)' tenants.json >/dev/null && { echo "org '$ORG' already exists"; exit 1; }

TOKEN="agw_$(openssl rand -hex 20)"
PNIDS='[]'; [ -n "$PNID" ] && PNIDS="[\"$PNID\"]"

jq --arg o "$ORG" --arg b "${BACKEND%/}" --arg t "$TOKEN" --arg g "$GKEY" \
   --argjson p "$PNIDS" \
   '. + {($o): {backend_url:$b, api_token:$t, google_api_key:$g,
                qdrant_prefix:$o, phone_number_ids:$p, webchat_origins:[]}}' \
   tenants.json > tenants.json.tmp && mv tenants.json.tmp tenants.json
chmod 600 tenants.json

# live reload - any registered org token authenticates the admin endpoint
curl -sf -X POST http://localhost:8080/infra/reload-tenants \
     -H "X-Agent-Token: $TOKEN" >/dev/null \
  && echo "agent reloaded" || echo "WARN: reload failed - is the agent up?"

cat <<EOF

Org '$ORG' onboarded. Complete the setup:

1. The org's OnlineCampus appsettings.json:
     "AgentGateway": {
       "AutoFollowup": false,
       "ApiToken": "$TOKEN",
       "AgentBaseUrl": "http://<this-server>:8080"
     }
2. nginx (optional per-org domain): copy nginx-org.conf.template
3. Web chat widget on the org site posts to: /webhooks/$ORG/webchat/<channelId>
4. Meta webhook: shared callback URL works - just add the org's
   phone_number_id to tenants.json (done: ${PNID:-'none yet - edit tenants.json when known'})
5. CRM AI Agent page: Channels / Profile / Flow / FAQs + Reindex
EOF
