"""The parallel settings warm-up fills the SAME cache the agent build reads.

    python -m unittest discover -s tests -p "test_prefetch.py"
"""
import unittest
from unittest.mock import patch

import infra.tenants as T
import tools.prefetch as P


class TestPrefetch(unittest.TestCase):

    def setUp(self):
        self.addCleanup(T._current_org.reset, T._current_org.set('acme'))

    def test_every_worker_runs_as_the_callers_organisation(self):
        """The org is part of every cache key. A worker that lost it would warm
        the default tenant's cache, and the build would refetch everything."""
        seen = []

        def record(**kw):
            seen.append(T.get_org())
            return {}
        with patch.multiple(P, get_active_flows=record, get_active_profile=record,
                            get_status=record, get_sources=record,
                            get_active_classes=record, get_inquiry_types=record,
                            get_relations=record, get_active_locations=record,
                            get_active_faqs=record, get_active_knowledge_base=record,
                            get_active_institutions=record):
            P.prefetch_agent_data(2)
        self.assertEqual(len(seen), 11)
        self.assertEqual(set(seen), {'acme'})

    def test_the_flow_details_follow_the_flow(self):
        nodes, examples = [], []
        with patch.multiple(P, get_active_flows=lambda **kw: {'id': 7},
                            get_active_profile=lambda **kw: {}, get_status=lambda **kw: [],
                            get_sources=lambda **kw: [], get_active_classes=lambda **kw: [],
                            get_inquiry_types=lambda **kw: [], get_relations=lambda **kw: [],
                            get_active_locations=lambda **kw: [], get_active_faqs=lambda **kw: [],
                            get_active_knowledge_base=lambda **kw: [],
                            get_active_institutions=lambda **kw: [],
                            get_flow_nodes=nodes.append, get_flow_qa_examples=examples.append):
            P.prefetch_agent_data(2)
        self.assertEqual((nodes, examples), ([7], [7]))

    def test_a_failing_fetch_never_stops_the_build(self):
        def boom(**kw):
            raise RuntimeError('backend down')
        with patch.multiple(P, get_active_flows=boom, get_active_profile=boom,
                            get_status=boom, get_sources=boom, get_active_classes=boom,
                            get_inquiry_types=boom, get_relations=boom,
                            get_active_locations=boom, get_active_faqs=boom,
                            get_active_knowledge_base=boom, get_active_institutions=boom):
            P.prefetch_agent_data(2)       # must not raise


if __name__ == '__main__':
    unittest.main()
