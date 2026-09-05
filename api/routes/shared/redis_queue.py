# SaaS Phase 3 (first split): gateway/worker decoupling over Redis.
#
# In QUEUE_MODE=redis the webhook process becomes a thin GATEWAY - it
# validates, resolves the tenant, calls the org backend inbound, then pushes
# a serialized job to Redis and returns immediately. Separate WORKER
# processes (worker.py, scale with --scale worker=N) consume jobs and run
# the AI turns. Without QUEUE_MODE=redis nothing here activates and the
# in-process batcher (message_batcher.py) behaves exactly as before.
#
# Redis keys (all prefixed cq:):
#   cq:parts:{key}   list  - message fragments awaiting the debounce window
#   cq:job:{key}     str   - JSON context to rebuild the handler in a worker
#   cq:pending       zset  - batch keys scored by last-received time
#   cq:claim:{key}   str   - per-chat processing claim (one AI turn at a time)
#   cq:org:{org}     str   - per-org in-flight counter (tenant fairness)
#
# Load-management knobs (env):
#   QUEUE_DEBOUNCE_SECONDS   default 1.5   (merge rapid fragments)
#   TENANT_MAX_CONCURRENT    default 3     (fairness cap per org)
#   WORKER_CONCURRENCY       default 4     (AI turns per worker process)
import asyncio
import json
import logging
import os
import time

log = logging.getLogger(__name__)

_PARTS = 'cq:parts:'
_JOB = 'cq:job:'
_PENDING = 'cq:pending'
_CLAIM = 'cq:claim:'
_ORG = 'cq:org:'


def _debounce() -> float:
    try:
        return float(os.getenv('QUEUE_DEBOUNCE_SECONDS') or 1.5)
    except ValueError:
        return 1.5


def _org_cap() -> int:
    try:
        return int(os.getenv('TENANT_MAX_CONCURRENT') or 3)
    except ValueError:
        return 3


def _client():
    from infra.cache_utils import _get_redis_client
    return _get_redis_client()


def queue_enabled() -> bool:
    return (os.getenv('QUEUE_MODE') or '').strip().lower() == 'redis' and _client() is not None


# ── gateway side ────────────────────────────────────────────────────────────

def enqueue_job(*, key: str, text: str, job: dict) -> None:
    """Push a message fragment + its rebuild context. Re-scoring the pending
    zset on every fragment EXTENDS the debounce window, exactly like the
    in-process batcher."""
    r = _client()
    r.rpush(_PARTS + key, text or '')
    r.set(_JOB + key, json.dumps(job, ensure_ascii=False), ex=3600)
    r.zadd(_PENDING, {key: time.time()})


# ── worker side ─────────────────────────────────────────────────────────────

async def _process(key: str, parts: list[str], job: dict) -> None:
    """Rebuild the handler context (Phase 1 tenant included) and run the turn."""
    from infra.tenants import set_org, get_org, DEFAULT_ORG
    from api.routes.shared.channel_registry import registry
    from api.routes.shared.channel_adapters import IncomingMessage
    from api.routes.shared.msgchannel_processor import handle_agent_batch

    set_org(job.get('org') or DEFAULT_ORG)
    adapter = registry.get(job['channel_type'])
    service = adapter.get_service(job['channel_id'])
    message = IncomingMessage(
        chat_id=str(job['chat_id']),
        user_id=str(job.get('user_id') or job['chat_id']),
        text='',
        message_id=job.get('message_id'),
    )

    async def send_message(text: str) -> None:
        adapter.send_message(service=service, message=message, text=text)

    def escalation_message_getter() -> str:
        try:
            from tools.getAi import get_active_flows
            flow_message = (get_active_flows(agent_id=job.get('agent_id')) or {}).get('escalation_message')
            if flow_message and str(flow_message).strip():
                return str(flow_message).strip()
        except Exception:
            pass
        return "I'm connecting you with our team now. They'll be with you shortly to help you further. 👋"

    org = get_org()
    lead_id = int(job['lead_id'])
    sid = str(lead_id) if org == DEFAULT_ORG else f"{org}:{lead_id}"
    uid = message.user_id if (org == DEFAULT_ORG or not message.user_id) else f"{org}:{message.user_id}"

    await handle_agent_batch(
        batched_text="\n".join(p for p in parts if p),
        user_id=uid,
        session_id=sid,
        lead_id=lead_id,
        channel_agent_id=int(job['agent_id']),
        send_message=send_message,
        escalation_message_getter=escalation_message_getter,
        voice_audio_bytes=None,          # voice turns bypass the queue
        known_phone=job.get('known_phone'),
        channel_type=job.get('channel_type'),
        institution_id=job.get('institution_id'),
    )


async def _run_one(key: str) -> None:
    r = _client()
    org = key.split(':', 1)[0]
    claimed = False
    counted = False
    try:
        # one AI turn at a time per chat: skip (leave pending) if busy
        if not r.set(_CLAIM + key, '1', nx=True, ex=300):
            r.zadd(_PENDING, {key: time.time()})
            return
        claimed = True
        # tenant fairness: cap concurrent turns per org; over cap -> small delay
        if int(r.incr(_ORG + org)) > _org_cap():
            counted = True
            r.zadd(_PENDING, {key: time.time() - _debounce() + 0.7})
            return
        counted = True

        r.zrem(_PENDING, key)
        pipe = r.pipeline()
        pipe.lrange(_PARTS + key, 0, -1)
        pipe.delete(_PARTS + key)
        pipe.get(_JOB + key)
        raw_parts, _, raw_job = pipe.execute()
        parts = [p.decode('utf-8', errors='ignore') if isinstance(p, bytes) else str(p) for p in (raw_parts or [])]
        if not parts or not raw_job:
            return
        job = json.loads(raw_job.decode('utf-8') if isinstance(raw_job, bytes) else raw_job)
        await _process(key, parts, job)
    except Exception:
        log.exception("queue worker error (key=%s)", key)
    finally:
        if counted:
            try:
                r.decr(_ORG + org)
            except Exception:
                pass
        if claimed:
            try:
                r.delete(_CLAIM + key)
            except Exception:
                pass


async def run_worker() -> None:
    """Consumer loop: pick batches whose debounce window has closed."""
    try:
        conc = int(os.getenv('WORKER_CONCURRENCY') or 4)
    except ValueError:
        conc = 4
    sem = asyncio.Semaphore(conc)
    log.info("queue worker up (concurrency=%s, debounce=%ss, org cap=%s)",
             conc, _debounce(), _org_cap())

    async def guarded(key: str) -> None:
        async with sem:
            await _run_one(key)

    while True:
        try:
            # liveness heartbeat for Kubernetes exec probes (worker has no HTTP)
            try:
                with open('/tmp/worker-heartbeat', 'w') as hb:
                    hb.write(str(time.time()))
            except OSError:
                pass
            r = _client()
            if r is None:
                await asyncio.sleep(2)
                continue
            due = r.zrangebyscore(_PENDING, 0, time.time() - _debounce(), start=0, num=8)
            if not due:
                await asyncio.sleep(0.3)
                continue
            tasks = []
            for k in due:
                key = k.decode('utf-8') if isinstance(k, bytes) else str(k)
                tasks.append(asyncio.create_task(guarded(key)))
            if tasks:
                await asyncio.gather(*tasks)
        except Exception:
            log.exception("queue worker loop error")
            await asyncio.sleep(1)
