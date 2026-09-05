"""Deterministic tests for one AI turn.

No network, no model, no API key: a stub agent stands in for agno and canned
backend responses stand in for Django, so the whole suite runs in under a
second. What is under test is the part that used to be guesswork - which
STAGE the conversation is in, what the model is told, which messages actually
reach the parent, and what goes into history.

    python -m unittest discover -s tests -p "test_turn_stages.py"
"""
import unittest
from unittest.mock import patch

import api.routes.shared.msgchannel_processor as MP
from api.routes.shared.msgchannel_processor import (
    STAGE_AWAITING_VERIFICATION,
    STAGE_COLLECTING,
    STAGE_SUBMITTED,
    compute_stage,
)

REPLY = '{"message":"Which class is your child applying for?","status":1,"source":1}'


class StubAgent:
    """Records what it was asked and what was written to its session."""

    id = 'stub'
    name = 'stub'

    class model:
        assistant_message_role = 'assistant'

    def __init__(self, *, stage=STAGE_COLLECTING, existing_session=True, content=REPLY,
                 institution=None):
        self._stage = stage
        self._existing = existing_session
        self._content = content
        self._institution = institution
        self.prompts: list[str] = []
        self.saved_state: dict = {}

    def get_session(self, session_id=None, user_id=None):
        return object() if self._existing else None

    def get_session_state(self, session_id=None):
        state = {}
        if self._stage:
            state['admission_stage'] = self._stage
        if self._institution is not None:
            state['institution_id'] = self._institution
        # what update_session_state already wrote wins, like a real session
        state.update(self.saved_state)
        return state

    def update_session_state(self, updates, session_id=None):
        self.saved_state.update(updates)
        return session_id

    def run(self, text, **kwargs):
        self.prompts.append(text)
        return type('RunOutput', (), {'content': self._content})()

    def get_session_summary(self, session_id=None):
        return None


class TurnHarness(unittest.IsolatedAsyncioTestCase):
    """Runs one turn with every outbound edge captured."""

    async def run_turn(self, *, agent=None, update_resp=None, channel_type='whatsapp',
                       text='hi', introduction='Hi! I am the admissions assistant.',
                       institution_id=None, serving_agent=None):
        agent = agent or StubAgent()
        self.intro_kwargs: dict = {}
        # every agent id the turn asked for, in order - the last one serves it
        self.agent_ids: list = []
        self.sent: list[str] = []
        self.crm: list[str] = []
        self.remembered: list[str] = []
        self.lead_updates: list[dict] = []

        def record_update(**kw):
            self.lead_updates.append(kw['payload'])
            return update_resp or {}

        async def send(t):
            self.sent.append(t)

        def take_agent(agent_id):
            self.agent_ids.append(agent_id)
            return agent

        with patch.object(MP, 'get_agent', take_agent), \
             patch('infra.profile.resolve_institution_agent_id',
                   lambda iid, **kw: serving_agent), \
             patch('infra.profile.get_institution_name',
                   lambda iid, **kw: {1: 'Sanabl Al', 3: 'Green Valley College'}.get(int(iid), '')), \
             patch.object(MP, 'save_message', lambda **kw: self.crm.append(kw['content'])), \
             patch.object(MP, 'update_lead_ai_summary', lambda **kw: False), \
             patch.object(MP, 'update_lead_from_payload', record_update), \
             patch.object(MP, '_remember_server_message',
                          lambda a, s, t: self.remembered.append(t)), \
             patch('tools.getAi.get_active_profile', lambda **kw: {'timezone': 'Asia/Riyadh'}), \
             patch('infra.profile.get_institution_name',
                   lambda iid, **kw: {3: 'Green Valley College'}.get(int(iid), '')),              patch('infra.profile.build_introduction',
                   lambda **kw: (self.intro_kwargs.update(kw), introduction)[1]):
            await MP.handle_agent_batch(
                batched_text=text,
                user_id='u1',
                session_id='s1',
                lead_id=7,
                channel_agent_id=1,
                send_message=send,
                escalation_message_getter=lambda: 'escalating',
                channel_type=channel_type,
                institution_id=institution_id,
            )
        self.agent = agent
        return agent


