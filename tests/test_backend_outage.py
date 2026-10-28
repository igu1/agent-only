"""When the backend is unreachable, hand the message back instead of eating it.

/inbound/ is the call that persists the parent's text - nothing before it
writes anything. Answering HTTP 200 after it failed told Meta the message had
been handled, so it was never sent again and was gone for good. Meta already
retries with backoff; the fix is to stop suppressing that.

The line these tests defend hardest is the difference between "the backend is
down" and "you asked for a channel that does not exist". The first deserves a
retry; the second would become a retry storm.

    python -m unittest discover -s tests -p "test_backend_outage.py"
"""
import unittest
from unittest.mock import patch

import httpx

from fastapi import HTTPException

import api.routes.shared.generic_webhook as GW
import infra.django_api as API


class FakeMessage:
    chat_id = '919876543210'
    user_id = 'wa_919876543210'
    message_id = 'wamid.T1'
    text = 'I want to apply.'
    has_media = False
    is_voice = False
    voice_media_id = None
    sent_at = None
    institution_id = None
    media_kind = None


class FakeAdapter:
    def __init__(self):
        self.sent: list[str] = []

    def get_service(self, channel_id):
        return object()

    def validate(self, **kw):
        return None

    def parse(self, **kw):
        return FakeMessage()

    def should_ignore(self, **kw):
        return False

    def send_typing_indicator(self, **kw):
        return None

    def send_message(self, *, service, message, text):
        self.sent.append(text)
        return True


class WebhookHarness(unittest.IsolatedAsyncioTestCase):
    """One webhook call with the backend's answer canned."""

    def setUp(self):
        self.adapter = FakeAdapter()
        self.agent_calls: list = []
        self.saved: list = []

    async def call(self, inbound):
        with patch.object(GW.registry, 'has', return_value=True), \
             patch.object(GW.registry, 'get', return_value=self.adapter), \
             patch.object(GW, 'django_post', return_value=inbound), \
             patch.object(GW, 'save_message',
                          side_effect=lambda **kw: self.saved.append(kw)), \
             patch.object(GW, 'broadcast_to_conversation_async'), \
             patch.object(GW, 'broadcast_to_org_async'), \
             patch.object(GW, 'enqueue',
                          side_effect=lambda **kw: self.agent_calls.append(kw)), \
             patch.object(GW, 'handle_agent_batch',
                          side_effect=lambda **kw: self.agent_calls.append(kw)):
            return await GW.handle_incoming_webhook(
                channel_type='whatsapp', channel_id='2', payload={}, headers={})

    async def status_for(self, inbound):
        with self.assertRaises(HTTPException) as caught:
            await self.call(inbound)
        return caught.exception.status_code


def down(status):
    """What django_api returns when the backend could not answer."""
    return {'ok': False, 'status': status, 'error': 'boom'}


class TestATemporaryFailureIsHandedBack(WebhookHarness):
    """503 so the channel retries - the message is NOT saved by us here, and
    that is precisely why the channel must send it again."""

    async def test_connection_refused_returns_503(self):
        """django_api maps every transport failure onto 503."""
        self.assertEqual(await self.status_for(down(503)), 503)

    async def test_http_500_returns_503(self):
        self.assertEqual(await self.status_for(down(500)), 503)

    async def test_http_502_returns_503(self):
        self.assertEqual(await self.status_for(down(502)), 503)

    async def test_http_504_returns_503(self):
        self.assertEqual(await self.status_for(down(504)), 503)

    async def test_the_ai_is_not_invoked(self):
        with self.assertRaises(HTTPException):
            await self.call(down(503))
        self.assertEqual(self.agent_calls, [], 'the AI ran without a conversation')

    async def test_nothing_downstream_runs(self):
        with self.assertRaises(HTTPException):
            await self.call(down(500))
        self.assertEqual(self.saved, [])
        self.assertEqual(self.adapter.sent, [],
                         'the parent was answered as though we had taken the message')

    async def test_the_outage_is_logged_with_the_status(self):
        with self.assertLogs('api.routes.shared.generic_webhook', level='WARNING') as log:
            with self.assertRaises(HTTPException):
                await self.call(down(502))
        blob = '\n'.join(log.output)
        self.assertIn('502', blob)
        self.assertIn('retries', blob)

    # THE MESSAGE-LOSS REGRESSION
    async def test_a_real_message_is_not_acknowledged_as_handled(self):
        """Parent: "I want to apply." Backend down. We must NOT answer 200.

        This does not claim we saved the message - we did not, and could not.
        It claims we stopped telling the channel that we had.
        """
        self.assertEqual(FakeMessage.text, 'I want to apply.')
        result = None
        try:
            result = await self.call(down(503))
        except HTTPException as e:
            self.assertEqual(e.status_code, 503)
        else:
            self.fail('the webhook returned %r instead of 503 - the message is lost'
                      % (result,))


