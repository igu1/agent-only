"""An escalated conversation answers once, and never wakes the AI.

Escalation hands the chat to a person, so the turn is dropped before the agent
is built. That is correct, and it is also why the parent hears nothing at all
after the hand-off - live case C ended with a parent saying "yes that is right"
into a channel that never spoke again.

These cover the acknowledgement that closes that silence: that it is sent, sent
ONCE, sent only for escalation and not for the other reasons a turn is ignored,
and that it changes nothing else - no agent, no conversation state.

    python -m unittest discover -s tests -p "test_escalated_ack.py"
"""
import unittest
from unittest.mock import patch

import api.routes.shared.generic_webhook as GW


class FakeMessage:
    chat_id = 'chat-1'
    user_id = 'user-1'
    message_id = 'm-1'
    text = 'are you still there?'
    has_media = False
    is_voice = False
    voice_media_id = None
    sent_at = None
    institution_id = 1


class FakeAdapter:
    """Stands in for the WhatsApp/webchat adapter, and records every send."""

    def __init__(self):
        self.sent: list[str] = []

    def get_service(self, channel_id):
        return object()

    def validate(self, **kwargs):
        return None

    def parse(self, **kwargs):
        return FakeMessage()

    def should_ignore(self, **kwargs):
        return False

    def send_typing_indicator(self, **kwargs):
        return None

    def send_message(self, *, service, message, text):
        self.sent.append(text)
        return True


class AckHarness(unittest.IsolatedAsyncioTestCase):
    """One webhook call, with the backend's inbound reply canned."""

    def setUp(self):
        self.adapter = FakeAdapter()
        self.saved: list[dict] = []
        self.agent_calls: list[tuple] = []
        GW._acked.clear()

    async def call(self, inbound):
        """Drive the real handler with a canned inbound response."""
        with patch.object(GW.registry, 'has', return_value=True), \
             patch.object(GW.registry, 'get', return_value=self.adapter), \
             patch.object(GW, 'django_post', return_value=inbound), \
             patch.object(GW, 'save_message',
                          side_effect=lambda **kw: self.saved.append(kw)), \
             patch.object(GW, 'broadcast_to_conversation_async'), \
             patch.object(GW, 'broadcast_to_org_async'), \
             patch.object(GW, 'enqueue',
                          side_effect=lambda **kw: self.agent_calls.append(('enqueue', kw))), \
             patch.object(GW, 'handle_agent_batch',
                          side_effect=lambda **kw: self.agent_calls.append(('batch', kw))):
            return await GW.handle_incoming_webhook(
                channel_type='whatsapp', channel_id='2', payload={}, headers={})

    ESCALATED = {'ok': True, 'lead_id': 42, 'agent_id': 1, 'organization_id': 1,
                 'ai_enabled': False, 'escalated': True, 'ignore': True}


class TestTheParentIsAnswered(AckHarness):

    async def test_an_escalated_inbound_gets_an_acknowledgement(self):
        await self.call(self.ESCALATED)
        self.assertEqual(self.adapter.sent, [GW.ESCALATED_ACK])

    async def test_the_acknowledgement_reaches_the_transcript(self):
        """Staff read the chat to decide what to do; a reply the record does
        not show is a reply they will contradict."""
        await self.call(self.ESCALATED)
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0]['lead_id'], 42)
        self.assertEqual(self.saved[0]['content'], GW.ESCALATED_ACK)
        self.assertEqual(self.saved[0]['sender_type'], 'ai')

    async def test_it_does_not_promise_a_reply_in_this_chat(self):
        """Staff cannot send into a conversation yet, so an acknowledgement
        that implied they could would be the same mistake as telling a parent
        their new number was saved when it was not."""
        text = GW.ESCALATED_ACK.lower()
        self.assertIn('follow up with you directly', text)
        for promise in ('reply here', 'reply to this', 'message you here',
                        'respond here', 'in this chat'):
            self.assertNotIn(promise, text)