class TestComputeStage(unittest.TestCase):
    """The backend's flags are the only input; nothing is inferred."""

    def test_nothing_known_yet_is_collecting(self):
        self.assertEqual(compute_stage({}), STAGE_COLLECTING)

    def test_outstanding_code_is_awaiting_verification(self):
        self.assertEqual(compute_stage({'otp_required': True}), STAGE_AWAITING_VERIFICATION)

    def test_accepted_code_is_submitted(self):
        self.assertEqual(compute_stage({'otp_verified_now': True}), STAGE_SUBMITTED)

    def test_registration_id_is_submitted(self):
        self.assertEqual(compute_stage({'registration_id': 55}), STAGE_SUBMITTED)

    def test_linked_to_existing_inquiry_is_submitted(self):
        self.assertEqual(compute_stage({'already_registered': True}), STAGE_SUBMITTED)

    def test_a_turn_with_no_lead_update_keeps_the_stage(self):
        self.assertEqual(
            compute_stage({}, previous=STAGE_AWAITING_VERIFICATION),
            STAGE_AWAITING_VERIFICATION,
        )

    def test_submitted_is_terminal(self):
        for later in ({}, {'otp_required': True}):
            with self.subTest(later=later):
                self.assertEqual(
                    compute_stage(later, previous=STAGE_SUBMITTED), STAGE_SUBMITTED
                )

    def test_verification_supersedes_collecting(self):
        self.assertEqual(
            compute_stage({'otp_required': True}, previous=STAGE_COLLECTING),
            STAGE_AWAITING_VERIFICATION,
        )


class TestStageReachesTheModel(TurnHarness):
    """The stage is handed over as a fact, not left to be worked out."""

    async def test_awaiting_verification_tells_the_model_it_is_not_submitted(self):
        await self.run_turn(agent=StubAgent(stage=STAGE_AWAITING_VERIFICATION))
        prompt = self.agent.prompts[0]
        self.assertIn('STAGE: awaiting_verification', prompt)
        self.assertIn('NOT submitted', prompt)
        self.assertIn('may still correct ANY detail', prompt)

    async def test_submitted_tells_the_model_to_escalate_changes(self):
        await self.run_turn(agent=StubAgent(stage=STAGE_SUBMITTED))
        self.assertIn('STAGE: submitted', self.agent.prompts[0])

    async def test_a_fresh_conversation_starts_collecting(self):
        await self.run_turn(agent=StubAgent(stage=None))
        self.assertIn('STAGE: collecting', self.agent.prompts[0])

    async def test_exactly_one_stage_line_per_turn(self):
        await self.run_turn(agent=StubAgent(stage=STAGE_AWAITING_VERIFICATION))
        self.assertEqual(self.agent.prompts[0].count('STAGE: '), 1)


class TestStageIsPersisted(TurnHarness):
    """What the backend just said is stored for the next turn."""

    async def test_issuing_a_code_advances_the_stage(self):
        await self.run_turn(
            agent=StubAgent(stage=STAGE_COLLECTING),
            update_resp={'otp_required': True, 'otp_notice': 'new', 'otp_target': 'phone'},
        )
        self.assertEqual(self.agent.saved_state.get('admission_stage'),
                         STAGE_AWAITING_VERIFICATION)

    async def test_accepting_a_code_advances_to_submitted(self):
        await self.run_turn(
            agent=StubAgent(stage=STAGE_AWAITING_VERIFICATION),
            update_resp={'otp_verified_now': True, 'registration_id': 9},
        )
        self.assertEqual(self.agent.saved_state.get('admission_stage'), STAGE_SUBMITTED)

    async def test_an_unchanged_stage_is_not_rewritten(self):
        await self.run_turn(agent=StubAgent(stage=STAGE_COLLECTING), update_resp={})
        self.assertEqual(self.agent.saved_state, {})


