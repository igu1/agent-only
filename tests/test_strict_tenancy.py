"""Under SaaS, "which organization?" must never be answered by guessing.

get_tenant() ends in `_registry.get(org) or _registry[DEFAULT_ORG]`. That line
kept a registry-less deployment working, and for one organization it is
correct. Once several share one stack it turns every failure to identify a
tenant into a quiet success: the request is served from whatever .env points
at. A WhatsApp number missing from tenants.json would create that parent's
inquiry in ANOTHER company's database - no error, no log line, and the CRM it
landed in looks entirely normal.

STRICT_TENANCY refuses instead, at the entry points, where the answer can still
be a status code the caller can act on. Off by default, so a single-org
deployment behaves exactly as before - that is half of what these tests pin
down. The other half: with it on, every path that could reach the default
tenant is closed, including the two that carry no organization at all (Meta's
verification GET, and Telegram, which has no org-prefixed route).

    python -m unittest discover -s tests -p "test_strict_tenancy.py"
"""
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import api.routes.msgchannels.telegram as TG
import api.routes.msgchannels.webchat as WC
import api.routes.msgchannels.whatsapp as WA
import infra.tenants as T
from api.routes.shared.admin_auth import require_org_token
from api.routes.shared.channel_registry import require_tenant


ACME = {'backend_url': 'https://acme.example.com', 'api_token': 'tok-acme',
        'phone_number_ids': ['111222333']}
BETA = {'backend_url': 'https://beta.example.com', 'api_token': 'tok-beta',
        'phone_number_ids': ['999888777']}


class FakeRequest:
    """Enough of a Request for a handler that only reads the body."""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def wa_payload(pnid):
    return {'entry': [{'changes': [{'value': {
        'metadata': {'phone_number_id': pnid}}}]}]}


class TenancyCase(unittest.IsolatedAsyncioTestCase):
    """Two real organizations loaded, and both context vars reset - a tenant
    resolved by one test must not silently satisfy the next one's guard."""

    strict = True

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        with io.open(path, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps({'acme': ACME, 'beta': BETA}))
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        env = {'TENANTS_FILE': path}
        if self.strict:
            env['STRICT_TENANCY'] = 'true'
        p = patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        if not self.strict:
            os.environ.pop('STRICT_TENANCY', None)
        T.reload_registry()

        T._current_org.set(T.DEFAULT_ORG)
        T._org_resolved.set(False)

    def assertRefused(self, cm):
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn('organization', str(cm.exception.detail).lower())

    def webchat_patches(self):
        return (patch.object(WC, '_channel_agent_id', return_value=0),
                patch.object(WC, 'build_institution_choices', return_value=[]),
                patch.object(WC, 'build_introduction', return_value='welcome'),
                patch.object(WC, 'build_institution_prompt', return_value='which?'))


# -- the primitives -------------------------------------------------------

class TestResolutionIsTracked(TenancyCase):
    """get_org() cannot answer "was I told?" - it reads 'default' both when
    nobody said and when somebody said 'default'."""

    def test_a_fresh_request_has_not_resolved_one(self):
        self.assertFalse(T.org_resolved())

    def test_set_org_marks_it_resolved(self):
        T.set_org('acme')
        self.assertTrue(T.org_resolved())

    def test_even_choosing_the_default_counts_as_choosing(self):
        T.set_org('default')
        self.assertTrue(T.org_resolved())


class TestTheSwitch(TenancyCase):
    strict = False

    def test_off_by_default(self):
        self.assertFalse(T.strict_tenancy())

    def test_the_usual_spellings_all_turn_it_on(self):
        for value in ('1', 'true', 'TRUE', 'yes', 'on', ' true '):
            with self.subTest(value=value):
                with patch.dict(os.environ, {'STRICT_TENANCY': value}):
                    self.assertTrue(T.strict_tenancy())

    def test_anything_else_leaves_it_off(self):
        for value in ('', '0', 'false', 'no', 'maybe'):
            with self.subTest(value=value):
                with patch.dict(os.environ, {'STRICT_TENANCY': value}):
                    self.assertFalse(T.strict_tenancy())