class TestItIsSaidOnce(AckHarness):

    async def test_a_second_message_inside_the_window_is_not_answered_again(self):
        """A paragraph typed as five messages asked once."""
        await self.call(self.ESCALATED)
        await self.call(self.ESCALATED)
        await self.call(self.ESCALATED)
        self.assertEqual(self.adapter.sent, [GW.ESCALATED_ACK])
        self.assertEqual(len(self.saved), 1)

    async def test_it_is_said_again_once_the_window_has_passed(self):
        """A parent coming back hours later is asking afresh, not repeating."""
        await self.call(self.ESCALATED)
        for key in list(GW._acked):
            GW._acked[key] -= (GW._ACK_COOLDOWN_SECONDS + 1)
        await self.call(self.ESCALATED)
        self.assertEqual(self.adapter.sent, [GW.ESCALATED_ACK, GW.ESCALATED_ACK])

    async def test_two_conversations_do_not_share_a_cooldown(self):
        await self.call(self.ESCALATED)
        other = dict(self.ESCALATED)
        with patch.object(FakeMessage, 'chat_id', 'chat-2'):
            await self.call(other)
        self.assertEqual(len(self.adapter.sent), 2)


class TestOnlyEscalationIsAnswered(AckHarness):
    """`ignore` covers more than escalation, and the rest have nobody to
    answer - a deactivated channel has no conversation and asked us nothing."""

    async def test_an_unknown_or_inactive_channel_is_not_answered(self):
        await self.call({'ok': False, 'ignore': True})
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.saved, [])

    async def test_an_ignored_turn_with_no_conversation_is_not_answered(self):
        """ok, ignored, but no lead_id - a blank chat id never became a row."""
        await self.call({'ok': True, 'ignore': True, 'escalated': True,
                         'lead_id': None})
        self.assertEqual(self.adapter.sent, [])

    async def test_an_ignored_turn_that_is_not_escalated_is_not_answered(self):
        await self.call({'ok': True, 'ignore': True, 'escalated': False,
                         'lead_id': 42})
        self.assertEqual(self.adapter.sent, [])


class TestNothingElseMoves(AckHarness):

    async def test_the_agent_is_never_invoked(self):
        """The whole point of escalation: a person is answering, not the AI."""
        await self.call(self.ESCALATED)
        self.assertEqual(self.agent_calls, [])

    async def test_no_conversation_state_is_written(self):
        """Only inbound/ is called - nothing posts to leads/update or
        escalation/, so Escalated and AiEnabled are left exactly as found."""
        posts = []
        with patch.object(GW.registry, 'has', return_value=True), \
             patch.object(GW.registry, 'get', return_value=self.adapter), \
             patch.object(GW, 'django_post',
                          side_effect=lambda path, body, **kw: (
                              posts.append(path) or dict(self.ESCALATED))), \
             patch.object(GW, 'save_message', side_effect=lambda **kw: None), \
             patch.object(GW, 'broadcast_to_conversation_async'), \
             patch.object(GW, 'broadcast_to_org_async'):
            await GW.handle_incoming_webhook(
                channel_type='whatsapp', channel_id='2', payload={}, headers={})
        self.assertEqual(posts, ['/api/agent/v1/inbound/'])

    async def test_a_failed_send_does_not_break_the_webhook(self):
        """A courtesy line is not worth 500-ing a webhook Meta will retry."""
        self.adapter.send_message = lambda **kw: (_ for _ in ()).throw(RuntimeError('down'))
        result = await self.call(self.ESCALATED)
        self.assertTrue(result.get('ok'))

    async def test_an_ordinary_turn_still_reaches_the_agent(self):
        """The guard must not have swallowed live conversations."""
        await self.call({'ok': True, 'lead_id': 42, 'agent_id': 1,
                         'organization_id': 1, 'ai_enabled': True,
                         'escalated': False, 'ignore': False})
        self.assertEqual(self.adapter.sent, [])
        self.assertTrue(self.agent_calls, 'a normal turn never reached the agent')


if __name__ == '__main__':
    unittest.main(verbosity=2)