class TestServerMessagesEnterHistory(TurnHarness):
    """The regression behind the did-you-mean loop and the skipped OTP."""

    async def test_did_you_mean_email_is_remembered(self):
        await self.run_turn(update_resp={'email_typo_suggestion': 'a@gmail.com'})
        self.assertTrue(any('did you mean a@gmail.com' in t for t in self.remembered))

    async def test_verification_notice_is_remembered(self):
        await self.run_turn(update_resp={
            'otp_required': True, 'otp_notice': 'new', 'otp_target': 'phone',
        })
        self.assertTrue(any('verification code' in t for t in self.remembered))

    async def test_read_only_refusal_is_remembered(self):
        await self.run_turn(update_resp={'edit_refused': True})
        self.assertTrue(any('already been submitted' in t for t in self.remembered))

    async def test_the_models_own_reply_is_not_re_recorded(self):
        await self.run_turn(update_resp={})
        self.assertEqual(self.sent, ['Which class is your child applying for?'])
        self.assertEqual(self.remembered, [])


class TestWhichMessageReachesTheParent(TurnHarness):
    """Turns where the model may not know the verdict send the server's."""

    async def test_typo_turn_suppresses_the_models_guess(self):
        await self.run_turn(update_resp={'email_typo_suggestion': 'a@gmail.com'})
        self.assertEqual(len(self.sent), 1)
        self.assertIn('looks misspelled', self.sent[0])

    async def test_refused_edit_sends_only_the_server_verdict(self):
        await self.run_turn(update_resp={'edit_refused': True})
        self.assertEqual(len(self.sent), 1)
        self.assertIn("can't be changed here", self.sent[0])

    async def test_success_is_confirmed_before_the_models_follow_up(self):
        await self.run_turn(update_resp={'otp_verified_now': True, 'registration_id': 9})
        self.assertEqual(len(self.sent), 2)
        self.assertIn('Verification successful', self.sent[0])
        self.assertIn('Which class', self.sent[1])

    async def test_a_wrong_code_asks_again_and_says_how_many_tries_are_left(self):
        await self.run_turn(update_resp={
            'otp_required': True, 'otp_notice': 'retry',
            'otp_target': 'phone', 'otp_attempts_left': 2,
        })
        self.assertIn("didn't match", self.sent[0])
        self.assertIn('2 attempts left', self.sent[0])

    async def test_an_ordinary_turn_sends_the_models_reply(self):
        await self.run_turn(update_resp={})
        self.assertEqual(self.sent, ['Which class is your child applying for?'])

    async def test_every_delivered_message_is_stored_in_the_crm(self):
        await self.run_turn(update_resp={'otp_verified_now': True, 'registration_id': 9})
        self.assertEqual(self.sent, self.crm)


class TestNewChatGreeting(TurnHarness):
    """Greeted once, on the channel's own idea of where a chat starts."""

    async def test_a_new_messaging_chat_is_greeted_before_the_reply(self):
        await self.run_turn(agent=StubAgent(existing_session=False), channel_type='whatsapp')
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[0].startswith('Hi!'))

    async def test_a_returning_chat_is_not_greeted_again(self):
        await self.run_turn(agent=StubAgent(existing_session=True), channel_type='whatsapp')
        self.assertEqual(len(self.sent), 1)

    async def test_webchat_is_not_greeted_here_the_widget_did_it_on_open(self):
        await self.run_turn(agent=StubAgent(existing_session=False), channel_type='webchat')
        self.assertEqual(len(self.sent), 1)


