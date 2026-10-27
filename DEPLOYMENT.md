# CronoCRM Agent — Production Deployment Guide

Runbook for the deploy team. The server is assumed to be a Linux host that
already runs other Docker projects — this stack uses its own project name, its
own network and a configurable port, so it will not interfere with them.

**One shared deployment serves every organization.** Onboarding a school is
"append one block to `tenants.json` and reload" — no rebuild, no restart, and
no downtime for the organizations already being served. Design and rationale:
`SAAS-ARCHITECTURE.md`.

## 1. What you are deploying

| Component | Image | Purpose |
|---|---|---|
| Agent service | built from this repo (FastAPI, listens on `AGENT_PORT` — default 5021, the same number inside the container and on the host) | AI admissions chat: web-chat + WhatsApp webhooks, RAG, lead sync |
| Qdrant | `qdrant/qdrant:latest` | Vector database for the AI knowledge, shared by every organization |

Two files configure it, and the split matters:

- **`.env`** — what every organization shares: model ids, embedder, Qdrant
  URL, cache TTLs, the operator token. Nothing customer-specific.
- **`tenants.json`** — one block per organization: its .NET backend URL, its
  agent token, its Gemini key, its Qdrant prefix, its WhatsApp Phone Number
  IDs, its website origins. This is the registry, and it is the only place a
  customer appears.

Each organization keeps its own OnlineCampus .NET API and SQL Server; the
agent is what is shared. Inside Qdrant they are separated by `qdrant_prefix`,
so collections are named `<prefix>-<agent_id>` and one school's documents can
never be searched from another's chat.

External connections per organization:
- That organization's **OnlineCampus .NET API** (`backend_url`) — lead/inquiry
  sync, prompt content.
- **Google Gemini API** — chat + embeddings (outbound HTTPS only).
- **Meta WhatsApp Cloud API** — inbound webhook (needs a public HTTPS URL),
  outbound sends.

The agent also keeps a small SQLite file (`mem/db.sqlite3`) — the model's
working memory, nothing more. Every message is already in `CmpAgentMessage`
and every inquiry in `CmpRegistration` on the org's own SQL Server, so this
file is a CACHE: it needs no backup, and deleting it costs an assistant that
has forgotten conversations nobody has touched recently. Sessions older than
`SESSION_RETENTION_DAYS` (default 7) are swept at startup, and the file is
compacted when a quarter of it has gone empty. Set the variable to 0 to
disable the sweep; raise it if you want longer chat memory.

## 2. Prerequisites

- Linux server with Docker Engine + Compose plugin (`docker compose version`).
- One free TCP port for the shared agent: `AGENT_PORT` in `.env`, default 5021 (what every example below uses).
- `jq` and `openssl` (the onboarding script uses both).
- A public HTTPS domain, proxied by the server's existing nginx — Meta refuses
  non-HTTPS callbacks. **One domain is enough for every organization**, since
  the org is in the URL path; give each org its own domain only if the
  customer wants to see their own hostname.
- Per organization (provided separately, NEVER in git):
  - `backend_url` — that org's .NET API base URL
  - `google_api_key` — its Gemini key (omit to share the stack's key, but then
    it shares the quota and the bill too)
  - Meta WhatsApp Phone Number ID (the access token lives in the org's own
    database, set through the CRM "AI Agent → Channels" tab, never here)
  - the org's website origin(s), for web chat

The org's agent token is **generated during onboarding**, not supplied.

## 3. Build the image once

```bash
git clone <this-repo-url> /opt/cronoagent-src
cd /opt/cronoagent-src
docker build -t cronoagent:v1.0 .
```

(If you use a registry, `docker tag` + `docker push` and reference the
registry path in the compose file instead of the local tag.)

## 4. The stack

Everything lives in one folder, `deploy/saas/`, which ships in this repo:

```
deploy/saas/
├── docker-compose.yml          # agent + qdrant, tenants.json mounted read-only
├── nginx-org.conf.template     # a site per org domain, all to the one agent
├── onboard-org.sh              # append an org + live reload + next steps
├── .env                        # YOU create this, from .env.example
└── tenants.json                # YOU create this, from tenants.example.json
```

```bash
cd /opt/cronoagent-src/deploy/saas
cp ../../.env.example .env              # then fill it in
echo '{}' > tenants.json                # orgs are added in section 5
chmod 600 .env tenants.json
docker compose up -d
curl -s http://localhost:5021/health    # -> {"status":"ok"}
```

`tenants.json` is **volume-mounted, never baked into the image** — see
section 13. Qdrant publishes no host port; only the agent reaches it.

## 5. Onboard an organization

```bash
cd /opt/cronoagent-src/deploy/saas
./onboard-org.sh <org-key> <backend_url> <google_api_key> \
                 <website-origin> [phone_number_id]
```

