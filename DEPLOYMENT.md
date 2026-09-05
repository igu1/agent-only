# CronoCRM Agent — Production Deployment Guide

Runbook for the deploy team. The server is assumed to be a Linux host that
already runs other Docker projects — this stack uses its own project names,
its own network, and configurable ports, so it will not interfere with them.

## 1. What you are deploying

| Component | Image | Purpose |
|---|---|---|
| Agent service | built from this repo (FastAPI, port 8001 inside) | AI admissions chat: web-chat + WhatsApp webhooks, RAG, lead sync |
| Qdrant | `qdrant/qdrant:latest` | Vector database for the AI knowledge (one instance shared by all organizations) |

External connections per organization:
- The organization's **OnlineCampus .NET API** (`DJANGO_INTERNAL_URL`) — lead/inquiry sync, prompt content.
- **Google Gemini API** — chat + embeddings (outbound HTTPS only).
- **Meta WhatsApp Cloud API** — inbound webhook (needs a public HTTPS URL), outbound sends.

One organization = one agent container + one `.env`. Qdrant is shared;
organizations are separated inside it by `QDRANT_COLLECTION_PREFIX`.

## 1b. Deployment models — pick one before continuing

**Model A — one container per organization (sections 3–11 of this guide).**
Simplest isolation: each org gets its own container, port, and `.env`. No
`tenants.json` is needed — a container without one runs in single-tenant
mode from its `.env` alone. Recommended for the first production org.

**Model B — one shared stack serving every organization (SaaS).**
A single agent stack reads the org registry from `tenants.json`; onboarding
a new school is "append one JSON block + `POST /infra/reload-tenants`" with
no rebuild and no restart. Web chat uses per-org URLs
(`/webhooks/<org>/webchat/...`); WhatsApp routes itself by the Phone Number
ID in the webhook payload. The ready-made kit lives in **`deploy/saas/`**
(compose file, nginx site template, `onboard-org.sh`). Design and rationale:
`SAAS-ARCHITECTURE.md`.

**Scale add-on (works with either model).** Under load, split the stack into
gateway + queue + workers by adding the overlay:
`docker compose -f docker-compose.yml -f docker-compose.queue.yml up -d`
(adds Redis and a worker service; scale with `--scale worker=3`). For a
cluster instead of one server, Kubernetes manifests are in **`deploy/k8s/`**
with their own README. Note: keep workers ≤3 until chat memory is moved off
sqlite (see the caveat in `deploy/k8s/README`).

## 2. Prerequisites

- Linux server with Docker Engine + Compose plugin (`docker compose version`).
- One free TCP port per organization (examples below use 8081, 8082, …).
- A public HTTPS domain per organization for the WhatsApp webhook
  (e.g. `agent.<org-domain>`), proxied by the server's existing nginx —
  Meta refuses non-HTTPS callbacks.