class TestInquiryProvenance(TurnHarness):
    """An inquiry must be identifiable as agent-made, deterministically.

    `source` cannot do it: the model picks it every turn. Staff need to filter
    and report on what the AI actually captured.
    """

    EXTRACTED = ('{"message":"Noted.","status":1,"source":1,'
                 '"lead":{"name":"Varun","student_name":"Ashiq"}}')

    async def test_captured_details_are_marked_as_agent_made(self):
        await self.run_turn(agent=StubAgent(content=self.EXTRACTED))
        self.assertEqual(len(self.lead_updates), 1)
        sent = self.lead_updates[0]
        self.assertEqual(sent['inquiry_source'], 'ai_agent')
        self.assertEqual(sent['inquiry_agent_id'], 1)

    async def test_the_channel_is_recorded_with_it(self):
        for channel in ('whatsapp', 'webchat', 'telegram'):
            with self.subTest(channel=channel):
                await self.run_turn(agent=StubAgent(content=self.EXTRACTED),
                                    channel_type=channel)
                self.assertEqual(self.lead_updates[0]['inquiry_channel'], channel)

    async def test_provenance_never_travels_alone(self):
        """A turn that extracted nothing must still make no backend call."""
        await self.run_turn(agent=StubAgent(content='{"message":"Hi there."}'))
        self.assertEqual(self.lead_updates, [])

    async def test_the_extracted_fields_still_get_through(self):
        await self.run_turn(agent=StubAgent(content=self.EXTRACTED))
        sent = self.lead_updates[0]
        self.assertEqual(sent['name'], 'Varun')
        self.assertEqual(sent['student_name'], 'Ashiq')
        self.assertEqual(sent['status'], 1)


class TestPromptStateBoundary(unittest.TestCase):
    """Backend decides state. Prompt decides language and behaviour.

    Nothing else in the suite reads the prompt, so without these a state rule
    can quietly be written back into the prose, drift from what the backend
    reports, and put the two in conflict again. These are the ratchet.
    """

    RULES = staticmethod(
        lambda: [r for r in __import__('infra.profile', fromlist=['x'])
                 .build_responsibilities_base_instructions() if r.startswith('-')]
    )

    def test_every_stage_has_exactly_one_context_line(self):
        from api.routes.shared.msgchannel_processor import STAGE_CONTEXT
        self.assertEqual(
            set(STAGE_CONTEXT),
            {STAGE_COLLECTING, STAGE_AWAITING_VERIFICATION, STAGE_SUBMITTED},
        )
        for stage, line in STAGE_CONTEXT.items():
            with self.subTest(stage=stage):
                self.assertTrue(line.startswith(f'STAGE: {stage}'))

    def test_submission_state_is_declared_only_by_the_stage_line(self):
        """The prompt must not re-declare what STAGE already states.

        Rules like "AFTER the inquiry is submitted, NO data can be changed"
        duplicated the submitted STAGE line; a pending code being read as a
        submission is exactly the bug that produced "your details have already
        been submitted" while verification was still outstanding.
        """
        # the DECLARATIONS STAGE owns - not any mention of submission, which a
        # behavioural rule may legitimately make when naming its scope
        banned = (
            'no data can be changed',
            'is not a submitted inquiry',
            "inquiry is 'registered' or 'submitted'",
        )
        offenders = [
            (phrase, rule[:70])
            for rule in self.RULES()
            for phrase in banned
            if phrase in rule.lower()
        ]
        self.assertEqual(offenders, [], f'state prose is back in the prompt: {offenders}')

    def test_responsibilities_stay_within_budget(self):
        """An anti-accretion ratchet, not a style rule.

        Every past bug was answered by appending another long rule until the
        block reached 43 rules / 12k chars and started contradicting itself.
        Raise these numbers deliberately, never by reflex.
        """
        rules = self.RULES()
        self.assertLessEqual(len(rules), 30, 'rule count is creeping back up')
        self.assertLessEqual(sum(len(r) for r in rules), 9000, 'prompt is growing again')