It generates a fresh `agw_` token, appends the block, reloads the running
agent, and prints the remaining steps. The org key is lowercase, URL-safe and
**permanent**: it appears in every web-chat URL and prefixes that org's chat
memory and Qdrant collections, so renaming it orphans both.

By hand instead — add the block to `tenants.json`, then:

```bash
curl -X POST http://localhost:5021/infra/reload-tenants \
     -H "X-Agent-Token: $AGENT_API_TOKEN"
```

The endpoint is admin-guarded: an org's own token works too (it identifies the
caller's organization), and without `AGENT_API_TOKEN` set the guard stays open
for local development. In production it is set, so the header is required — a
bare `curl` returns 401.

The reload also refreshes the web-chat CORS allow-list, which is built from
every org's `webchat_origins` on each request. An org whose website origin is
listed can chat immediately; one whose origin is missing cannot — and that
failure is invisible from the server: the request arrives, the agent answers,
and the browser discards the reply. Check the origin is in the block before
telling the customer they are live.

## 6. The org's .NET API side

In that organization's OnlineCampus `appsettings.json` (or environment):

```json
"AgentGateway": {
  "AutoFollowup": false,
  "ApiToken": "agw_<the-token-onboard-org.sh-printed>",
  "AgentBaseUrl": "http://<this-server>:5021"
}
```

`ApiToken` must match that org's `api_token` in `tenants.json` exactly. It
authenticates both directions, **and it is how the agent knows which
organization a CRM call belongs to** — which is why every org needs its own.
`AgentBaseUrl` is what the CRM Reindex button calls.

## 7. Public HTTPS and the WhatsApp webhook

Copy `nginx-org.conf.template`, replace the domain, then:

```bash
ln -s /etc/nginx/sites-available/<org>-agent /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d agent.<org-domain>
```

In the org's Meta app (developers.facebook.com → WhatsApp → Configuration):

