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
        # every send succeeds unless a test says otherwise
        if not hasattr(self, 'send_ok'):
            self.send_ok = True

        def record_update(**kw):
            self.lead_updates.append(kw['payload'])
            return update_resp or {}

        async def send(t):
            # the channel reports whether it delivered; set send_ok=False to
            # rehearse a WhatsApp outage
            if not self.send_ok:
                return False
            self.sent.append(t)
            return True

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

    async def test_a_promised_follow_up_is_forwarded(self):
        """The assistant's report of a promise it just made is the ONLY thing
        that puts an unanswerable question in front of staff - if it is dropped
        between here and the gateway, the parent waits for a call nobody knows
        to make."""
        await self.run_turn(agent=StubAgent(content=(
            '{"message":"Our admissions team will confirm that for you.",'
            '"status":1,"source":1,'
            '"promised_staff_followup":"bus route to Feroke"}')))
        self.assertEqual(len(self.lead_updates), 1)
        self.assertEqual(self.lead_updates[0]['promised_staff_followup'],
                         'bus route to Feroke')

    async def test_no_promise_sends_no_field(self):
        """Absent must stay absent - COALESCE on the other side reads a value
        as a value, so an empty one every turn would be noise."""
        await self.run_turn(agent=StubAgent(content=(
            '{"message":"Noted.","status":1,"source":1,'
            '"lead":{"name":"Varun","student_name":"Ashiq"}}')))
        self.assertNotIn('promised_staff_followup', self.lead_updates[0])

    async def test_a_no_otp_school_is_told_there_is_no_code(self):
        """Live, on a no-OTP school, the assistant answered "yes register it"
        with "a verification code has been sent to your mobile number". No code
        was sent; the inquiry had already been created; the parent sat waiting.
        The stage line said "...once they verify" and it filled in the rest."""
        from api.routes.shared.msgchannel_processor import (
            stage_context, STAGE_COLLECTING)
        line = stage_context(STAGE_COLLECTING, verifies=False)
        self.assertIn('does NOT verify', line)
        self.assertIn('Never mention a code', line)

    def test_a_verifying_school_still_says_so(self):
        from api.routes.shared.msgchannel_processor import (
            stage_context, STAGE_COLLECTING)
        line = stage_context(STAGE_COLLECTING, verifies=True)
        self.assertIn('DOES verify', line)
        self.assertIn('do not announce it before it has', line)

    def test_an_unknown_posture_promises_nothing_either_way(self):
        """The first turns of a company-level chat, before a school is picked."""
        from api.routes.shared.msgchannel_processor import (
            stage_context, STAGE_COLLECTING)
        line = stage_context(STAGE_COLLECTING, verifies=None)
        self.assertNotIn('verify', line.split('STAGE: collecting')[-1][:400].replace(
            'no verification code is outstanding', ''))

    def test_the_stage_line_no_longer_assumes_verification(self):
        """The sentence that invited the invention is gone."""
        from api.routes.shared.msgchannel_processor import STAGE_CONTEXT, STAGE_COLLECTING
        self.assertNotIn('once they verify', STAGE_CONTEXT[STAGE_COLLECTING])

    def test_only_the_collecting_stage_carries_it(self):
        """By awaiting_verification a code demonstrably exists; by submitted it
        no longer matters."""
        from api.routes.shared.msgchannel_processor import (
            stage_context, STAGE_AWAITING_VERIFICATION, STAGE_SUBMITTED, STAGE_CONTEXT)
        for stage in (STAGE_AWAITING_VERIFICATION, STAGE_SUBMITTED):
            with self.subTest(stage=stage):
                self.assertEqual(stage_context(stage, verifies=False),
                                 STAGE_CONTEXT[stage])

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


