#!/bin/bash
# Onboard an organization into the SHARED deployment.
# Usage:
#   ./onboard-org.sh <org> <backend_url> <google_api_key> <website_origin> [phone_number_id]
# Example:
#   ./onboard-org.sh alnoor https://api.alnoor.com AIza... https://alnoor.com 123456789012345
#
# Appends the org to tenants.json (generating a fresh agw_ token), reloads the
# running agent, and prints what still has to be done by hand.
#
# website_origin is REQUIRED, and it is not paperwork: the agent builds its
# web-chat CORS allow-list from every org's webchat_origins. An org missing
# from it cannot be reached from its own site, and that failure is invisible
# server-side - the request arrives, the agent answers, the browser throws the
# reply away. Pass "none" for an org that will only use WhatsApp.
set -euo pipefail
cd "$(dirname "$0")"

ORG=$(echo "${1:?org key required}" | tr '[:upper:]' '[:lower:]')
BACKEND=${2:?backend_url required}
GKEY=${3:?google_api_key required}
ORIGIN=${4:?website_origin required (or the word "none")}
PNID=${5:-}

# Each of these is a literal path segment in an org-prefixed URL
# (/webhooks/<org>/whatsapp/...), so an org with that key makes the route
# ambiguous. "default" is the built-in fallback tenant. A leading underscore
# marks a comment block in tenants.json and is skipped by the loader, so an
# org named that way would silently never load at all.
case "$ORG" in
  webchat|whatsapp|telegram|instagram|default)
    echo "org key '$ORG' is reserved"; exit 1 ;;
  _*)
    echo "org key '$ORG' starts with '_', which marks a comment block"; exit 1 ;;
esac

command -v jq >/dev/null || { echo "jq is required (apt install jq)"; exit 1; }

[ -f tenants.json ] || echo '{}' > tenants.json
jq -e --arg o "$ORG" 'has($o)' tenants.json >/dev/null && { echo "org '$ORG' already exists"; exit 1; }

TOKEN="agw_$(openssl rand -hex 20)"
PNIDS='[]'; [ -n "$PNID" ] && PNIDS="[\"$PNID\"]"
ORIGINS='[]'; [ "$ORIGIN" != "none" ] && ORIGINS="[\"${ORIGIN%/}\"]"

jq --arg o "$ORG" --arg b "${BACKEND%/}" --arg t "$TOKEN" --arg g "$GKEY" \
   --argjson p "$PNIDS" --argjson w "$ORIGINS" \
   '. + {($o): {backend_url:$b, api_token:$t, google_api_key:$g,
                qdrant_prefix:$o, phone_number_ids:$p, webchat_origins:$w}}' \
   tenants.json > tenants.json.tmp && mv tenants.json.tmp tenants.json
chmod 600 tenants.json

# live reload - any registered org token authenticates the admin endpoint
curl -sf -X POST http://localhost:8080/infra/reload-tenants \
     -H "X-Agent-Token: $TOKEN" >/dev/null \
  && echo "agent reloaded" || echo "WARN: reload failed - is the agent up?"

cat <<EOF

Org '$ORG' onboarded. Complete the setup:

1. The org's OnlineCampus appsettings.json - the token is also how the agent
   knows which organization a CRM call is from, so it must be THIS one:
     "AgentGateway": {
       "AutoFollowup": false,
       "ApiToken": "$TOKEN",
       "AgentBaseUrl": "http://<this-server>:8080"
     }

2. The org's Angular client, so its web chat uses the org-prefixed URL:
     agentChat: { baseUrl: 'https://agent.<domain>', channelId: '1',
                  orgKey: '$ORG' }
   Then rebuild the client. Without orgKey the agent cannot tell which
   organization the chat belongs to and refuses it (STRICT_TENANCY).

3. Meta webhook callback URL - use the ORG-PREFIXED form. Meta's verification
   GET carries no payload, so on the bare URL there is nothing to resolve:
     https://agent.<domain>/webhooks/$ORG/whatsapp/<channelId>/
   Phone Number ID: ${PNID:-not set yet - add it to tenants.json when known}

4. nginx (only if this org wants its own hostname): nginx-org.conf.template

5. CRM AI Agent page: Channels / Profile / Flow / FAQs, then Reindex.

6. Verify: a WhatsApp "Hi" to the org's number, and a web-chat message from
   its public inquiry page. Both must get AI replies.
EOF
