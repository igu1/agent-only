"""The prompt must not name a tool the agent does not have.

`search_knowledge_base` was listed unconditionally, while the tool itself is
registered only when QDRANT_URL is set AND Qdrant answers. In this deployment
neither holds, so the model was told to ALWAYS search a knowledge library
before answering any factual question, and never to deflect to the admissions
team without searching first - with no such tool on it. It cannot obey either
rule. It answers instead from its own general knowledge (a plausible document
list presented as this school's policy) or deflects anyway.

These tests pin the two halves: advertised when attached, withheld when not,
and never fatal either way.

    python -m unittest discover -s tests -p "test_knowledge_advertisement.py"
"""
import logging
import unittest
from unittest.mock import patch

import core.manager as M


# ── build_agent_knowledge: the decision itself ────────────────────────────

class TestTheKnowledgeDecision(unittest.TestCase):

    def test_no_qdrant_url_means_no_knowledge(self):
        with patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('QDRANT_URL', None)
            self.assertIsNone(M.build_agent_knowledge(agent_id=2))

    def test_no_qdrant_url_is_logged(self):
        import os
        with patch.dict('os.environ', {}, clear=False):
            os.environ.pop('QDRANT_URL', None)
            with self.assertLogs('core.manager', level='WARNING') as log:
                M.build_agent_knowledge(agent_id=2)
        blob = '\n'.join(log.output)
        self.assertIn('QDRANT_URL', blob)
        self.assertIn('search_knowledge_base', blob)

    def test_an_unreachable_qdrant_returns_none_instead_of_raising(self):
        """The real failure: Qdrant refuses the connection while BUILDING the
        client, so the exception arrives from the constructor, not from a
        search. A school losing its documents must not lose its chat."""
        with patch.dict('os.environ', {'QDRANT_URL': 'http://127.0.0.1:1'}):
            with patch('services.rag.build_knowledge',
                       side_effect=ConnectionRefusedError('refused')):
                with self.assertLogs('core.manager', level='WARNING'):
                    self.assertIsNone(M.build_agent_knowledge(agent_id=2))

    def test_an_unreachable_qdrant_is_logged_as_a_fault_not_a_choice(self):
        """A missing URL is a deployment decision; an unreachable server is a
        fault. Reading the same line for both hides the outage."""
        with patch.dict('os.environ', {'QDRANT_URL': 'http://127.0.0.1:1'}):
            with patch('services.rag.build_knowledge',
                       side_effect=ConnectionRefusedError('refused')):
                with self.assertLogs('core.manager', level='WARNING') as log:
                    M.build_agent_knowledge(agent_id=7)
        blob = '\n'.join(log.output)
        self.assertIn('UNREACHABLE', blob)
        self.assertIn('ConnectionRefusedError', blob)
        self.assertIn('7', blob, 'the agent id is what identifies the school')

    def test_a_reachable_qdrant_is_returned_untouched(self):
        sentinel = object()
        with patch.dict('os.environ', {'QDRANT_URL': 'http://qdrant:6333'}):
            with patch('services.rag.build_knowledge', return_value=sentinel) as built:
                self.assertIs(M.build_agent_knowledge(agent_id=3), sentinel)
        built.assert_called_once_with(agent_id=3)

    def test_a_reachable_qdrant_logs_nothing(self):
        with patch.dict('os.environ', {'QDRANT_URL': 'http://qdrant:6333'}):
            with patch('services.rag.build_knowledge', return_value=object()):
                with patch.object(M.logger, 'warning') as warned:
                    M.build_agent_knowledge(agent_id=3)
        warned.assert_not_called()


# ── the prompt ────────────────────────────────────────────────────────────

_EMPTY = ('build_base_guard_instructions', 'build_identity_instructions',
          'build_responsibilities_instructions', 'build_tone_instructions',
          'build_flow_instructions', 'build_class_instructions',
          'build_confirmation_instructions', 'build_sibling_flow_instructions',
          'build_format_guidelines', 'build_escalation_guidelines',
          'build_privacy_instructions', 'build_restrictions_instructions')


class PromptCase(unittest.TestCase):
    """Only the tool list matters here, so everything the profile contributes
    is stubbed to nothing - the business rules are not what is under test."""

    def prompt(self, *, has_knowledge):
        stack = [patch.object(M, name, return_value=[]) for name in _EMPTY]
        stack.append(patch.object(M, 'get_flows', return_value={}))
        stack.append(patch.object(M, 'build_introduction', return_value=''))
        stack.append(patch.object(M, 'build_expected_output', return_value=''))
        for p in stack:
            p.start()
        try:
            cfg = M.build_agent_prompt(profile={'business_name': 'Test School'},
                                       agent_id=2, has_knowledge=has_knowledge)
        finally:
            for p in stack:
                p.stop()
        return '\n'.join(cfg['instructions'])