class TestAChatCannotCloseOnNothing(TurnHarness):
    """A parent answered every question and was told "your details have been
    noted, our team will be in touch" - while the conversation had no class and
    no inquiry behind it. The stage is what knows: still `collecting` means the
    backend created nothing."""

    CLOSING = ('{"message":"All details noted. Our team will be in touch.",'
               '"status":1,"source":1,"is_chat_completed":true}')

    async def test_the_false_sign_off_is_not_sent(self):
        await self.run_turn(agent=StubAgent(content=self.CLOSING), update_resp={})
        self.assertNotIn('All details noted. Our team will be in touch.', self.sent)

    async def test_the_parent_is_told_something_honest_instead(self):
        await self.run_turn(agent=StubAgent(content=self.CLOSING), update_resp={})
        self.assertEqual(len(self.sent), 1)
        self.assertIn("can't pass this to our admissions team just yet", self.sent[0])

    async def test_the_correction_reaches_the_models_history(self):
        """The server spoke on the model's behalf, so the next turn must know."""
        await self.run_turn(agent=StubAgent(content=self.CLOSING), update_resp={})
        self.assertTrue(any('admissions team just yet' in t for t in self.remembered))

    async def test_the_crm_is_told_the_chat_is_not_finished(self):
        await self.run_turn(agent=StubAgent(content=self.CLOSING), update_resp={})
        corrections = [u for u in self.lead_updates if u.get('is_chat_completed') is False]
        self.assertEqual(len(corrections), 1)

    async def test_a_real_close_is_left_alone(self):
        """Verification completed, so the inquiry exists - the model's sign-off
        is true and must reach the parent."""
        await self.run_turn(agent=StubAgent(stage=STAGE_AWAITING_VERIFICATION,
                                            content=self.CLOSING),
                            update_resp={'otp_verified_now': True, 'registration_id': 9})
        self.assertTrue(any('Verification successful' in t for t in self.sent))
        self.assertNotIn("can't pass this", ' '.join(self.sent))

    async def test_an_ordinary_turn_is_untouched(self):
        await self.run_turn(update_resp={})
        self.assertEqual(self.sent, ['Which class is your child applying for?'])


class TestAnUnconfirmedSummaryLeavesNoInquiry(TurnHarness):
    """The summary turn collects everything - class included - and confirms
    nothing. The server refuses to create an inquiry without
    details_confirmed, so the turn must record the details in full and must
    not behave as though an inquiry exists."""

    # the summary turn: every field set, no confirmation claimed
    SUMMARY = ('{"message":"So that\'s Firoz, Grade 5, under Ashiq. Shall I register that?",'
               '"status":1,"source":1,"class_id":45,'
               '"lead":{"name":"Ashiq","student_name":"Firoz"}}')

    async def test_the_details_still_reach_the_record(self):
        """An abandoned confirmation has to leave a lead staff can follow up."""
        await self.run_turn(agent=StubAgent(content=self.SUMMARY), update_resp={})
        self.assertEqual(self.lead_updates[0].get('name'), 'Ashiq')
        self.assertEqual(self.lead_updates[0].get('student_name'), 'Firoz')
        self.assertEqual(self.lead_updates[0].get('class_id'), 45)

    async def test_no_confirmation_is_claimed(self):
        """The absent flag is what stops the backend creating anything - it
        must not be invented from a turn that merely has every field."""
        await self.run_turn(agent=StubAgent(content=self.SUMMARY), update_resp={})
        self.assertIsNone(self.lead_updates[0].get('details_confirmed'))

    async def test_the_summary_reaches_the_parent(self):
        await self.run_turn(agent=StubAgent(content=self.SUMMARY), update_resp={})
        self.assertEqual(len(self.sent), 1)
        self.assertIn('Shall I register that?', self.sent[0])

    async def test_the_chat_stays_in_collecting(self):
        """Nothing was created, so the stage must not advance - that is what
        stops a later turn claiming the inquiry is registered."""
        agent = StubAgent(content=self.SUMMARY)
        await self.run_turn(agent=agent, update_resp={})
        self.assertNotIn('admission_stage', agent.saved_state)

    async def test_confirming_forwards_the_flag(self):
        confirmed = ('{"message":"Registered.","status":1,"source":1,"class_id":45,'
                     '"details_confirmed":true,"lead":{"student_name":"Firoz"}}')
        await self.run_turn(agent=StubAgent(content=confirmed),
                            update_resp={'registration_id': 77})
        self.assertIs(self.lead_updates[0].get('details_confirmed'), True)

    async def test_a_withdrawn_confirmation_is_forwarded_as_false(self):
        """false and null are different answers: null leaves the stored flag
        alone, false clears it. Dropping false would let a correction keep a
        confirmation the parent has taken back."""
        withdrawn = ('{"message":"Sure, which class?","status":1,"source":1,'
                     '"details_confirmed":false,"lead":{"student_name":"Firoz"}}')
        await self.run_turn(agent=StubAgent(content=withdrawn), update_resp={})
        self.assertIs(self.lead_updates[0].get('details_confirmed'), False)


