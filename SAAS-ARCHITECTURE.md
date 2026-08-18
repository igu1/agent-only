# CronoCRM Agent — SaaS Architecture Plan

Companion to DEPLOYMENT.md. That runbook describes the CURRENT model
(one container per organization). This document is the roadmap for the
SHARED SaaS model: one deployment serving every organization, split into
services with managed load. Adopt it in phases — each phase is
independently shippable and reversible.

## When to switch (decision thresholds)

Stay on container-per-org until at least one of these is true:
- ~30+ organizations (per-container RAM / update time hurts)
- self-service signup (can't run a script per customer)
- Meta webhook timeouts under load
- a second server is needed

## Phase 1 — Tenant registry + context plumbing (code only) — ✅ IMPLEMENTED

Status: implemented and verified (2026-08-17). The registry lives in
`infra/tenants.py`; `TENANTS_FILE` points at `tenants.json` (git-ignored,
see `tenants.example.json`). With no registry file the agent serves the
single built-in "default" tenant from the classic `.env` vars — existing
per-org deployments behave exactly as before. Org-prefixed webchat routes
(`/webhooks/{org}/webchat/...`), WhatsApp tenant resolution by
`phone_number_id`, token→org resolution on admin endpoints, and
`POST /infra/reload-tenants` are live. Verified: default-route regression,
org-route conversation, unknown-org 404, registry reload.

Goal: the agent stops reading its identity from `.env` and resolves it
PER REQUEST. Ship this while still deploying per-org — behavior identical,
code becomes tenant-ready.

`tenants.json` (one versioned file, all orgs):

```json
{
  "sanabl": {
    "backend_url": "https://api.sanabl.com",
    "api_token": "agw_...",
    "google_api_key": "...",
    "qdrant_prefix": "sanabl",
    "phone_number_ids": ["1277267818798741"],
    "webchat_origins": ["https://sanabl.com"]
  }
}
```

Code changes (all in this repo):

| File | Change |
|---|---|
| new `infra/tenants.py` | registry loader + reload endpoint + token→org and phone_number_id→org lookups |
| webhook routes | org in the path for webchat (`/webhooks/{org}/webchat/{id}`); WhatsApp resolved from payload `phone_number_id` (one shared callback URL for all orgs) |
| entrypoints | set a `contextvars` tenant context before any processing |
| `tools/getAi.py` `_api_get` | backend URL + token from tenant context |
| `infra/config.py` | caches keyed `(org, agent_id)` |
| `core/manager.py` | agent singletons + Gemini key per `(org, agent_id)` |
| `services/rag.py` | Qdrant prefix from tenant context |
| `message_batcher.py` | keys `org:channel:chat` (every org has a "channel 1") |
| chat memory ids | `org:lead` (lead ids collide across org databases) |

MANDATORY exit test: two fake tenants, parallel conversations, assert zero
cross-tenant leakage (config, replies, memory, vectors). Isolation is now
code, not container walls.

## Phase 2 — Collapse to one deployment — ✅ ARTIFACTS READY

Status: deployment artifacts implemented and verified locally (2026-08-17).
`deploy/saas/` contains the shared-deployment kit:
  - `docker-compose.yml` — ONE agent + qdrant, `tenants.json` volume-mounted
  - `nginx-org.conf.template` — per-org domain → the one shared upstream
  - `onboard-org.sh` — append org (fresh token) + live reload + next-steps
Verified: registry mounted live into the running container; org added by
EDITING THE FILE ONLY + `POST /infra/reload-tenants` — no rebuild, no
restart; new org's webchat route served immediately. The dev compose also
mounts the registry now. Adopt in production only at the switch thresholds.

`/deploy/<org>/...` folders retire. One compose: `agent` (single replica at
first) + `qdrant` + `tenants.json` mounted read-only. nginx: all org domains
proxy to the same upstream (or one shared domain — WhatsApp routes by
phone_number_id, webchat by path). Onboarding = append to tenants.json +
reload. update-all.sh becomes one `docker compose up -d`.

## Phase 3 — Service split with load management — ✅ FIRST SPLIT IMPLEMENTED

Status (2026-08-18): the first and largest split — GATEWAY + REDIS QUEUE +
CHAT WORKERS — is implemented and verified. Opt-in via QUEUE_MODE=redis;
without it the agent runs single-service exactly as before.
  - `api/routes/shared/redis_queue.py` — queue, debounce, per-chat claims,
    per-org fairness counters (knobs: QUEUE_DEBOUNCE_SECONDS,
    TENANT_MAX_CONCURRENT, WORKER_CONCURRENCY)
  - `worker.py` — the chat-worker process (same image, `python worker.py`;
    scale with `--scale worker=N`)
  - `docker-compose.queue.yml` — overlay adding redis + worker and flipping
    the agent into gateway mode
  - webchat reply outbox is Redis-backed in queue mode (workers write,
    gateway serves the polling widget)
Verified live: gateway answered in ~300ms while the AI turn ran on the
worker; a 2-message burst merged into ONE turn via the Redis debounce; the
reply reached the widget through the shared outbox. Voice turns bypass the
queue (raw audio bytes) and process in-gateway.
Caveats for multi-worker production: the agno chat-memory sqlite volume is
shared across workers — move memory to Postgres before scaling workers
beyond ~2-3; voice-heavy channels keep load on the gateway.
Remaining sub-phases: notification service, indexing workers, tenant/admin
portal.

The monolith agent splits into services with distinct jobs. THIS is the
load-management architecture:

```
Meta / web widgets
      │
      ▼
[1] GATEWAY  — validate, resolve tenant, enqueue, answer 200 in <100ms
      │
      ▼
[2] REDIS    — queue + 1.5s batching + per-chat locks + per-org counters
      │
      ▼
[3] CHAT WORKERS ×N — one AI turn per job: Gemini, tools, RAG (Qdrant),
      │               lead sync to the org's .NET API
      ├────────► [4] NOTIFICATION service — OTP email, staff WhatsApp,
      │               escalation alerts; own queue, retries, provider limits
      └────────► [6] INDEXING workers — reindex jobs (embedding) off the
                      hot path; CRM button enqueues, progress reported
[5] QDRANT   — unchanged, prefix per tenant
[7] TENANT/ADMIN — registry API, onboarding, per-org usage metering
```

Load management mechanics, stage by stage:

1. **Ingestion is never blocked.** The gateway does no AI work — Meta gets
   its 200 instantly (Meta disables webhooks that respond slowly). Spikes
   become queue depth, not dropped messages.
2. **Batching + ordering move to Redis.** The current in-process debounce
   (1.5s) and per-conversation lock are re-implemented on Redis keys, which
   makes chat workers STATELESS — any worker may take any job, replicas
   become safe.
3. **Workers are the scaling dial.** Each worker processes one AI turn at a
   time per conversation; total throughput = worker count. Scale workers
   only — gateway and notification services stay small.
4. **Per-tenant fairness.** A per-org concurrency cap (e.g. max 3 turns in
   flight per org) enforced via Redis counters: one school's campaign day
   queues its own backlog instead of starving other schools.
5. **Backpressure signals.** Queue depth per org is the health metric —
   alert when a tenant's backlog exceeds N or age exceeds M seconds.
6. **Sends retry independently.** OTP emails / staff WhatsApp / alerts run
   in the notification service with retry + dead-letter — a provider hiccup
   never fails a chat turn.
7. **Heavy embedding is isolated.** A 50-page PDF reindex runs on indexing
   workers; live chat latency is untouched.

Split order (each step pays on its own): gateway+Redis+workers first,
notifications second, indexing third, tenant/admin portal last.

## Phase 4 — Kubernetes — ✅ MANIFEST KIT READY

Status (2026-08-18): complete manifest kit in `deploy/k8s/` — namespace,
secrets/config (tenants.json as a Secret), redis (ephemeral Deployment),
qdrant (StatefulSet + PVC), gateway (2 replicas, HTTP probes, Service),
worker (Deployment + heartbeat exec probe + CPU HPA 2-3), Ingress with
cert-manager, kustomization, and a k3s runbook (README.md). All 13
resources schema-validated with kubeconform. The worker loop writes a
/tmp/worker-heartbeat file so K8s can liveness-probe the non-HTTP worker.
Adopt at the Phase 4 triggers; the same image runs unchanged.

The service layout maps 1:1 to K8s:
Deployment per service, HPA on the chat workers (scale on queue depth or
CPU), Ingress + cert-manager for domains, Secrets for the registry, Qdrant
and Redis as StatefulSets, probes replace healthcheck.sh, rolling updates
replace update-all.sh. Entry point on a single server: k3s. The same Docker
images run unchanged.

## What never changes

Each organization keeps its own OnlineCampus .NET API + SQL database, its
own CRM AI Agent page, its own Meta app and tokens. The SaaS change is
entirely inside the shared AI layer.
