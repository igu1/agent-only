import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch

from core.manager import create_agent
from tools.datetime_tools import build_datetime_tools

# Every module that talks to the backend binds `get` and `cached_query` at
# import time, so patching infra.django_api alone would miss them.
_BACKEND_MODULES = ('tools.getAi', 'tools.getStatus', 'tools.getSource')


def _no_backend(*_args, **_kwargs):
    """A failed call. Every fetcher degrades to its empty default on `ok: False`."""
    return {'ok': False}


def _passthrough(_cache_key, fetch, *_args, **_kwargs):
    """Skip both cache layers so the result cannot depend on a warm Redis."""
    return fetch()


def _no_network(*_args, **_kwargs):
    raise AssertionError('test attempted a real network call')


@contextmanager
def offline_backend():
    """Cut every route to the backend and fail loudly if one is left open.

    Without this the test reached for a live Django on an unroutable host:
    build_expected_output pulls statuses, sources and the dropdown lists, and
    build_introduction pulls the flow plus the classes/locations/FAQ/knowledge
    rows the greeting is built from. Mocking core.manager's own get_profile and
    get_flows never covered any of those.
    """
    with ExitStack() as stack:
        for module in _BACKEND_MODULES:
            stack.enter_context(patch(f'{module}.get', _no_backend))
            stack.enter_context(patch(f'{module}.cached_query', _passthrough))
        stack.enter_context(patch('urllib.request.urlopen', _no_network))
        stack.enter_context(patch('infra.django_api._send', _no_network))
        yield