if __name__ == '__main__':
    unittest.main()


class TestInstitutionIsSettledBeforeTheChat(TurnHarness):
    """The college is picked on the inquiry page, so the assistant is TOLD
    which one it serves and never spends a turn asking."""

    async def test_the_pages_choice_reaches_the_model_as_a_fact(self):
        await self.run_turn(institution_id=3)
        prompt = self.agent.prompts[0]
        self.assertIn('ROUTING:', prompt)
        self.assertIn('institution_id 3 (Green Valley College)', prompt)
        self.assertIn('NEVER ask which institution', prompt)

    async def test_no_choice_means_no_routing_line(self):
        await self.run_turn()
        self.assertNotIn('ROUTING:', self.agent.prompts[0])

    async def test_exactly_one_routing_line_per_turn(self):
        await self.run_turn(institution_id=3)
        self.assertEqual(self.agent.prompts[0].count('ROUTING:'), 1)

    async def test_the_choice_is_stored_on_the_session(self):
        await self.run_turn(institution_id=3)
        self.assertEqual(self.agent.saved_state.get('institution_id'), 3)

    async def test_a_stored_choice_survives_a_message_that_omits_it(self):
        await self.run_turn(agent=StubAgent(institution=3))
        self.assertIn('institution_id 3', self.agent.prompts[0])

    async def test_a_stored_choice_is_not_rewritten(self):
        await self.run_turn(agent=StubAgent(institution=3), institution_id=3)
        self.assertNotIn('institution_id', self.agent.saved_state)

    async def test_an_unknown_id_still_routes(self):
        """A college the name lookup cannot resolve is still settled - the
        chat must not fall back to asking."""
        await self.run_turn(institution_id=8)
        self.assertIn('institution_id 8.', self.agent.prompts[0])

    async def test_the_new_chat_greeting_is_scoped_to_the_college(self):
        await self.run_turn(agent=StubAgent(existing_session=False), institution_id=3)
        self.assertEqual(self.intro_kwargs.get('institution_id'), 3)


class TestTheInquiryIsFiledAgainstTheRoutedCollege(TurnHarness):
    """institution_id on the record comes from the page, never from the model."""

    async def test_the_routed_college_is_written_to_the_lead(self):
        await self.run_turn(institution_id=3, update_resp={})
        self.assertEqual(self.lead_updates[0].get('institution_id'), 3)

    async def test_a_model_guess_cannot_override_the_page(self):
        guess = '{"message":"ok","status":1,"source":1,"institution_id":99}'
        await self.run_turn(agent=StubAgent(content=guess), institution_id=3)
        self.assertEqual(self.lead_updates[0].get('institution_id'), 3)

    async def test_without_routing_the_model_still_supplies_it(self):
        guess = '{"message":"ok","status":1,"source":1,"institution_id":99}'
        await self.run_turn(agent=StubAgent(content=guess))
        self.assertEqual(self.lead_updates[0].get('institution_id'), 99)


class TestTheSchoolsOwnAgentServesTheTurn(TurnHarness):
    """A routed conversation is answered by the school's agent, not the group's.

    The company agent's FAQs, classes and knowledge base are the pooled union
    across every school, so serving a routed chat from it leaks one campus's
    content into another's conversation.
    """

    async def test_an_unrouted_turn_stays_on_the_channels_agent(self):
        await self.run_turn()
        self.assertEqual(self.agent_ids, [1])

    async def test_a_routed_turn_switches_to_the_schools_agent(self):
        await self.run_turn(institution_id=3, serving_agent=7)
        self.assertEqual(self.agent_ids[0], 1)      # channel agent, to read state
        self.assertEqual(self.agent_ids[-1], 7)     # school agent serves it

    async def test_an_unresolvable_school_keeps_the_channels_agent(self):
        """No confident match - stay put rather than serve the wrong content."""
        await self.run_turn(institution_id=3, serving_agent=None)
        self.assertEqual(self.agent_ids, [1])

    async def test_a_school_that_is_already_the_channels_agent_is_not_reloaded(self):
        await self.run_turn(institution_id=1, serving_agent=1)
        self.assertEqual(self.agent_ids, [1])

    async def test_the_greeting_comes_from_the_serving_agent(self):
        await self.run_turn(agent=StubAgent(existing_session=False),
                            institution_id=3, serving_agent=7)
        self.assertEqual(self.intro_kwargs.get('agent_id'), 7)

    async def test_routing_still_reaches_the_model_after_the_switch(self):
        await self.run_turn(institution_id=3, serving_agent=7)
        self.assertIn('ROUTING:', self.agent.prompts[0])