- Credentials per organization (provided separately, NEVER in git):
  - `AGENT_API_TOKEN` — shared secret with that org's .NET API (generate: `openssl rand -hex 20`, prefix `agw_`)
  - `GOOGLE_API_KEY` — Gemini key
  - Meta WhatsApp: Phone Number ID + access token (stored in the org's DB via the CRM "AI Agent → Channels" tab, not in `.env`)

## 3. Folder structure on the server

```
/deploy/
├── qdrant/
│   └── docker-compose.yml          # shared vector DB (started once)
├── <org1>/                         # e.g. sanabl
│   ├── docker-compose.yml
│   └── .env                        # org-specific secrets (chmod 600)
├── <org2>/
│   ├── docker-compose.yml
│   └── .env
├── update-all.sh                   # roll a new image version to every org
└── healthcheck.sh                  # cron: auto-restart a dead agent
```

## 4. Build the image once

```bash
git clone <this-repo-url> /opt/cronoagent-src
cd /opt/cronoagent-src
docker build -t cronoagent:v1.0 .
```

(If you use a registry, `docker tag` + `docker push` and reference the
registry path below instead of the local tag.)

## 5. Shared Qdrant (once)

`/deploy/qdrant/docker-compose.yml`:

```yaml
services:
  qdrant:
    image: qdrant/qdrant:latest
    container_name: qdrant
    restart: unless-stopped
    mem_limit: 1g
    volumes:
      - qdrant_data:/qdrant/storage
    networks: [qdrant-net]
volumes:
  qdrant_data:
networks:
  qdrant-net:
    name: qdrant-net
```

```bash
cd /deploy/qdrant && docker compose up -d
```

No host port is published — only agents on `qdrant-net` can reach it.

## 6. Per-organization stack

`/deploy/<org>/docker-compose.yml` (change the two marked lines per org):

```yaml
services:
  agent:
    image: cronoagent:v1.0
    container_name: <org>-agent            # CHANGE per org
    restart: unless-stopped
    mem_limit: 768m
    ports:
      - "8081:8001"                        # CHANGE per org: 8081, 8082, ...
    env_file: [.env]
    environment:
      - PYTHONPATH=/app/agent
      - QDRANT_URL=http://qdrant:6333
    volumes:
      - agent_logs:/app/agent/logs
      - agent_mem:/app/agent/mem           # chat memory - keep per org
    networks: [qdrant-net]
volumes:
  agent_logs:
  agent_mem:
networks:
  qdrant-net:
    external: true
```

`/deploy/<org>/.env` (template — fill real values, `chmod 600 .env`):

```
AGENT_API_TOKEN=agw_<generate-per-org>
DJANGO_INTERNAL_URL=https://api.<org-domain>      # the org's .NET API base URL
GOOGLE_API_KEY=<org-gemini-key>
AI_MODEL=google
GOOGLE_MODEL_ID=gemini-3-flash-preview
AGNO_EMBEDDER=google
QDRANT_COLLECTION_PREFIX=<org-short-name>
WEBCHAT_ALLOWED_ORIGINS=https://<org-website-domain>
REALTIME_MODE=http
```

Start and verify:

```bash
cd /deploy/<org>
docker compose -p <org> up -d
curl -s http://localhost:8081/health        # -> {"status":"ok"}
```

## 7. The org's .NET API side

In that organization's OnlineCampus `appsettings.json` (or environment):

```json
"AgentGateway": {
  "AutoFollowup": false,
  "ApiToken": "agw_<same-token-as-the-org-.env>",
  "AgentBaseUrl": "http://<server-ip-or-name>:8081"
}
```

`ApiToken` must match the org's `AGENT_API_TOKEN` exactly — it authenticates
both directions. `AgentBaseUrl` is used by the CRM Reindex button.

## 8. Public HTTPS for the WhatsApp webhook

Add a site to the server's existing nginx:

```nginx
server {
    server_name agent.<org-domain>;
    location / {
        proxy_pass http://127.0.0.1:8081;    # the org's agent port
        proxy_set_header Host $host;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/<org>-agent /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d agent.<org-domain>
```

Then in the org's Meta app (developers.facebook.com → WhatsApp → Configuration):

- Callback URL: `https://agent.<org-domain>/webhooks/whatsapp/<channelId>/`
  (`channelId` = the WhatsApp row's ChannelID in the CRM AI Agent → Channels tab)
- Verify token: the channel row's VerifyToken
- Subscribe to the **messages** field.

The verify handshake must return the challenge — check
`docker logs <org>-agent` for the incoming GET if it fails.

## 9. CRM-side onboarding (done by the org's admin, not the deploy team)

In the org's CRM → AI Agent page:
1. **Channels** tab: webchat channel row + WhatsApp row (Phone Number ID,
   access token, verify token).
2. **Profile** and **Flow** tabs: one row per institution (seeded rows can be
   edited any time).
3. **FAQs / Knowledge Articles**: add content, then click **Reindex AI**.
4. Send a WhatsApp "Hi" to the org's number and a web-chat message from the
   public inquiry page — both must get AI replies.

## 10. Updating all organizations

`/deploy/update-all.sh`:

```bash
#!/bin/bash
# usage: ./update-all.sh v1.1   (after building/pushing the new image tag)
VERSION=$1
for org in sanabl alnoor; do            # keep this list current
  cd /deploy/$org
  sed -i "s|cronoagent:.*|cronoagent:$VERSION|" docker-compose.yml
  docker compose -p $org up -d
  echo "$org -> $VERSION"
done
```

Vectors and chat memory live in volumes — updates do not lose them.

## 11. Health monitoring

`/deploy/healthcheck.sh`:

```bash
#!/bin/bash
declare -A ORGS=( [sanabl]=8081 [alnoor]=8082 )   # keep current
for org in "${!ORGS[@]}"; do
  if ! curl -sf http://localhost:${ORGS[$org]}/health > /dev/null; then
    echo "$(date) $org DOWN - restarting" >> /deploy/health.log
    (cd /deploy/$org && docker compose -p $org restart)
  fi
done
```

```bash
chmod +x /deploy/healthcheck.sh
crontab -e     # add:
*/5 * * * * /deploy/healthcheck.sh
```

## 12. Day-2 commands

```bash
docker ps                                   # all orgs at a glance
docker stats --no-stream                    # RAM/CPU per org
docker logs <org>-agent --tail 100 -f       # live log for one org
docker compose -p <org> restart             # bounce one org only
```

## 13. Troubleshooting

| Symptom | Check |
|---|---|
| CRM Reindex shows red "agent service unreachable" | agent container up? `AgentBaseUrl` port right? token match? |
| WhatsApp messages not arriving | Meta webhook URL/verify token; `docker logs` shows the POST? nginx site enabled? |
| WhatsApp replies not delivered | Meta error 190 in logs = expired access token → paste a new one in CRM → Channels (use a permanent System User token in production) |
| AI answers "I don't have that information" | content added but **Reindex not clicked**, or Qdrant container down |
| 401 between .NET and agent | `AGENT_API_TOKEN` ≠ `AgentGateway:ApiToken` |
| (Model B) org web-chat URL returns 404 | org key missing/misspelled in `tenants.json`, or registry not reloaded (`POST /infra/reload-tenants`) |
| (queue overlay) replies never arrive but webhooks return ok | worker container down or unhealthy — `docker ps` should show the worker `(healthy)`; check `docker logs <project>-worker-1` |

## 14. Security rules

- `.env` files: never in git (repo `.gitignore` already excludes them), `chmod 600`, one per org, no shared tokens.
- Meta access tokens live in the org's database (managed via the CRM Channels
  tab) — use permanent System User tokens, not 24-hour dev tokens.
- Qdrant has no published port; keep it that way.
- Agent ports (8081…) can be firewalled to localhost + the .NET host if the
  webhook goes through nginx only.