class TestARefusalKeepsTheAssistant(TurnHarness):
    """A refused edit used to end the conversation. The notice is the only
    thing the parent sees, so it has to be true AND leave a way forward."""

    # 7
    async def test_the_deterministic_notice_is_still_sent(self):
        await self.run_turn(update_resp={'edit_refused': True})
        self.assertEqual(len(self.sent), 1)
        self.assertIn("can't be changed here", self.sent[0])

    async def test_the_notice_no_longer_claims_a_hand_off(self):
        """Nothing passes the request to admissions and nothing can - staff
        have no way to reply into this conversation."""
        await self.run_turn(update_resp={'edit_refused': True})
        text = self.sent[0].lower()
        for claim in ("i've passed", 'passed your request', 'they will review',
                      'have been notified', 'has been forwarded'):
            self.assertNotIn(claim, text)

    async def test_the_notice_invites_the_parent_to_carry_on(self):
        """The assistant is still available now, so it should say so."""
        await self.run_turn(update_resp={'edit_refused': True})
        self.assertIn('anything else', self.sent[0].lower())

    async def test_the_models_own_reply_is_still_suppressed(self):
        """It may claim the change was made - the server verdict speaks alone."""
        await self.run_turn(update_resp={'edit_refused': True})
        self.assertNotIn('Which class is your child applying for?', self.sent)

    # 8
    async def test_the_next_turn_still_reaches_the_model(self):
        """No escalation means the chat stays live: the following message is
        answered normally."""
        await self.run_turn(update_resp={'edit_refused': True})
        await self.run_turn(update_resp={})
        self.assertEqual(self.sent, ['Which class is your child applying for?'])

    async def test_a_failed_promotion_still_speaks_for_itself(self):
        """The other server verdict is untouched by this change."""
        await self.run_turn(update_resp={'promotion_failed': True})
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn('Which class', self.sent[0])


class TestSubmittedStageDoesNotEscalateACorrection(unittest.TestCase):
    """The instruction the model acts on. Escalation stops the assistant for
    the rest of the conversation, so it must not be the answer to "can you fix
    my email" - it is the answer to "can I speak to someone"."""

    def line(self):
        return MP.STAGE_CONTEXT[STAGE_SUBMITTED]

    def test_it_no_longer_orders_an_escalation_for_a_change(self):
        self.assertNotIn('set escalation=true and tell them', self.line())
        self.assertIn('Do NOT set escalation for this', self.line())

    def test_the_read_only_rule_is_untouched(self):
        """The gateway enforces this too, but the model must not try."""
        self.assertIn('do NOT set any lead field', self.line())
        self.assertIn('cannot be changed here', self.line())

    def test_it_says_who_actually_handles_corrections(self):
        self.assertIn('admissions team handles corrections', self.line())

    def test_it_keeps_the_conversation_open(self):
        self.assertIn('offer to help with anything else', self.line())

    def test_escalation_is_still_available_for_a_human_request(self):
        """Removing the reflex must not remove the capability."""
        self.assertIn('Set escalation=true only when they ASK to speak to someone',
                      self.line())
        self.assertIn('genuinely cannot help', self.line())

    def test_a_second_child_is_still_allowed(self):
        self.assertIn('Registering ANOTHER child', self.line())

    def test_the_other_stages_are_unchanged(self):
        """Only the submitted stage was in scope."""
        self.assertIn('may still correct ANY detail',
                      MP.STAGE_CONTEXT[STAGE_AWAITING_VERIFICATION])
        self.assertIn('nothing is submitted yet', MP.STAGE_CONTEXT[STAGE_COLLECTING])


