"""What the channel adapters report back when a send does or does not go.

This layer had no tests at all, which is how `_ = e` survived: the adapter
caught every failure - a 131047 outside Meta's 24-hour window, an expired
token, an unreachable API - and returned the same `None` it returns on success.
Callers could not tell the difference, so they recorded every message as
delivered.

No network here. The service is a stub raising exactly what
`services/whatsapp.py:_post` raises, and one test drives that real `_post`
with urlopen patched, so the RuntimeError contract itself is pinned.

    python -m unittest discover -s tests -p "test_channel_send.py"
"""
import logging
import unittest
import urllib.error
from unittest.mock import patch

from api.routes.shared.channel_adapters import (
    IncomingMessage,
    InstagramAdapter,
    TelegramAdapter,
    WhatsAppAdapter,
)


def a_message(chat_id='+919876543210'):
    return IncomingMessage(chat_id=chat_id, user_id='u', text='hi', message_id='m1')


class Service:
    """Stands in for the channel service; records or raises."""

    def __init__(self, error=None):
        self.error = error
        self.sent: list[tuple] = []

    def send_message(self, to, text):
        if self.error:
            raise self.error
        self.sent.append((to, text))
        return {'messages': [{'id': 'wamid.OK'}]}


class TestWhatsAppReportsDelivery(unittest.TestCase):

    def test_a_delivered_message_reports_true(self):
        service = Service()
        ok = WhatsAppAdapter().send_message(
            service=service, message=a_message(), text='hello')
        self.assertIs(ok, True)
        self.assertEqual(service.sent, [('919876543210', 'hello')],
                         'the leading + must be stripped for the Graph API')

    def test_a_meta_error_reports_false(self):
        """131047: free-form text outside the 24-hour service window - the
        commonest real failure, and previously invisible."""
        service = Service(RuntimeError(
            'WhatsApp request failed: HTTP 400 {"error":{"code":131047}}'))
        with self.assertLogs('api.routes.shared.channel_adapters', level='ERROR'):
            ok = WhatsAppAdapter().send_message(
                service=service, message=a_message(), text='hello')
        self.assertIs(ok, False)

    def test_a_network_failure_reports_false(self):
        service = Service(RuntimeError(
            'WhatsApp request failed: <urlopen error timed out>'))
        with self.assertLogs('api.routes.shared.channel_adapters', level='ERROR'):
            ok = WhatsAppAdapter().send_message(
                service=service, message=a_message(), text='hello')
        self.assertIs(ok, False)

    def test_the_failure_is_logged_with_the_chat_it_belonged_to(self):
        """Staff investigating silence need to know WHICH conversation."""
        service = Service(RuntimeError('WhatsApp request failed: HTTP 401'))
        with self.assertLogs('api.routes.shared.channel_adapters',
                             level='ERROR') as captured:
            WhatsAppAdapter().send_message(
                service=service, message=a_message(), text='hello')
        blob = '\n'.join(captured.output)
        self.assertIn('919876543210', blob)
        self.assertIn('HTTP 401', blob, 'the diagnostics were thrown away again')

    def test_an_unexpected_error_still_reports_false(self):
        """Anything at all - the send must never raise into the turn."""
        service = Service(ValueError('something else entirely'))
        with self.assertLogs('api.routes.shared.channel_adapters', level='ERROR'):
            ok = WhatsAppAdapter().send_message(
                service=service, message=a_message(), text='hello')
        self.assertIs(ok, False)


class TestTheOtherAdaptersReportToo(unittest.TestCase):
    """Telegram and Instagram had the identical swallowed shape."""

    def test_telegram_reports_true_and_false(self):
        self.assertIs(TelegramAdapter().send_message(
            service=Service(), message=a_message(chat_id='12345'), text='x'), True)
        with self.assertLogs('api.routes.shared.channel_adapters', level='ERROR'):
            self.assertIs(TelegramAdapter().send_message(
                service=Service(RuntimeError('down')),
                message=a_message(chat_id='12345'), text='x'), False)

    def test_instagram_reports_true_and_false(self):
        self.assertIs(InstagramAdapter().send_message(
            service=Service(), message=a_message(chat_id='ig1'), text='x'), True)
        with self.assertLogs('api.routes.shared.channel_adapters', level='ERROR'):
            self.assertIs(InstagramAdapter().send_message(
                service=Service(RuntimeError('down')),
                message=a_message(chat_id='ig1'), text='x'), False)


class TestTheServiceRaisesWhatTheAdapterCatches(unittest.TestCase):
    """Pins the contract between the two layers, without touching the network."""

    def service(self):
        # the constructor only reads channel_config - the individual kwargs
        # are inert, which is how production builds it from the DB row
        from services.whatsapp import WhatsAppService
        return WhatsAppService(
            channel_config={'phone_number_id': '1', 'access_token': 't'},
            api_base='http://example.invalid')

    def test_an_http_error_becomes_a_runtime_error_carrying_the_status(self):
        err = urllib.error.HTTPError(
            'u', 400, 'Bad Request', {},
            __import__('io').BytesIO(b'{"error":{"code":131047}}'))
        with patch('urllib.request.urlopen', side_effect=err):
            with self.assertRaises(RuntimeError) as caught:
                self.service().send_message('919876543210', 'hi')
        self.assertIn('400', str(caught.exception))
        self.assertIn('131047', str(caught.exception))

    def test_a_url_error_becomes_a_runtime_error(self):
        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.URLError('timed out')):
            with self.assertRaises(RuntimeError) as caught:
                self.service().send_message('919876543210', 'hi')
        self.assertIn('timed out', str(caught.exception))

    def test_the_adapter_turns_that_contract_into_false(self):
        """End to end across the seam: a failing HTTP call reaches the adapter
        as a RuntimeError and leaves as False."""
        service = self.service()
        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.URLError('unreachable')):
            with self.assertLogs('api.routes.shared.channel_adapters', level='ERROR'):
                ok = WhatsAppAdapter().send_message(
                    service=service, message=a_message(), text='hello')
        self.assertIs(ok, False)


if __name__ == '__main__':
    unittest.main(verbosity=2)
