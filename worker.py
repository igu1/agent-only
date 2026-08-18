# SaaS Phase 3 - chat worker process (no HTTP). Consumes batches from the
# Redis queue and runs the AI turns. Scale with:
#   docker compose -f docker-compose.yml -f docker-compose.queue.yml up -d --scale worker=3
import logging

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s %(message)s')

if __name__ == '__main__':
    import asyncio
    import api.routes.msgchannels  # noqa: F401  (registers the channel adapters)
    from api.routes.shared.redis_queue import run_worker
    asyncio.run(run_worker())