class TestEscalationStillWorksWhenTheModelAsksForIt(TurnHarness):
    """The path, not the judgement: a model that escalates is still escalated."""

    ESCALATE = ('{"message":"Let me get someone from our team.","status":1,'
                '"source":1,"escalation":true}')

    async def test_an_explicit_escalation_still_reaches_the_parent(self):
        # the escalation path posts to the CRM and broadcasts over redis -
        # neither belongs in a unit test, and both are slow when absent
        with patch.object(MP, 'django_post', return_value={'ok': True}),              patch.object(MP, 'broadcast_to_conversation_async'),              patch.object(MP, 'broadcast_to_org_async'):
            await self.run_turn(agent=StubAgent(content=self.ESCALATE), update_resp={})
        self.assertTrue(self.sent, 'an escalation sent nothing to the parent')
        self.assertIn('team', ' '.join(self.sent).lower())

    async def test_an_ordinary_turn_is_not_escalated(self):
        await self.run_turn(update_resp={})
        self.assertEqual(self.sent, ['Which class is your child applying for?'])


class TestAnImpossiblePhoneIsAskedForAgain(TurnHarness):
    """The number was dropped, so the model's "noted your mobile" would be a
    lie - the server says what actually happened instead."""

    async def test_the_deterministic_notice_is_sent(self):
        await self.run_turn(update_resp={'phone_invalid': True})
        self.assertEqual(len(self.sent), 1)
        self.assertIn("doesn't look like a complete phone number", self.sent[0])

    async def test_it_names_the_likely_fix(self):
        """Most rejected numbers are missing their country code."""
        await self.run_turn(update_resp={'phone_invalid': True})
        self.assertIn('country code', self.sent[0])

    async def test_the_models_reply_is_suppressed(self):
        await self.run_turn(update_resp={'phone_invalid': True})
        self.assertNotIn('Which class is your child applying for?', self.sent)

    async def test_the_notice_reaches_the_transcript(self):
        await self.run_turn(update_resp={'phone_invalid': True})
        self.assertTrue(any('complete phone number' in c for c in self.crm))

    async def test_an_ordinary_turn_is_untouched(self):
        await self.run_turn(update_resp={})
        self.assertEqual(self.sent, ['Which class is your child applying for?'])

    async def test_the_next_turn_carries_on(self):
        """Rejecting a number must not end the conversation."""
        await self.run_turn(update_resp={'phone_invalid': True})
        await self.run_turn(update_resp={})
        self.assertEqual(self.sent, ['Which class is your child applying for?'])


class TestResendNotices(TurnHarness):
    """Four verdicts the model cannot know, so the server says all four. The
    trap is the catch-all else: an unrecognised notice would announce a code
    that was never made."""

    def resend(self, notice, target='email'):
        return {'otp_required': True, 'otp_notice': notice, 'otp_target': target}

    async def test_a_successful_resend_says_so(self):
        await self.run_turn(update_resp=self.resend('resent'))
        self.assertEqual(len(self.sent), 1)
        self.assertIn('sent a new verification code', self.sent[0])
        self.assertIn('your email', self.sent[0])

    async def test_a_successful_resend_says_the_old_code_is_dead(self):
        await self.run_turn(update_resp=self.resend('resent'))
        self.assertIn('no longer works', self.sent[0])

    async def test_too_soon_does_not_claim_a_code_was_sent(self):
        """The whole point of giving it its own branch."""
        await self.run_turn(update_resp=self.resend('resend_too_soon'))
        text = self.sent[0].lower()
        self.assertIn('only just sent', text)
        self.assertNotIn("i've sent a new", text)
        self.assertIn('spam', text)

    async def test_the_limit_offers_a_way_out(self):
        await self.run_turn(update_resp=self.resend('resend_limit'))
        text = self.sent[0].lower()
        self.assertIn("can't send another", text)
        self.assertIn('admissions team', text)

    async def test_a_failed_send_is_admitted(self):
        await self.run_turn(update_resp=self.resend('resend_failed'))
        text = self.sent[0].lower()
        self.assertIn("wasn't able to send", text)
        self.assertNotIn('have sent', text)

    async def test_the_models_reply_is_suppressed_for_every_verdict(self):
        for notice in ('resent', 'resend_too_soon', 'resend_limit', 'resend_failed'):
            with self.subTest(notice=notice):
                await self.run_turn(update_resp=self.resend(notice))
                self.assertEqual(len(self.sent), 1, 'the model also spoke on %s' % notice)
                self.assertNotIn('Which class is your child applying for?', self.sent)

    async def test_every_verdict_reaches_the_transcript(self):
        for notice in ('resent', 'resend_too_soon', 'resend_limit', 'resend_failed'):
            with self.subTest(notice=notice):
                await self.run_turn(update_resp=self.resend(notice))
                self.assertEqual(len(self.crm), 1)

    async def test_a_silent_otp_turn_still_lets_the_model_answer(self):
        """A question while a code is outstanding: no notice, no suppression."""
        await self.run_turn(update_resp={'otp_required': True, 'otp_notice': None})
        self.assertEqual(self.sent, ['Which class is your child applying for?'])