class TestTheFallbackIsNoLongerSilent(TenancyCase):
    """It still falls back rather than raising - this is called mid-turn, where
    an exception surfaces as a 500 long after the request could have been
    refused cleanly. But an unidentified request being served from whatever
    .env points at is exactly what belongs in a log."""

    def test_an_unregistered_org_is_logged(self):
        T.set_org('nobody')
        with self.assertLogs('infra.tenants', level='WARNING') as logged:
            T.get_tenant()
        self.assertIn('nobody', ' '.join(logged.output))

    def test_it_still_returns_a_usable_config(self):
        T.set_org('nobody')
        self.assertIn('backend_url', T.get_tenant())

    def test_a_real_org_logs_nothing(self):
        T.set_org('acme')
        with patch.object(T.log, 'warning') as warn:
            self.assertEqual(T.get_tenant()['api_token'], 'tok-acme')
        warn.assert_not_called()

    def test_being_served_the_default_WITHOUT_asking_is_logged(self):
        """The case a registry lookup can never report, and the only one that
        actually loses data: nobody said which org, so get_org() reads
        'default', the registry HAS a default, and the request succeeds - in
        the wrong company's database. Nothing was missing; nothing failed."""
        with self.assertLogs('infra.tenants', level='WARNING') as logged:
            T.get_tenant()
        self.assertIn('named no organization', ' '.join(logged.output))

    def test_choosing_the_default_deliberately_is_not_a_warning(self):
        T.set_org('default')
        with patch.object(T.log, 'warning') as warn:
            T.get_tenant()
        warn.assert_not_called()


class TestASingleTenantDeploymentIsNotNagged(TenancyCase):
    """With no other organization to have belonged to, serving the default IS
    the right answer - warning on every turn would be noise, and noise is how
    a real warning gets ignored."""

    strict = False

    def setUp(self):
        super().setUp()
        os.environ.pop('TENANTS_FILE', None)
        T.reload_registry()
        T._current_org.set(T.DEFAULT_ORG)
        T._org_resolved.set(False)

    def test_an_unresolved_request_logs_nothing(self):
        with patch.object(T.log, 'warning') as warn:
            T.get_tenant()
        warn.assert_not_called()


# -- web chat -------------------------------------------------------------

class TestWebchatNeedsAnOrg(TenancyCase):

    async def test_minting_a_session_is_refused(self):
        """The greeting is built from the org's own schools and documents."""
        with self.assertRaises(HTTPException) as cm:
            await WC.webchat_session('1')
        self.assertRefused(cm)

    async def test_the_introduction_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            await WC.webchat_introduction('1')
        self.assertRefused(cm)

    async def test_sending_a_message_is_refused(self):
        """The one that writes an inquiry row."""
        with self.assertRaises(HTTPException) as cm:
            await WC.webchat_message('1', FakeRequest({'text': 'hi'}))
        self.assertRefused(cm)

    async def test_polling_replies_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            await WC.webchat_messages('1', session_id='s', after_id=0)
        self.assertRefused(cm)

    async def test_the_refusal_names_the_url_that_would_work(self):
        with self.assertRaises(HTTPException) as cm:
            await WC.webchat_session('1')
        self.assertIn('/webhooks/<org>/webchat/', str(cm.exception.detail))