class TestDatetimeTools(unittest.TestCase):
    def test_get_current_day(self):
        # The tool returns an object, we need to call its entrypoint
        (tool,) = build_datetime_tools(timezone='Asia/Riyadh')
        result = tool.entrypoint()
        expected = datetime.now(ZoneInfo('Asia/Riyadh')).strftime("%A")
        self.assertEqual(result, expected)
        self.assertIn(result, ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])

    def test_the_day_follows_the_schools_own_region(self):
        """A group with campuses in different regions gets a different 'today'
        per school - the reason this tool is bound rather than global."""
        for zone in ('Pacific/Kiritimati', 'Pacific/Niue'):
            (tool,) = build_datetime_tools(timezone=zone)
            self.assertEqual(tool.entrypoint(),
                             datetime.now(ZoneInfo(zone)).strftime("%A"))

    def test_no_timezone_falls_back_to_the_deployment_default(self):
        (tool,) = build_datetime_tools()
        self.assertIn(tool.entrypoint(),
                      ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])

    @patch('core.manager.get_profile')
    @patch('core.manager.get_flows')
    @patch('core.manager.get_model')
    @patch('core.manager.Agent')
    def test_create_agent_with_tool(self, mock_agent_class, mock_get_model, mock_get_flows, mock_get_profile):
        # Setup mocks
        mock_get_profile.return_value = {
            'business_name': 'Test Business',
            'business_description': 'Test Description'
        }
        mock_get_flows.return_value = {}
        mock_get_model.return_value = MagicMock()

        # Create agent
        with offline_backend():
            create_agent(agent_id=1)

        # Check if Agent was initialized with correct tools
        args, kwargs = mock_agent_class.call_args
        tools = kwargs.get('tools', [])

        # get_current_day should be in the tools list (built per agent now, so
        # the same tool is a different object each time - match on name)
        self.assertIn('get_current_day', [getattr(t, 'name', None) for t in tools])

    @patch('core.manager.get_profile')
    @patch('core.manager.get_flows')
    @patch('core.manager.get_model')
    @patch('core.manager.Agent')
    def test_agent_is_built_with_a_usable_prompt(self, mock_agent_class, mock_get_model,
                                                 mock_get_flows, mock_get_profile):
        """A school with no data configured must still get a working agent.

        Everything the prompt normally pulls is unavailable here, so this is
        the worst case: it proves the builder degrades instead of producing an
        agent with no instructions or no greeting.
        """
        mock_get_profile.return_value = {'business_name': 'Test Business',
                                         'business_description': 'Test Description'}
        mock_get_flows.return_value = {}
        mock_get_model.return_value = MagicMock()

        with offline_backend():
            create_agent(agent_id=1)

        _, kwargs = mock_agent_class.call_args
        self.assertTrue(kwargs.get('instructions'), 'agent built with no instructions')
        self.assertTrue(kwargs.get('expected_output'), 'agent built with no output schema')
        self.assertTrue(kwargs.get('introduction'), 'agent built with no greeting')
        self.assertEqual(kwargs.get('name'), 'Test Business')

    @patch('core.manager.get_profile')
    @patch('core.manager.get_flows')
    @patch('core.manager.get_model')
    @patch('core.manager.Agent')
    def test_fees_are_answer_only_not_volunteered(self, mock_agent_class, mock_get_model,
                                                  mock_get_flows, mock_get_profile):
        """Giving the class is a flow step, not a fee question.

        The assistant collected parent name, student name and class, then -
        unprompted - opened the fee-category question instead of continuing to
        the next step. Both fee instructions must gate on the parent having
        actually asked.
        """
        mock_get_profile.return_value = {'business_name': 'Test Business'}
        mock_get_flows.return_value = {}
        mock_get_model.return_value = MagicMock()

        with offline_backend():
            create_agent(agent_id=1)

        _, kwargs = mock_agent_class.call_args
        prompt = '\n'.join(kwargs.get('instructions') or [])

        fee_tool = next(l for l in prompt.splitlines() if 'get_fee_details(class_id)' in l)
        self.assertIn('ONLY when the parent has ASKED', fee_tool)
        self.assertIn('is NOT a fee question', fee_tool)

        categories = next(l for l in prompt.splitlines() if 'FEE CATEGORIES rule' in l)
        self.assertIn('ONLY once the parent has asked', categories)
        self.assertIn('never raise categories on your own', categories)


    @patch('core.manager.get_profile')
    @patch('core.manager.get_flows')
    @patch('core.manager.get_model')
    @patch('core.manager.Agent')
    def test_the_real_class_list_is_in_the_conversation(self, mock_agent_class,
                                                        mock_get_model, mock_get_flows,
                                                        mock_get_profile):
        """A school offering Grade 1-6 was told to parents as "Kindergarten
        (Nursery-UKG) and Grades 1-10". The list reached the model only as valid
        ids for a JSON field, which it read as output formatting rather than as
        the vocabulary it may speak."""
        mock_get_profile.return_value = {'business_name': 'Model Girls School'}
        mock_get_flows.return_value = {}
        mock_get_model.return_value = MagicMock()

        classes = [{'id': 44, 'name': 'Grade 1'}, {'id': 45, 'name': 'Grade 2'}]
        with offline_backend(), \
             patch('infra.profile.get_active_classes', lambda **kw: classes):
            create_agent(agent_id=2)

        _, kwargs = mock_agent_class.call_args
        prompt = '\n'.join(kwargs.get('instructions') or [])
        self.assertIn('CLASSES - the complete list this school offers: Grade 1, Grade 2.',
                      prompt)
        self.assertIn('never invent a range', prompt.lower())
        self.assertIn('Do NOT accept it, do NOT set class_id', prompt)

    @patch('core.manager.get_profile')
    @patch('core.manager.get_flows')
    @patch('core.manager.get_model')
    @patch('core.manager.Agent')
    def test_answering_outranks_the_numbered_steps(self, mock_agent_class,
                                                   mock_get_model, mock_get_flows,
                                                   mock_get_profile):
        """"What is the admission process?" was answered with "Could I please
        start by getting your name?" - the numbered steps beat a rule that never
        said it outranked them."""
        mock_get_profile.return_value = {'business_name': 'Test Business'}
        mock_get_flows.return_value = {}
        mock_get_model.return_value = MagicMock()

        with offline_backend():
            create_agent(agent_id=1)

        _, kwargs = mock_agent_class.call_args
        prompt = '\n'.join(kwargs.get('instructions') or [])
        self.assertIn('THIS OUTRANKS THE NUMBERED STEPS', prompt)
        self.assertIn('ASKING ABOUT THE PROCESS IS NOT A REQUEST TO START IT', prompt)


    @patch('core.manager.get_profile')
    @patch('core.manager.get_flows')
    @patch('core.manager.get_model')
    @patch('core.manager.Agent')
    def test_the_sibling_flow_asks_only_child_and_class(self, mock_agent_class,
                                                        mock_get_model, mock_get_flows,
                                                        mock_get_profile):
        """A second child inherits the parent, contacts and verification, so the
        only new facts are the child and the class."""
        mock_get_profile.return_value = {'business_name': 'Test Business'}
        mock_get_flows.return_value = {}
        mock_get_model.return_value = MagicMock()
        with offline_backend():
            create_agent(agent_id=1)
        _, kwargs = mock_agent_class.call_args
        prompt = '\n'.join(kwargs.get('instructions') or [])

        self.assertIn('SECOND CHILD (the sibling flow):', prompt)
        self.assertIn("Ask ONLY two things", prompt)
        self.assertIn('never ask for any of them again', prompt)

    @patch('core.manager.get_profile')
    @patch('core.manager.get_flows')
    @patch('core.manager.get_model')
    @patch('core.manager.Agent')
    def test_reused_contacts_are_announced(self, mock_agent_class, mock_get_model,
                                           mock_get_flows, mock_get_profile):
        """Silently reusing them is why a parent needing a different contact for
        one child had no way to say so."""
        mock_get_profile.return_value = {'business_name': 'Test Business'}
        mock_get_flows.return_value = {}
        mock_get_model.return_value = MagicMock()
        with offline_backend():
            create_agent(agent_id=1)
        _, kwargs = mock_agent_class.call_args
        prompt = '\n'.join(kwargs.get('instructions') or [])

        self.assertIn('SAY that you are reusing them', prompt)
        self.assertIn('same mobile number and email', prompt)

    @patch('core.manager.get_profile')
    @patch('core.manager.get_flows')
    @patch('core.manager.get_model')
    @patch('core.manager.Agent')
    def test_the_summary_is_shown_before_registering(self, mock_agent_class,
                                                     mock_get_model, mock_get_flows,
                                                     mock_get_profile):
        """The OTP notice was the only review any inquiry got, and it only
        appears where the school requires a code - so schools with OTP off,
        and every sibling everywhere, registered unseen."""
        mock_get_profile.return_value = {'business_name': 'Test Business'}
        mock_get_flows.return_value = {}
        mock_get_model.return_value = MagicMock()
        with offline_backend():
            create_agent(agent_id=1)
        _, kwargs = mock_agent_class.call_args
        prompt = '\n'.join(kwargs.get('instructions') or [])

        self.assertIn('CONFIRM BEFORE REGISTERING - this applies to EVERY inquiry:',
                      prompt)
        for field in ('parent', 'student', 'class', 'mobile', 'email'):
            self.assertIn(field, prompt.lower())
        self.assertIn('Shall I register that?', prompt)

    @patch('core.manager.get_profile')
    @patch('core.manager.get_flows')
    @patch('core.manager.get_model')
    @patch('core.manager.Agent')
    def test_the_confirmation_is_reported_only_when_the_parent_gives_it(
            self, mock_agent_class, mock_get_model, mock_get_flows, mock_get_profile):
        """details_confirmed is a REPORT of what the parent said, not a
        judgement about whether the details look complete - the server reads it
        as consent, so a model inferring it registers a child who never
        agreed."""
        mock_get_profile.return_value = {'business_name': 'Test Business'}
        mock_get_flows.return_value = {}
        mock_get_model.return_value = MagicMock()
        with offline_backend():
            create_agent(agent_id=1)
        _, kwargs = mock_agent_class.call_args
        prompt = '\n'.join(kwargs.get('instructions') or [])

        self.assertIn('SET details_confirmed: true ONLY in the turn where the '
                      'parent has just said yes', prompt)
        self.assertIn('never your own judgement that the details look complete',
                      prompt)
        self.assertIn('A CORRECTION IS NOT A CONFIRMATION', prompt)
        self.assertIn('set details_confirmed: false', prompt)
        self.assertIn('Never send a correction and a confirmation in the same turn',
                      prompt)

    @patch('core.manager.get_profile')
    @patch('core.manager.get_flows')
    @patch('core.manager.get_model')
    @patch('core.manager.Agent')
    def test_an_abandoned_confirmation_still_leaves_the_details(self, mock_agent_class,
                                                                mock_get_model,
                                                                mock_get_flows,
                                                                mock_get_profile):
        """A parent who stops replying after the summary is a warm lead, and
        staff can only follow it up if the record has their details - the class
        included. Nothing needs withholding now that the server holds
        creation."""
        mock_get_profile.return_value = {'business_name': 'Test Business'}
        mock_get_flows.return_value = {}
        mock_get_model.return_value = MagicMock()
        with offline_backend():
            create_agent(agent_id=1)
        _, kwargs = mock_agent_class.call_args
        prompt = '\n'.join(kwargs.get('instructions') or [])

        self.assertIn('Keep setting every field as you collect it, including class_id',
                      prompt)
        self.assertIn('stops replying after the summary still leaves their details',
                      prompt)

    @patch('core.manager.get_profile')
    @patch('core.manager.get_flows')
    @patch('core.manager.get_model')
    @patch('core.manager.Agent')
    def test_verification_is_reused_but_a_changed_contact_is_not(self, mock_agent_class,
                                                                 mock_get_model,
                                                                 mock_get_flows,
                                                                 mock_get_profile):
        """Proof belongs to a contact, not to a child: unchanged contacts keep
        their proof, a changed one has none, and a channel-proven mobile is
        never re-verified."""
        mock_get_profile.return_value = {'business_name': 'Test Business'}
        mock_get_flows.return_value = {}
        mock_get_model.return_value = MagicMock()
        with offline_backend():
            create_agent(agent_id=1)
        _, kwargs = mock_agent_class.call_args
        prompt = '\n'.join(kwargs.get('instructions') or [])

        self.assertIn('reused verification covers the reused contacts ONLY', prompt)
        self.assertIn('let the system verify the changed one', prompt)
        self.assertIn('never re-verify that', prompt)


if __name__ == '__main__':
    unittest.main()
