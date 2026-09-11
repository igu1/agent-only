"""Unsupported attachments get an answer instead of vanishing.

A parent photographing their child's birth certificate is the most natural
thing to do when a school asks for documents, and it used to disappear twice
over: the WhatsApp parser left `text` empty for an image, so
`if not text: return None` threw the whole message away, and the guard further
downstream never fired for WhatsApp at all.

Text and audio must be untouched - those are the two things that DO work, and
half these tests exist to prove they still do.

    python -m unittest discover -s tests -p "test_unreadable_media.py"
"""
import unittest
from unittest.mock import patch

import api.routes.shared.generic_webhook as GW
from api.routes.shared.channel_adapters import WhatsAppAdapter


def wa_payload(message_type, **extra):
    """A Meta webhook body carrying one message of the given type."""
    msg = {'from': '919876543210', 'id': 'wamid.T1',
           'timestamp': '1700000000', 'type': message_type}
    msg.update(extra)
    return {'object': 'whatsapp_business_account', 'entry': [{'id': '1', 'changes': [{
        'field': 'messages', 'value': {
            'messaging_product': 'whatsapp',
            'metadata': {'display_phone_number': '1555', 'phone_number_id': 'PNID'},
            'contacts': [{'profile': {'name': 'Parent'}, 'wa_id': '919876543210'}],
            'messages': [msg]}}]}]}


class TestTheParserStopsDiscardingAttachments(unittest.TestCase):
    """Half of the bug lived here: unsupported types produced no message."""

    def parse(self, payload):
        return WhatsAppAdapter().parse(service=object(), payload=payload)

    def test_text_still_parses_normally(self):
        m = self.parse(wa_payload('text', text={'body': 'hello'}))
        self.assertEqual(m.text, 'hello')
        self.assertFalse(m.has_media)
        self.assertIsNone(m.media_kind)

    def test_audio_still_parses_as_voice(self):
        """The supported path - bytes reach the model. Untouched."""
        m = self.parse(wa_payload('audio', audio={'id': 'media-1'}))
        self.assertTrue(m.is_voice)
        self.assertTrue(m.has_media)
        self.assertEqual(m.voice_media_id, 'media-1')
        self.assertIsNone(m.media_kind, 'audio must not be marked unreadable')

    def test_voice_still_parses_as_voice(self):
        m = self.parse(wa_payload('voice', voice={'id': 'media-2'}))
        self.assertTrue(m.is_voice)
        self.assertEqual(m.voice_media_id, 'media-2')

    def test_an_image_now_produces_a_message(self):
        m = self.parse(wa_payload('image', image={'id': 'i1'}))
        self.assertIsNotNone(m, 'the image was discarded at parse again')
        self.assertTrue(m.has_media)
        self.assertFalse(m.is_voice)
        self.assertEqual(m.media_kind, 'image')

    def test_a_document_now_produces_a_message(self):
        m = self.parse(wa_payload('document', document={'id': 'd1'}))
        self.assertIsNotNone(m)
        self.assertEqual(m.media_kind, 'document')
        self.assertFalse(m.is_voice)

    def test_a_video_now_produces_a_message(self):
        m = self.parse(wa_payload('video', video={'id': 'v1'}))
        self.assertEqual(m.media_kind, 'video')

    def test_a_sticker_now_produces_a_message(self):
        m = self.parse(wa_payload('sticker', sticker={'id': 's1'}))
        self.assertEqual(m.media_kind, 'sticker')

    def test_a_type_we_do_not_handle_is_still_ignored(self):
        """Reactions, locations, system notices - not attachments a parent is
        waiting on an answer about."""
        self.assertIsNone(self.parse(wa_payload('reaction', reaction={'emoji': 'x'})))


class MediaHarness(unittest.IsolatedAsyncioTestCase):
    """One webhook call with the backend's inbound reply canned."""

    class Adapter:
        def __init__(self, message):
            self._message = message
            self.sent: list[str] = []

        def get_service(self, channel_id):
            return object()

        def validate(self, **kw):
            return None

        def parse(self, **kw):
            return self._message

        def should_ignore(self, **kw):
            return False

        def send_typing_indicator(self, **kw):
            return None

        def send_message(self, *, service, message, text):
            self.sent.append(text)
            return True

    INBOUND = {'ok': True, 'lead_id': 42, 'agent_id': 1, 'organization_id': 1,
               'ai_enabled': True, 'escalated': False, 'ignore': False}

    def setUp(self):
        GW._media_acked.clear()
        self.saved: list[dict] = []
        self.agent_calls: list = []

    async def send(self, message, inbound=None):
        adapter = self.Adapter(message)
        with patch.object(GW.registry, 'has', return_value=True), \
             patch.object(GW.registry, 'get', return_value=adapter), \
             patch.object(GW, 'django_post', return_value=inbound or self.INBOUND), \
             patch.object(GW, 'save_message',
                          side_effect=lambda **kw: self.saved.append(kw)), \
             patch.object(GW, 'broadcast_to_conversation_async'), \
             patch.object(GW, 'broadcast_to_org_async'), \
             patch.object(GW, 'enqueue',
                          side_effect=lambda **kw: self.agent_calls.append(kw)), \
             patch.object(GW, 'handle_agent_batch',
                          side_effect=lambda **kw: self.agent_calls.append(kw)):
            result = await GW.handle_incoming_webhook(
                channel_type='whatsapp', channel_id='2', payload={}, headers={})
        return adapter, result