class TestResendIntentIsForwarded(TurnHarness):
    """The model reports the request; it never makes a code."""

    ASK = ('{"message":"Sure, let me resend that.","status":1,"source":1,'
           '"otp_resend_requested":true}')

    async def test_a_true_flag_reaches_the_backend(self):
        await self.run_turn(agent=StubAgent(content=self.ASK),
                            update_resp=self.__class__.__dict__.get('_r') or
                            {'otp_required': True, 'otp_notice': 'resent',
                             'otp_target': 'email'})
        self.assertIs(self.lead_updates[0].get('otp_resend_requested'), True)

    async def test_an_ordinary_turn_forwards_nothing(self):
        await self.run_turn(update_resp={})
        self.assertIsNone(self.lead_updates[0].get('otp_resend_requested'))


class TestSendFailureIsAdmitted(TurnHarness):
    """A code was generated but the sender could not deliver it. Every other
    notice in this family announces a send; this one must not, and the model -
    which has no idea either way - must not speak over it."""

    FAILED = {'otp_required': True, 'otp_notice': 'send_failed', 'otp_target': 'email'}

    async def test_the_failure_is_stated_plainly(self):
        await self.run_turn(update_resp=self.FAILED)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("wasn't able to send the verification code", self.sent[0])

    async def test_it_never_claims_a_code_was_sent(self):
        """The bug this exists to kill: falling through to the 'new' branch."""
        await self.run_turn(update_resp=self.FAILED)
        text = self.sent[0].lower()
        for claim in ("we've sent", "i've sent", 'has been sent', 'sent a verification code to'):
            self.assertNotIn(claim, text)

    async def test_it_does_not_blame_the_parent(self):
        await self.run_turn(update_resp=self.FAILED)
        self.assertIn('on our side, not yours', self.sent[0])

    async def test_it_points_at_the_way_back(self):
        """The code is live and resend is the recovery path, so say so."""
        await self.run_turn(update_resp=self.FAILED)
        self.assertIn('resend', self.sent[0].lower())

    async def test_the_models_reply_is_suppressed(self):
        await self.run_turn(update_resp=self.FAILED)
        self.assertNotIn('Which class is your child applying for?', self.sent)

    async def test_it_reaches_the_transcript(self):
        await self.run_turn(update_resp=self.FAILED)
        self.assertTrue(any("wasn't able to send" in c for c in self.crm))

    async def test_a_successful_send_is_unaffected(self):
        await self.run_turn(update_resp={
            'otp_required': True, 'otp_notice': 'new', 'otp_target': 'email'})
        self.assertIn("We've sent a verification code", self.sent[0])

    async def test_a_successful_renewal_is_unaffected(self):
        await self.run_turn(update_resp={
            'otp_required': True, 'otp_notice': 'renewed', 'otp_target': 'email'})
        self.assertIn('FRESH verification code', self.sent[0])

    async def test_a_wrong_guess_with_tries_left_is_unaffected(self):
        await self.run_turn(update_resp={
            'otp_required': True, 'otp_notice': 'retry',
            'otp_target': 'email', 'otp_attempts_left': 2})
        self.assertIn("didn't match", self.sent[0])

    async def test_the_conversation_continues_afterwards(self):
        await self.run_turn(update_resp=self.FAILED)
        await self.run_turn(update_resp={})
        self.assertEqual(self.sent, ['Which class is your child applying for?'])

    async def test_send_failed_is_distinct_from_resend_failed(self):
        """Both are honest, but they answer different questions - one follows a
        request the parent made, the other does not."""
        await self.run_turn(update_resp=self.FAILED)
        automatic = self.sent[0]
        await self.run_turn(update_resp={
            'otp_required': True, 'otp_notice': 'resend_failed', 'otp_target': 'email'})
        self.assertNotEqual(automatic, self.sent[0])