class TestTheToolIsAdvertisedOnlyWhenAttached(PromptCase):

    def test_advertised_when_the_tool_is_attached(self):
        text = self.prompt(has_knowledge=True)
        self.assertIn('search_knowledge_base', text)
        self.assertIn('ALWAYS search it FIRST', text)

    def test_withheld_when_the_tool_is_not_attached(self):
        text = self.prompt(has_knowledge=False)
        self.assertNotIn('search_knowledge_base', text,
                         'the model is told to call a tool it does not have')

    def test_the_order_to_always_search_goes_with_it(self):
        """The instruction is what does the damage, not the name."""
        self.assertNotIn('ALWAYS search it FIRST', self.prompt(has_knowledge=False))

    def test_absent_means_withheld(self):
        """Fail closed: a caller that says nothing has not proved a tool."""
        stack = [patch.object(M, name, return_value=[]) for name in _EMPTY]
        stack.append(patch.object(M, 'get_flows', return_value={}))
        stack.append(patch.object(M, 'build_introduction', return_value=''))
        stack.append(patch.object(M, 'build_expected_output', return_value=''))
        for p in stack:
            p.start()
        try:
            cfg = M.build_agent_prompt(profile={'business_name': 'X'}, agent_id=2)
        finally:
            for p in stack:
                p.stop()
        self.assertNotIn('search_knowledge_base', '\n'.join(cfg['instructions']))

    def test_the_other_tools_are_unchanged_either_way(self):
        """Only the knowledge line moves. Everything the agent really carries
        must be listed in both states."""
        on, off = self.prompt(has_knowledge=True), self.prompt(has_knowledge=False)
        for tool in ('get_business_locations', 'get_fee_details', 'get_faqs',
                     'get_products', 'get_current_day', 'get_sources',
                     'get_inquiry_status'):
            with self.subTest(tool=tool):
                self.assertIn(tool, on)
                self.assertIn(tool, off)

    def test_the_tool_heading_survives(self):
        self.assertIn('## Available Tools', self.prompt(has_knowledge=False))


# ── the agent still starts ────────────────────────────────────────────────

class TestTheAgentStartsWithoutAKnowledgeBase(unittest.TestCase):
    """Degrading is the whole point: no knowledge base must cost the school
    its chat, its tools or its prompt."""

    def build(self, knowledge):
        seen = {}

        class FakeAgent:
            def __init__(self, **kw):
                seen.update(kw)

        with patch.object(M, 'get_profile', return_value={'business_name': 'S'}), \
             patch.object(M, 'build_agent_knowledge', return_value=knowledge), \
             patch.object(M, 'build_agent_prompt',
                          side_effect=lambda **kw: (seen.update(prompt_kw=kw),
                                                    {'name': 'S', 'instructions': [],
                                                     'description': '', 'introduction': '',
                                                     'expected_output': ''})[1]), \
             patch.object(M, 'get_timezone', return_value='Asia/Riyadh'), \
             patch.object(M, 'get_model', return_value=object()), \
             patch.object(M, 'build_business_tools', return_value=['biz']), \
             patch.object(M, 'build_datetime_tools', return_value=['dt']), \
             patch.object(M, 'Agent', FakeAgent):
            M.create_agent(agent_id=2)
        return seen

    def test_create_agent_does_not_raise_without_knowledge(self):
        self.build(None)              # the assertion is that this returns

    def test_search_knowledge_is_off_without_knowledge(self):
        seen = self.build(None)
        self.assertIsNone(seen['knowledge'])
        self.assertFalse(seen['search_knowledge'])

    def test_the_prompt_is_told_there_is_no_knowledge(self):
        self.assertIs(self.build(None)['prompt_kw']['has_knowledge'], False)

    def test_the_lookup_tools_are_still_attached(self):
        seen = self.build(None)
        self.assertIn('biz', seen['tools'])
        self.assertIn('dt', seen['tools'])

    def test_knowledge_is_attached_and_advertised_when_available(self):
        sentinel = object()
        seen = self.build(sentinel)
        self.assertIs(seen['knowledge'], sentinel)
        self.assertTrue(seen['search_knowledge'])
        self.assertIs(seen['prompt_kw']['has_knowledge'], True)


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