class TestTheOrgPrefixedRouteSatisfiesIt(TenancyCase):

    async def test_a_session_is_minted(self):
        a, b, c, d = self.webchat_patches()
        with a, b, c, d:
            res = await WC.webchat_session_org('acme', '1')
        self.assertTrue(res['ok'])

    async def test_and_it_is_that_org_being_served(self):
        a, b, c, d = self.webchat_patches()
        with a, b, c, d:
            await WC.webchat_session_org('beta', '1')
        self.assertEqual(T.get_tenant()['api_token'], 'tok-beta')

    async def test_an_unknown_org_is_404_not_400(self):
        """A different failure: the URL named one, it just does not exist."""
        with self.assertRaises(HTTPException) as cm:
            await WC.webchat_session_org('who', '1')
        self.assertEqual(cm.exception.status_code, 404)

    async def test_a_message_goes_through(self):
        a, b, c, d = self.webchat_patches()
        with a, b, c, d:
            with patch.object(WC, 'handle_incoming_webhook',
                              return_value={'ok': True}) as fwd:
                await WC.webchat_message_org('acme', '1', FakeRequest({'text': 'hi'}))
        fwd.assert_called_once()


class TestSingleOrgDeploymentsAreUntouched(TenancyCase):
    """Strict mode is opt-in. With it off the bare routes behave exactly as
    they did - this is what makes the change safe to ship turned off."""

    strict = False

    async def test_a_bare_session_still_works(self):
        a, b, c, d = self.webchat_patches()
        with a, b, c, d:
            self.assertTrue((await WC.webchat_session('1'))['ok'])

    async def test_a_bare_message_still_works(self):
        a, b, c, d = self.webchat_patches()
        with a, b, c, d:
            with patch.object(WC, 'handle_incoming_webhook',
                              return_value={'ok': True}) as fwd:
                await WC.webchat_message('1', FakeRequest({'text': 'hi'}))
        fwd.assert_called_once()

    async def test_telegram_still_works(self):
        with patch.object(TG, 'handle_incoming_webhook',
                          return_value={'ok': True}) as fwd:
            await TG.telegram_webhook(FakeRequest({'message': {}}), '1', None)
        fwd.assert_called_once()

    async def test_a_bare_whatsapp_message_still_works(self):
        with patch.object(WA, 'handle_incoming_webhook',
                          return_value={'ok': True}) as fwd:
            with patch.object(WA.registry, 'get'):
                await WA.whatsapp_webhook_by_channel(
                    '1', FakeRequest(wa_payload('000')))
        fwd.assert_called_once()


# -- WhatsApp -------------------------------------------------------------

class TestWhatsappVerification(TenancyCase):
    """The gap that blocked SaaS onboarding outright: Meta's GET carries no
    payload, so on the bare route there is nothing at all to resolve."""

    def service(self):
        class S:
            @staticmethod
            def validate_webhook(mode, token, challenge):
                return challenge
        return S()

    async def test_the_bare_route_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            await WA.whatsapp_webhook_verify_by_channel(
                '1', hub_mode='subscribe', hub_verify_token='t',
                hub_challenge='CHALLENGE')
        self.assertRefused(cm)

    async def test_it_is_refused_BEFORE_reading_a_database(self):
        """_get_service reads the tenant's CRM - by then the wrong one has
        already been chosen."""
        with patch.object(WA, '_get_service') as svc:
            with self.assertRaises(HTTPException):
                await WA.whatsapp_webhook_verify_by_channel(
                    '1', hub_mode='subscribe', hub_verify_token='t',
                    hub_challenge='C')
        svc.assert_not_called()

    async def test_the_org_prefixed_route_verifies(self):
        with patch.object(WA, '_get_service', return_value=self.service()):
            res = await WA.whatsapp_webhook_verify_by_org(
                'acme', '1', hub_mode='subscribe', hub_verify_token='t',
                hub_challenge='CHALLENGE')
        self.assertEqual(res.body, b'CHALLENGE')

    async def test_and_it_verified_as_that_org(self):
        with patch.object(WA, '_get_service', return_value=self.service()):
            await WA.whatsapp_webhook_verify_by_org(
                'beta', '1', hub_mode='subscribe', hub_verify_token='t',
                hub_challenge='C')
        self.assertEqual(T.get_tenant()['api_token'], 'tok-beta')