class TestAFailedSendIsNotRecorded(TurnHarness):
    """WhatsApp refused the message - outside the 24h window, an auth failure,
    the API unreachable. What must NOT happen is the record claiming otherwise:
    staff reading the transcript would contradict the parent, and the next turn
    would reason as though they had been told."""

    async def test_the_models_reply_is_not_written_to_the_transcript(self):
        self.send_ok = False
        await self.run_turn(update_resp={})
        self.assertEqual(self.crm, [], 'an undelivered reply was recorded as sent')

    async def test_a_server_message_is_not_written_to_the_transcript(self):
        self.send_ok = False
        await self.run_turn(update_resp={'edit_refused': True})
        self.assertEqual(self.crm, [])

    async def test_a_server_message_is_not_remembered(self):
        """The model must not be told the parent knows something they don't."""
        self.send_ok = False
        await self.run_turn(update_resp={'edit_refused': True})
        self.assertEqual(self.remembered, [], 'an undelivered message entered history')

    async def test_the_turn_still_completes(self):
        """A failed send is not a crash - the backend was already updated."""
        self.send_ok = False
        await self.run_turn(update_resp={})
        self.assertTrue(self.lead_updates, 'the turn aborted on a failed send')

    async def test_an_otp_notice_that_fails_is_not_recorded(self):
        self.send_ok = False
        await self.run_turn(update_resp={
            'otp_required': True, 'otp_notice': 'new', 'otp_target': 'email'})
        self.assertEqual(self.crm, [])
        self.assertEqual(self.remembered, [])

    async def test_send_failed_that_fails_to_send_is_not_recorded(self):
        """The honest failure notice can itself fail to reach WhatsApp."""
        self.send_ok = False
        await self.run_turn(update_resp={
            'otp_required': True, 'otp_notice': 'send_failed', 'otp_target': 'email'})
        self.assertEqual(self.crm, [])

    async def test_resend_failed_that_fails_to_send_is_not_recorded(self):
        self.send_ok = False
        await self.run_turn(update_resp={
            'otp_required': True, 'otp_notice': 'resend_failed', 'otp_target': 'email'})
        self.assertEqual(self.crm, [])


class TestASuccessfulSendIsUnchanged(TurnHarness):
    """The other half: nothing about the working path moved."""

    async def test_the_reply_is_delivered_and_recorded(self):
        await self.run_turn(update_resp={})
        self.assertEqual(self.sent, ['Which class is your child applying for?'])
        self.assertEqual(self.crm, ['Which class is your child applying for?'])

    async def test_a_server_message_is_delivered_recorded_and_remembered(self):
        await self.run_turn(update_resp={'edit_refused': True})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.crm), 1)
        self.assertEqual(len(self.remembered), 1)

    async def test_otp_notices_still_behave(self):
        await self.run_turn(update_resp={
            'otp_required': True, 'otp_notice': 'new', 'otp_target': 'email'})
        self.assertIn("We've sent a verification code", self.sent[0])
        self.assertEqual(len(self.crm), 1)

    async def test_send_failed_still_behaves(self):
        await self.run_turn(update_resp={
            'otp_required': True, 'otp_notice': 'send_failed', 'otp_target': 'email'})
        self.assertIn("wasn't able to send the verification code", self.sent[0])
        self.assertEqual(len(self.crm), 1)

    async def test_resend_notices_still_behave(self):
        for notice in ('resent', 'resend_too_soon', 'resend_limit', 'resend_failed'):
            with self.subTest(notice=notice):
                await self.run_turn(update_resp={
                    'otp_required': True, 'otp_notice': notice, 'otp_target': 'email'})
                self.assertEqual(len(self.sent), 1)
                self.assertEqual(len(self.crm), 1)