class TestAChannelWithNoPickerRemembersTheAnswer(TurnHarness):
    """A company-level WhatsApp number has no page to choose on, so the
    assistant asks. Once the parent answers, that answer must ROUTE the rest of
    the conversation - not just reach the CRM and be re-derived from history
    every turn afterwards."""

    async def test_the_models_answer_is_stored_for_the_next_turn(self):
        reply = '{"message":"ok","status":1,"source":1,"institution_id":3}'
        await self.run_turn(agent=StubAgent(content=reply))
        self.assertEqual(self.agent.saved_state.get('institution_id'), 3)

    async def test_an_invented_school_is_never_adopted(self):
        """A id the backend does not know must not route anything."""
        reply = '{"message":"ok","status":1,"source":1,"institution_id":99}'
        await self.run_turn(agent=StubAgent(content=reply))
        self.assertNotIn('institution_id', self.agent.saved_state)

    async def test_an_already_routed_chat_ignores_the_models_id(self):
        """The page's answer outranks the model's - and is what gets filed."""
        reply = '{"message":"ok","status":1,"source":1,"institution_id":3}'
        await self.run_turn(agent=StubAgent(content=reply), institution_id=1)
        self.assertEqual(self.lead_updates[0].get('institution_id'), 1)
        self.assertNotEqual(self.agent.saved_state.get('institution_id'), 3)


class TestTheParentIsNotWelcomedTwice(TurnHarness):
    """The server greets, then the model - replying to "Hi" - greeted again, so
    a new WhatsApp chat opened with two near-identical welcomes."""

    async def test_a_new_chat_tells_the_model_the_welcome_is_already_sent(self):
        await self.run_turn(agent=StubAgent(existing_session=False))
        prompt = self.agent.prompts[0]
        self.assertIn('GREETING:', prompt)
        self.assertIn('Do NOT greet, welcome, or introduce yourself again', prompt)

    async def test_an_ongoing_chat_carries_no_greeting_line(self):
        await self.run_turn(agent=StubAgent(existing_session=True))
        self.assertNotIn('GREETING:', self.agent.prompts[0])

    async def test_webchat_gets_the_line_without_the_server_sending_a_greeting(self):
        """The widget already showed it at /session, so the text must NOT be
        sent again - but the model still has to know it is on screen."""
        await self.run_turn(agent=StubAgent(existing_session=False),
                            channel_type='webchat',
                            introduction='Welcome to Sanabl Al!')
        self.assertIn('GREETING:', self.agent.prompts[0])
        self.assertNotIn('Welcome to Sanabl Al!', self.sent)

    async def test_whatsapp_still_sends_the_greeting_itself(self):
        await self.run_turn(agent=StubAgent(existing_session=False),
                            channel_type='whatsapp',
                            introduction='Welcome to Sanabl Al!')
        self.assertEqual(self.sent[0], 'Welcome to Sanabl Al!')

    async def test_exactly_one_greeting_line_per_turn(self):
        await self.run_turn(agent=StubAgent(existing_session=False))
        self.assertEqual(self.agent.prompts[0].count('GREETING:'), 1)