class TestAPermanentFailureIsUnchanged(WebhookHarness):
    """Retrying these forever would achieve nothing."""

    async def test_401_still_returns_401(self):
        self.assertEqual(await self.status_for(down(401)), 401)

    async def test_404_still_returns_404(self):
        self.assertEqual(await self.status_for(down(404)), 404)

    async def test_a_business_refusal_still_acknowledges(self):
        """The one that would have become a retry storm: an unknown channel
        answers HTTP 200 with {"ok": false} and NO status key."""
        result = await self.call({'ok': False, 'ignore': True})
        self.assertTrue(result.get('ok'), 'a business refusal now asks for a retry')
        self.assertEqual(self.agent_calls, [])

    async def test_a_400_still_acknowledges(self):
        result = await self.call(down(400))
        self.assertTrue(result.get('ok'))

    async def test_a_403_still_acknowledges(self):
        result = await self.call(down(403))
        self.assertTrue(result.get('ok'))


class TestSuccessIsUntouched(WebhookHarness):

    async def test_a_normal_turn_still_reaches_the_ai(self):
        result = await self.call({'ok': True, 'lead_id': 42, 'agent_id': 1,
                                  'organization_id': 1, 'ai_enabled': True,
                                  'escalated': False, 'ignore': False})
        self.assertTrue(result.get('ok'))
        self.assertTrue(self.agent_calls, 'a healthy turn stopped reaching the AI')

    async def test_an_ignored_turn_still_acknowledges(self):
        result = await self.call({'ok': True, 'lead_id': 42, 'agent_id': 1,
                                  'ai_enabled': True, 'escalated': False,
                                  'ignore': True})
        self.assertTrue(result.get('ok'))