class TestWhatsappMessages(TenancyCase):

    async def test_a_registered_number_resolves_its_own_org(self):
        with patch.object(WA, 'handle_incoming_webhook',
                          return_value={'ok': True}) as fwd:
            with patch.object(WA.registry, 'get'):
                await WA.whatsapp_webhook_by_channel(
                    '1', FakeRequest(wa_payload('999888777')))
        fwd.assert_called_once()
        self.assertEqual(T.get_tenant()['api_token'], 'tok-beta')

    async def test_an_UNREGISTERED_number_is_refused(self):
        """The quiet one: this parent's inquiry would be filed in whichever
        company the default tenant points at."""
        with patch.object(WA, 'handle_incoming_webhook') as fwd:
            with self.assertRaises(HTTPException) as cm:
                await WA.whatsapp_webhook_by_channel(
                    '1', FakeRequest(wa_payload('000000000')))
        self.assertRefused(cm)
        fwd.assert_not_called()

    async def test_the_refusal_is_not_swallowed_into_an_ok(self):
        """The handler answers {"ok": true} to every other exception so Meta
        stops retrying. Acknowledging THIS one would drop the message."""
        with self.assertRaises(HTTPException):
            await WA.whatsapp_webhook_by_channel('1', FakeRequest(wa_payload('')))

    async def test_the_org_prefixed_route_carries_it(self):
        with patch.object(WA, 'handle_incoming_webhook',
                          return_value={'ok': True}) as fwd:
            with patch.object(WA.registry, 'get'):
                await WA.whatsapp_webhook_by_org(
                    'acme', '1', FakeRequest(wa_payload('')))
        fwd.assert_called_once()
        self.assertEqual(T.get_tenant()['api_token'], 'tok-acme')


# -- Telegram: kept, but it cannot name an organization -------------------

class TestTelegramIsRefusedUnderStrict(TenancyCase):
    """No org-prefixed route and nothing in the payload that names a tenant.
    The code stays - the channel is simply unusable while strict mode is on,
    which is the honest outcome: the alternative is filing its messages
    against the default organization."""

    async def test_it_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            await TG.telegram_webhook(FakeRequest({'message': {}}), '1', None)
        self.assertRefused(cm)

    async def test_the_message_says_why_rather_than_offering_a_url(self):
        with self.assertRaises(HTTPException) as cm:
            await TG.telegram_webhook(FakeRequest({'message': {}}), '1', None)
        detail = str(cm.exception.detail)
        self.assertIn('no org-prefixed route', detail)
        self.assertNotIn('/webhooks/<org>/telegram', detail)

    async def test_nothing_is_forwarded(self):
        with patch.object(TG, 'handle_incoming_webhook') as fwd:
            with self.assertRaises(HTTPException):
                await TG.telegram_webhook(FakeRequest({'message': {}}), '1', None)
        fwd.assert_not_called()


# -- admin ----------------------------------------------------------------

class TestReindexingNeedsToKnowWhose(TenancyCase):
    """The operator token authenticates but names no organization. Reindexing
    writes to <qdrant_prefix>-<agent_id>, so it would rebuild the default
    tenant's collections while the caller meant their own."""

    def test_the_operator_token_alone_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            require_org_token()
        self.assertEqual(cm.exception.status_code, 400)

    def test_an_org_token_satisfies_it(self):
        T.set_org('acme')
        require_org_token()

    def test_the_message_points_at_tenants_json(self):
        with self.assertRaises(HTTPException) as cm:
            require_org_token()
        self.assertIn('tenants.json', str(cm.exception.detail))


class TestAdminIsUnchangedWhenStrictIsOff(TenancyCase):
    strict = False

    def test_the_operator_token_still_works(self):
        require_org_token()

    def test_and_so_does_a_channel_with_no_org(self):
        require_tenant('webchat')


if __name__ == '__main__':
    unittest.main(verbosity=2)