- Callback URL:
  `https://agent.<domain>/webhooks/<org>/whatsapp/<channelId>/`
  (`channelId` = the WhatsApp row's ChannelID in the CRM AI Agent → Channels tab)
- Verify token: the channel row's VerifyToken
- Subscribe to the **messages** field.

**Use the org-prefixed URL.** Meta's verification GET carries no payload at
all — no phone number, nothing that names an organization — so on the bare
`/webhooks/whatsapp/<id>/` path the agent has nothing to resolve and looks for
the channel in the wrong database. The org in the path settles it for both the
verification GET and the message POST. `phone_number_ids` still resolves the
tenant on a POST, now as a second opinion that also catches a number
configured against another org's URL.

If the handshake fails, `docker compose logs agent` shows the incoming GET.

## 8. The org's web chat

The CRM's chat widget builds its URL from `environment.agentChat` in the
Angular client. Set `orgKey` to the org key from `tenants.json` and rebuild:

```ts
// client/src/environments/environment.ts
agentChat: {
  baseUrl: 'https://agent.<domain>',
  channelId: '1',
  orgKey: '<org-key>'
}
```

Empty `orgKey` produces the plain `/webhooks/webchat/1/...` path, which a
shared agent cannot route — see section 10.

## 9. CRM-side onboarding (the org's admin, not the deploy team)

In the org's CRM → AI Agent page:
1. **Channels** tab: webchat channel row + WhatsApp row (Phone Number ID,
   access token, verify token).
2. **Profile** and **Flow** tabs: one row per institution (seeded rows can be
   edited any time).
3. **FAQs / Knowledge Articles**: add content, then click **Reindex AI**.
4. Send a WhatsApp "Hi" to the org's number and a web-chat message from the
   public inquiry page — both must get AI replies.

## 10. Strict tenancy (`STRICT_TENANCY=true`)

Leave this on. It is what stops "I could not tell which organization this is"
from being answered by quietly picking one.

Without it, the registry falls back to a default tenant built from `.env`.
That is harmless with one customer and wrong the moment two share a stack: a
WhatsApp number missing from `tenants.json` creates that parent's inquiry in
whichever company `.env` points at — no error, no failed request, and the CRM
it landed in looks entirely normal. Nothing surfaces later.

With it on, these are refused with **400** instead:

| Request | Why it could not name an organization |
|---|---|
| `POST /webhooks/webchat/<id>/...` | no `<org>` segment in the URL |
| `POST /webhooks/whatsapp/<id>/` | `phone_number_id` not listed under any org |
| `GET /webhooks/whatsapp/<id>/` (Meta verification) | the verification GET carries no payload at all |
| any Telegram webhook | that channel has no org-prefixed route (unused today) |
| `POST /rag/reindex` with the operator token | it authenticates but names no org — use the org's own `api_token` |

So every channel must use its org-prefixed URL:

```
https://agent.<domain>/webhooks/<org>/webchat/<channel_id>
https://agent.<domain>/webhooks/<org>/whatsapp/<channel_id>/
```

**Before switching it on**, confirm nothing still calls a bare route. With
strict mode OFF the agent logs a warning every time it serves an unidentified
request from the default tenant while other organizations are registered:

```bash
docker compose logs agent | grep "named no organization"
```

An empty result means nothing relies on the fallback and it is safe to turn
on. Afterwards, these two calls confirm it took:

```bash
curl -s -X POST http://localhost:5021/webhooks/webchat/1/session          # 400
curl -s -X POST http://localhost:5021/webhooks/<org>/webchat/1/session    # served
```

## 11. Day-2: updating, health, commands

**Update every organization at once** — there is no list of orgs to keep
current, which was the part of a per-org deployment most likely to go stale
and silently skip a customer:

```bash
cd /opt/cronoagent-src && git pull
docker compose -f deploy/saas/docker-compose.yml build
docker compose -f deploy/saas/docker-compose.yml up -d
```

Vectors live in the Qdrant volume and chat memory in the agent volume, so an
update loses neither. The chat memory is a cache in any case (section 1).

**Health** — one container, so one check:

```bash
curl -sf http://localhost:5021/health \
  || docker compose -f deploy/saas/docker-compose.yml restart agent
```

A per-org check is still worth having, because a healthy container can be
misconfigured for one tenant — an org whose `backend_url` is wrong answers 500
while every other org is fine:

```bash
curl -sf "http://localhost:5021/webhooks/<org>/webchat/1/session" -X POST
```

That returns 404 for an org key missing from the registry, which is the
fastest way to tell "not onboarded" from "onboarded but broken".

```bash
docker compose -f deploy/saas/docker-compose.yml ps
docker stats --no-stream
docker compose -f deploy/saas/docker-compose.yml logs agent --tail 100 -f
docker compose -f deploy/saas/docker-compose.yml restart agent
```

**Scale add-on.** Under load, split the stack into gateway + queue + workers:

```bash
docker compose -f docker-compose.yml -f docker-compose.queue.yml up -d
```

(adds Redis and a worker service; scale with `--scale worker=3`). For a
cluster instead of one server, Kubernetes manifests are in `deploy/k8s/` with
their own README. Keep workers ≤3 until chat memory is moved off SQLite — see
the caveat in that README.

## 12. Troubleshooting

| Symptom | Check |
|---|---|
| org web-chat URL returns 404 | org key missing/misspelled in `tenants.json`, or registry not reloaded (`POST /infra/reload-tenants`) |
| `400 organization could not be determined` | the caller used a bare URL — switch it to `/webhooks/<org>/...` (web chat: set `orgKey`, section 8), or add its `phone_number_id` to that org. For `/rag/reindex`, use the org's own token |
| an org's chats appear in ANOTHER org's CRM | `STRICT_TENANCY` is off and something is calling a bare URL — section 10; `grep "named no organization"` in the log finds the caller |
| web chat loads but every message fails silently in the browser | the org's website origin is missing from its `webchat_origins`; CORS failures leave nothing in the server log |
| CRM Reindex shows red "agent service unreachable" | agent container up? `AgentBaseUrl` right? token matches that org's `api_token`? |
| WhatsApp messages not arriving | Meta webhook URL/verify token; does `docker compose logs agent` show the POST? nginx site enabled? |
| WhatsApp replies not delivered | Meta error 190 in the log = expired access token → paste a new one in CRM → Channels (use a permanent System User token) |
| AI answers "I don't have that information" | content added but **Reindex not clicked**, or Qdrant container down |
| 401 between .NET and agent | that org's `api_token` ≠ its `AgentGateway:ApiToken` |
| (queue overlay) replies never arrive but webhooks return ok | worker container down or unhealthy — `docker ps` should show the worker `(healthy)`; check `docker logs <project>-worker-1` |

## 13. Security rules

- **`tenants.json` never goes INTO the image.** It holds every organization's
  agent token and Gemini key in one file; `.dockerignore` excludes it and the
  compose file mounts it read-only at runtime. Baking it in would ship every
  customer's secrets inside an artifact that gets pushed to a registry.
  `chmod 600` on the server, same as an `.env`.
- **Every organization gets its own `api_token`.** It is not only a password,
  it is the organization's identity on admin calls — reusing one across orgs
  makes them indistinguishable to the agent.
- `.env` never in git (already in `.gitignore`), `chmod 600`. It must contain
  nothing customer-specific; if you are tempted to add an org value there, it
  belongs in `tenants.json`.
- **Customer documents never go into the image either.** `static/*.pdf` is
  excluded: one organization's handbook must not ride along in an image every
  other organization pulls. Upload documents per organization and index them
  into that org's own Qdrant collection.
- Meta access tokens live in each org's own database (managed via the CRM
  Channels tab) — use permanent System User tokens, not 24-hour dev tokens.
- Qdrant has no published port; keep it that way.
- The agent port (`AGENT_PORT`, default 5021) can be firewalled to localhost + the .NET hosts if the
  webhook goes through nginx only.
