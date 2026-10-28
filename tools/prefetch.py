# Warm an agent's CRM settings in PARALLEL before it is built.
#
# create_agent reads ~10 settings lists (profile, flow, classes, FAQs,
# knowledge, statuses...) one after another, each a CRM round trip. Measured
# through a Cloudflare tunnel that is ~0.33s apiece - ~6s of a chat's first
# reply spent waiting in line. Every fetcher here goes through cached_query, so
# fetching them all at once simply fills the cache the sequential build then
# reads from: the build itself is untouched, and a failed prefetch only means
# the build fetches that one item itself, exactly as before.
import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor

from tools.getAi import (
    get_active_classes,
    get_active_faqs,
    get_active_flows,
    get_active_institutions,
    get_active_knowledge_base,
    get_active_locations,
    get_active_profile,
    get_flow_nodes,
    get_flow_qa_examples,
    get_inquiry_types,
    get_relations,
)
from tools.getSource import get_sources
from tools.getStatus import get_status

logger = logging.getLogger(__name__)

# Shared, small: the CRM is one backend per org, and the fetches are I/O bound.
_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix='prefetch')


def _run_all(calls) -> list:
    # each worker runs in a COPY of the caller's context - the tenant (org) is a
    # contextvar and is part of every cache key, so without this the warm-up
    # would fill the default tenant's cache instead of this org's
    futures = [_pool.submit(contextvars.copy_context().run, fn) for fn in calls]
    results = []
    for f in futures:
        try:
            results.append(f.result())
        except Exception as e:
            logger.debug('prefetch item failed (the build will fetch it): %s', e)
            results.append(None)
    return results


def prefetch_agent_data(agent_id: int | None) -> None:
    """Fill the settings cache for one agent. Never raises."""
    try:
        flow = _run_all([
            lambda: get_active_flows(agent_id=agent_id),
            lambda: get_active_profile(agent_id=agent_id),
            lambda: get_status(agent_id=agent_id),
            lambda: get_sources(agent_id=agent_id),
            lambda: get_active_classes(agent_id=agent_id),
            lambda: get_inquiry_types(agent_id=agent_id),
            lambda: get_relations(agent_id=agent_id),
            lambda: get_active_locations(agent_id=agent_id),
            lambda: get_active_faqs(agent_id=agent_id),
            lambda: get_active_knowledge_base(agent_id=agent_id),
            lambda: get_active_institutions(agent_id=agent_id),
        ])[0]
        # the flow's nodes and examples are keyed by ITS id, so they can only
        # start once the flow is known - one more parallel round, not two serial
        flow_id = (flow or {}).get('id') if isinstance(flow, dict) else None
        if flow_id is not None:
            _run_all([lambda: get_flow_nodes(flow_id),
                      lambda: get_flow_qa_examples(flow_id)])
    except Exception as e:
        logger.debug('prefetch skipped: %s', e)