def a_message(**kw):
    from api.routes.shared.channel_adapters import IncomingMessage
    base = dict(chat_id='919876543210', user_id='wa_919876543210',
                text='[image received]', message_id='m1',
                has_media=True, is_voice=False, media_kind='image')
    base.update(kw)
    return IncomingMessage(**base)


class TestTheParentIsTold(MediaHarness):

    async def test_an_image_is_acknowledged(self):
        adapter, _ = await self.send(a_message(media_kind='image'))
        self.assertEqual(len(adapter.sent), 1)
        self.assertIn("can't read images", adapter.sent[0])
        self.assertIn('type the information', adapter.sent[0])

    async def test_a_document_names_documents(self):
        adapter, _ = await self.send(a_message(media_kind='document'))
        self.assertIn("can't open documents", adapter.sent[0])

    async def test_a_video_names_videos(self):
        adapter, _ = await self.send(a_message(media_kind='video'))
        self.assertIn("can't watch videos", adapter.sent[0])

    async def test_an_unnamed_attachment_gets_the_generic_reply(self):
        """Telegram and Instagram reach this guard without a media_kind."""
        adapter, _ = await self.send(a_message(media_kind=None,
                                               text='[Media received]'))
        self.assertIn("can't process attachments", adapter.sent[0])

    async def test_the_acknowledgement_reaches_the_transcript(self):
        await self.send(a_message())
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0]['sender_type'], 'ai')
        self.assertEqual(self.saved[0]['lead_id'], 42)

    async def test_it_goes_out_through_the_normal_adapter_path(self):
        adapter, _ = await self.send(a_message())
        self.assertTrue(adapter.sent, 'the reply bypassed the channel adapter')


class TestTheModelNeverSeesIt(MediaHarness):

    async def test_the_agent_is_not_invoked(self):
        """The reason this is answered at the webhook rather than downstream:
        a model that never sees the attachment cannot claim to have read it."""
        await self.send(a_message())
        self.assertEqual(self.agent_calls, [],
                         'an unreadable attachment reached the AI')

    async def test_the_webhook_still_succeeds(self):
        _, result = await self.send(a_message())
        self.assertTrue(result.get('ok'), 'Meta would retry this')

    async def test_a_failing_send_does_not_break_the_webhook(self):
        adapter = self.Adapter(a_message())
        adapter.send_message = lambda **kw: (_ for _ in ()).throw(RuntimeError('down'))
        with patch.object(GW.registry, 'has', return_value=True), \
             patch.object(GW.registry, 'get', return_value=adapter), \
             patch.object(GW, 'django_post', return_value=self.INBOUND), \
             patch.object(GW, 'save_message', side_effect=lambda **kw: None), \
             patch.object(GW, 'broadcast_to_conversation_async'), \
             patch.object(GW, 'broadcast_to_org_async'):
            result = await GW.handle_incoming_webhook(
                channel_type='whatsapp', channel_id='2', payload={}, headers={})
        self.assertTrue(result.get('ok'))


class TestItIsSaidOncePerBurst(MediaHarness):
    """An album of six photos is one question, not six."""

    async def test_a_second_attachment_inside_the_window_is_not_answered_again(self):
        adapter, _ = await self.send(a_message())
        adapter2, _ = await self.send(a_message())
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(adapter2.sent, [], 'the burst produced a second reply')

    async def test_it_is_said_again_after_the_window(self):
        await self.send(a_message())
        for key in list(GW._media_acked):
            GW._media_acked[key] -= (GW._MEDIA_ACK_COOLDOWN_SECONDS + 1)
        adapter2, _ = await self.send(a_message())
        self.assertEqual(len(adapter2.sent), 1)


class TestSupportedContentIsUntouched(MediaHarness):

    async def test_text_still_reaches_the_agent(self):
        await self.send(a_message(text='hello', has_media=False,
                                  media_kind=None))
        self.assertTrue(self.agent_calls, 'a text message stopped reaching the AI')

    async def test_a_voice_note_still_reaches_the_agent(self):
        """The supported media path must be completely unaffected."""
        with patch.object(GW, '_download_whatsapp_voice', return_value=b'\x00\x01'):
            await self.send(a_message(text='[Voice message]', has_media=True,
                                      is_voice=True, voice_media_id='v1',
                                      media_kind=None))
        self.assertTrue(self.agent_calls, 'a voice note was treated as unreadable')

    async def test_a_voice_note_is_not_acknowledged_as_unreadable(self):
        with patch.object(GW, '_download_whatsapp_voice', return_value=b'\x00\x01'):
            adapter, _ = await self.send(a_message(text='[Voice message]',
                                                   has_media=True, is_voice=True,
                                                   voice_media_id='v1',
                                                   media_kind=None))
        self.assertEqual(adapter.sent, [])

    async def test_an_escalated_conversation_still_gets_its_own_notice(self):
        """The escalation acknowledgement runs before this guard and must win."""
        GW._acked.clear()
        adapter, _ = await self.send(
            a_message(),
            inbound={'ok': True, 'lead_id': 42, 'agent_id': 1,
                     'organization_id': 1, 'ai_enabled': False,
                     'escalated': True, 'ignore': True})
        self.assertEqual(adapter.sent, [GW.ESCALATED_ACK])


if __name__ == '__main__':
    unittest.main(verbosity=2)