class TestAClientSuppliedInstitutionIsValidated(TurnHarness):
    """The webchat widget posts institution_id in its own request body, and the
    endpoint is public. The model's answer has always been checked against the
    live list; the browser's was not, and it decides which school's knowledge
    the parent sees AND whether verification is required at all.

    The harness fixture offers this agent exactly ONE institution - 3 - so 1
    stands in for a real institution that belongs to somebody else, which is
    the case that matters.
    """

    # 1
    async def test_a_valid_institution_is_accepted(self):
        agent = StubAgent()
        await self.run_turn(agent=agent, channel_type='webchat',
                            institution_id=3, update_resp={})
        self.assertEqual(agent.saved_state.get('institution_id'), 3)
        self.assertEqual(self.lead_updates[0].get('institution_id'), 3)

    # 2
    async def test_a_nonexistent_institution_is_rejected(self):
        """A high unused id - the shape of a probe."""
        agent = StubAgent()
        await self.run_turn(agent=agent, channel_type='webchat',
                            institution_id=999999, update_resp={})
        self.assertIsNone(agent.saved_state.get('institution_id'))
        self.assertIsNone(self.lead_updates[0].get('institution_id'))

    # 3 - THE SECURITY CASE
    async def test_an_institution_outside_this_agents_list_is_rejected(self):
        """Syntactically fine, really exists somewhere, but is not offered by
        THIS agent. It must never reach the promotion payload."""
        agent = StubAgent()
        await self.run_turn(agent=agent, channel_type='webchat',
                            institution_id=1, update_resp={})
        self.assertIsNone(self.lead_updates[0].get('institution_id'),
                          'a foreign institution reached the CRM payload')

    # 4
    async def test_a_rejected_institution_is_not_stored_on_the_session(self):
        agent = StubAgent()
        await self.run_turn(agent=agent, channel_type='webchat',
                            institution_id=1, update_resp={})
        self.assertIsNone(agent.saved_state.get('institution_id'),
                          'a rejected institution was saved and would route later turns')

    # 5 + 7
    async def test_a_rejected_institution_cannot_reach_the_otp_lookup(self):
        """GetIsOtpRequiredAsync on an unknown id returns nothing, which is not
        'Y' - so an accepted bad id would silently switch verification off.
        The payload must carry no institution at all."""
        await self.run_turn(channel_type='webchat', institution_id=999999,
                            update_resp={})
        self.assertNotIn('institution_id', self.lead_updates[0])

    # 6
    async def test_a_valid_stored_institution_survives_an_invalid_client_value(self):
        agent = StubAgent(institution=3)
        await self.run_turn(agent=agent, channel_type='webchat',
                            institution_id=999999, update_resp={})
        self.assertEqual(self.lead_updates[0].get('institution_id'), 3,
                         'a bad client value displaced a good stored one')

    async def test_no_client_value_preserves_the_stored_one(self):
        agent = StubAgent(institution=3)
        await self.run_turn(agent=agent, channel_type='webchat',
                            institution_id=None, update_resp={})
        self.assertEqual(self.lead_updates[0].get('institution_id'), 3)

    # 9 - the regression that would break WhatsApp
    async def test_a_channel_bound_institution_is_never_second_guessed(self):
        """WhatsApp's institution comes from the channel row, and an
        institution-scoped agent has an EMPTY choice list - so validating it
        would reject it and hand the chat back to the pooled company agent."""
        agent = StubAgent()
        await self.run_turn(agent=agent, channel_type='whatsapp',
                            institution_id=2, update_resp={})
        self.assertEqual(agent.saved_state.get('institution_id'), 2)
        self.assertEqual(self.lead_updates[0].get('institution_id'), 2)

    async def test_telegram_is_also_left_alone(self):
        agent = StubAgent()
        await self.run_turn(agent=agent, channel_type='telegram',
                            institution_id=2, update_resp={})
        self.assertEqual(self.lead_updates[0].get('institution_id'), 2)

    async def test_the_school_agent_still_serves_a_valid_webchat_turn(self):
        """Routing must survive the check: a valid id still switches the turn
        to that school's own agent."""
        await self.run_turn(channel_type='webchat', institution_id=3,
                            serving_agent=7, update_resp={})
        self.assertEqual(self.agent_ids[-1], 7)

    async def test_a_rejected_id_does_not_reroute_the_turn(self):
        await self.run_turn(channel_type='webchat', institution_id=999999,
                            serving_agent=7, update_resp={})
        self.assertNotIn(7, self.agent_ids)