class TestTheClientReportsWhatWentWrong(unittest.TestCase):
    """django_api never raises - it classifies. These pin the mapping the
    webhook now depends on."""

    def _http_error(self, code):
        return httpx.Response(code, content=b'{"error":"x"}')

    def test_a_transport_failure_becomes_503(self):
        with patch.object(API, '_send',
                          side_effect=httpx.ConnectError('refused')):
            with patch.object(API, 'get_tenant',
                              return_value={'backend_url': 'http://x', 'api_token': 't'}):
                r = API.post('/api/agent/v1/inbound/', {'a': 1})
        self.assertFalse(r['ok'])
        self.assertEqual(r['status'], 503)

    def test_http_statuses_are_preserved(self):
        for code in (500, 502, 503, 504):
            with self.subTest(code=code):
                with patch.object(API, '_send', return_value=self._http_error(code)):
                    with patch.object(API, 'get_tenant',
                                      return_value={'backend_url': 'http://x',
                                                    'api_token': 't'}):
                        r = API.post('/api/agent/v1/inbound/', {'a': 1})
                self.assertFalse(r['ok'])
                self.assertEqual(r['status'], code)

    def test_a_get_failure_is_classified_too(self):
        with patch.object(API, '_send',
                          side_effect=httpx.ConnectError('refused')):
            with patch.object(API, 'get_tenant',
                              return_value={'backend_url': 'http://x', 'api_token': 't'}):
                r = API.get('/api/agent/v1/faqs/')
        self.assertEqual(r['status'], 503)

    def test_the_failure_is_logged_with_method_path_and_status(self):
        with patch.object(API, '_send',
                          side_effect=httpx.ConnectError('refused')):
            with patch.object(API, 'get_tenant',
                              return_value={'backend_url': 'http://x', 'api_token': 't'}):
                with self.assertLogs('infra.django_api', level='WARNING') as log:
                    API.post('/api/agent/v1/inbound/', {'a': 1})
        blob = '\n'.join(log.output)
        self.assertIn('POST', blob)
        self.assertIn('/api/agent/v1/inbound/', blob)
        self.assertIn('503', blob)

    def test_the_log_never_carries_the_token_or_the_payload(self):
        """OTP codes and parent contact details travel in these payloads, and
        the token travels in the headers. Neither belongs in a log line."""
        secret_token = 'agw_SUPERSECRET_TOKEN_VALUE'
        payload = {'otp_code': '123456', 'phone_number': '+919876543210',
                   'email': 'parent@example.com'}
        with patch.object(API, '_send',
                          side_effect=httpx.ConnectError('refused')):
            with patch.object(API, 'get_tenant',
                              return_value={'backend_url': 'http://x',
                                            'api_token': secret_token}):
                with self.assertLogs('infra.django_api', level='WARNING') as log:
                    API.post('/api/agent/v1/leads/update/', payload)
        blob = '\n'.join(log.output)
        for secret in (secret_token, '123456', '+919876543210', 'parent@example.com'):
            self.assertNotIn(secret, blob, 'a secret reached the log')

    def test_an_error_body_is_never_logged(self):
        """An unhandled .NET exception page can contain the connection string."""
        leak = b'Server=db;Password=hunter2;'
        err = httpx.Response(500, content=leak)
        with patch.object(API, '_send', return_value=err):
            with patch.object(API, 'get_tenant',
                              return_value={'backend_url': 'http://x', 'api_token': 't'}):
                with self.assertLogs('infra.django_api', level='WARNING') as log:
                    API.post('/api/agent/v1/inbound/', {'a': 1})
        self.assertNotIn('hunter2', '\n'.join(log.output))

    def test_a_successful_call_logs_nothing(self):
        logger = API.logger
        with patch.object(API, '_send', return_value=httpx.Response(200, content=b'{"ok": true}')):
            with patch.object(API, 'get_tenant',
                              return_value={'backend_url': 'http://x', 'api_token': 't'}):
                with patch.object(logger, 'warning') as warned:
                    r = API.post('/api/agent/v1/inbound/', {'a': 1})
        self.assertTrue(r['ok'])
        warned.assert_not_called()


class TestTheClientOutlivesTheServer(unittest.TestCase):
    """If the client gives up first, an inquiry can be created after Python has
    written the request off - and a caller that cannot tell what happened
    cannot safely retry. So the HTTP timeout must exceed the SQL command
    timeout the backend allows itself."""

    def _command_timeout(self):
        import io as _io, json as _json, os as _os
        path = _os.getenv(
            'ITEST_APPSETTINGS',
            'D:/Campus_Angular_Project/OnlineCampusAngular/server/src/OnlineCampus.Api/appsettings.json')
        if not _os.path.exists(path):
            self.skipTest('appsettings.json not reachable from here')
        with _io.open(path, encoding='utf-8-sig') as fh:
            cs = list((_json.load(fh).get('ConnectionStrings') or {}).values())[0]
        for part in cs.split(';'):
            if part.strip().lower().startswith('command timeout'):
                return int(part.split('=', 1)[1])
        self.skipTest('no Command Timeout in the connection string')

    def test_the_http_timeout_exceeds_the_sql_command_timeout(self):
        self.assertGreater(API.REQUEST_TIMEOUT_SECONDS, self._command_timeout(),
                           'the client would abandon work the server is still '
                           'allowed to finish')

    def test_the_timeout_is_the_agreed_value(self):
        self.assertEqual(API.REQUEST_TIMEOUT_SECONDS, 35)


if __name__ == '__main__':
    unittest.main(verbosity=2)