class TestASaveThatNeverLandedIsAdmitted(TurnHarness):
    """The model writes its reply BEFORE the backend call, so on a turn where
    /leads/update/ never reached the server it would happily say "your details
    have been noted" about details nothing had saved."""

    DOWN = {'ok': False, 'status': 503, 'error': 'refused'}

    async def test_the_models_reply_is_not_sent(self):
        await self.run_turn(update_resp=self.DOWN)
        self.assertNotIn('Which class is your child applying for?', self.sent)

    async def test_the_failure_is_stated_instead(self):
        await self.run_turn(update_resp=self.DOWN)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('trouble saving that just now', self.sent[0])

    async def test_it_says_nothing_was_lost(self):
        """True, and the reason the parent can simply try again: their message
        is already on the record and the details are still in the chat."""
        await self.run_turn(update_resp=self.DOWN)
        self.assertIn('nothing has been lost', self.sent[0])

    async def test_it_never_claims_the_details_were_saved(self):
        await self.run_turn(update_resp=self.DOWN)
        text = self.sent[0].lower()
        for claim in ('noted', 'saved', 'registered', 'submitted'):
            self.assertNotIn(claim, text)

    async def test_the_suppressed_reply_is_not_recorded_as_delivered(self):
        await self.run_turn(update_resp=self.DOWN)
        self.assertNotIn('Which class is your child applying for?', self.crm)

    async def test_the_stage_does_not_move(self):
        agent = StubAgent(stage=STAGE_COLLECTING)
        await self.run_turn(agent=agent, update_resp=self.DOWN)
        self.assertNotEqual(agent.saved_state.get('admission_stage'), STAGE_SUBMITTED)

    async def test_a_pending_verification_stage_is_preserved(self):
        agent = StubAgent(stage=STAGE_AWAITING_VERIFICATION)
        await self.run_turn(agent=agent, update_resp=self.DOWN)
        self.assertNotEqual(agent.saved_state.get('admission_stage'), STAGE_SUBMITTED)

    async def test_every_temporary_status_is_covered(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                await self.run_turn(update_resp={'ok': False, 'status': status})
                self.assertIn('trouble saving', self.sent[0])

    async def test_the_next_turn_continues_normally(self):
        await self.run_turn(update_resp=self.DOWN)
        await self.run_turn(update_resp={})
        self.assertEqual(self.sent, ['Which class is your child applying for?'])


class TestOnlyTransportFailureIsTreatedThatWay(TurnHarness):
    """Business refusals already have their own notices and their own
    escalation. An empty response means no call was made at all."""

    async def test_a_turn_that_saved_nothing_still_sends_the_reply(self):
        """update_resp={} is 'no lead fields this turn', not 'saving broke'.
        Keying on emptiness would have silenced every ordinary question."""
        await self.run_turn(update_resp={})
        self.assertEqual(self.sent, ['Which class is your child applying for?'])

    async def test_a_successful_save_still_sends_the_reply(self):
        await self.run_turn(update_resp={'ok': True, 'lead_id': 7, 'changed': True})
        self.assertEqual(self.sent, ['Which class is your child applying for?'])

    async def test_a_failed_promotion_keeps_its_own_notice(self):
        """ok:true with a business flag - untouched by this change."""
        await self.run_turn(update_resp={'ok': True, 'promotion_failed': True})
        self.assertEqual(len(self.sent), 1)
        self.assertIn('could not complete the registration', self.sent[0])
        self.assertNotIn('trouble saving', self.sent[0])

    async def test_a_refused_edit_keeps_its_own_notice(self):
        await self.run_turn(update_resp={'ok': True, 'edit_refused': True})
        self.assertIn("can't be changed here", self.sent[0])

    async def test_a_permanent_client_error_is_not_treated_as_temporary(self):
        """A 400 is the backend saying no, not the backend being absent."""
        await self.run_turn(update_resp={'ok': False, 'status': 400})
        self.assertEqual(self.sent, ['Which class is your child applying for?'])
