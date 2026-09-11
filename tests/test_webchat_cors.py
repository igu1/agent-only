"""Each organization's website must be able to reach its own chat.

`webchat_origins` has been in tenants.json, and documented in
tenants.example.json, since the registry was written - and nothing read it.
CORS came from one global `WEBCHAT_ALLOWED_ORIGINS`, captured once at
startup. Two consequences, both silent:

1. Onboarding is advertised as "append one org + POST /infra/reload-tenants,
   no restart". Routing honoured that; the browser did not. A newly onboarded
   school's chat failed CORS until someone bounced the container - and CORS
   failure has no server-side error at all. The request arrives, the agent
   answers, and the browser throws the reply away.

2. It forced every customer's origin into the shared .env, which is the one
   file that is supposed to hold nothing customer-specific.

The allow-list is now read from the live registry on every request.

    python -m unittest discover -s tests -p "test_webchat_cors.py"
"""
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import infra.tenants as T


A_SITE = 'https://acme-school.example.com'
B_SITE = 'https://beta-school.example.com'
LATE = 'https://onboarded-today.example.com'

ACME = {'backend_url': 'https://acme.example.com', 'api_token': 'tok-acme',
        'webchat_origins': [A_SITE]}
BETA = {'backend_url': 'https://beta.example.com', 'api_token': 'tok-beta',
        'webchat_origins': [B_SITE, 'https://admissions.beta.example.com']}


class RegistryCase(unittest.TestCase):

    def setUp(self):
        p = patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        self.own_the_env()
        self.addCleanup(T.reload_registry)

    def own_the_env(self):
        """`create_app()` calls `load_dotenv(..., override=True)`, so every
        app build re-imposes the developer's .env over whatever a test set.
        This machine's carries `WEBCHAT_ALLOWED_ORIGINS=*`, which allows every
        origin - and made the offboarding assertion unobservable: the removal
        worked, the wildcard just allowed the origin anyway. Take it back
        after each build, and before each registry load."""
        os.environ.pop('WEBCHAT_ALLOWED_ORIGINS', None)

    def write(self, data):
        """Put a registry on disk and point TENANTS_FILE at it."""
        if not hasattr(self, '_path'):
            fd, self._path = tempfile.mkstemp(suffix='.json')
            os.close(fd)
            self.addCleanup(
                lambda: os.path.exists(self._path) and os.remove(self._path))
            p = patch.dict(os.environ, {'TENANTS_FILE': self._path})
            p.start()
            self.addCleanup(p.stop)
        with io.open(self._path, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(data))
        self.own_the_env()
        return T.reload_registry()


class TestTheUnion(RegistryCase):

    def test_every_organizations_origins_are_present(self):
        self.write({'acme': ACME, 'beta': BETA})
        self.assertEqual(
            T.all_webchat_origins(),
            {A_SITE, B_SITE, 'https://admissions.beta.example.com'})

    def test_blanks_and_padding_are_not_origins(self):
        self.write({'acme': {**ACME, 'webchat_origins': ['  ' + A_SITE + ' ', '', '   ']}})
        self.assertEqual(T.all_webchat_origins(), {A_SITE})

    def test_an_org_with_none_contributes_nothing(self):
        self.write({'acme': ACME, 'quiet': {'backend_url': 'x', 'api_token': 'y'}})
        self.assertEqual(T.all_webchat_origins(), {A_SITE})


class TestTheSingleOrgEnvVarStillReachesIt(RegistryCase):
    """Model A sets WEBCHAT_ALLOWED_ORIGINS and has no registry file. It is
    now the DEFAULT TENANT's own origins, so one code path serves both
    deployment models instead of two that can disagree."""

    def test_the_env_var_is_honoured(self):
        with patch.dict(os.environ, {'WEBCHAT_ALLOWED_ORIGINS': A_SITE}):
            os.environ.pop('TENANTS_FILE', None)
            T.reload_registry()
            self.assertIn(A_SITE, T.all_webchat_origins())

    def test_a_comma_separated_list_is_honoured(self):
        with patch.dict(os.environ,
                        {'WEBCHAT_ALLOWED_ORIGINS': '%s, %s' % (A_SITE, B_SITE)}):
            os.environ.pop('TENANTS_FILE', None)
            T.reload_registry()
            self.assertEqual(T.all_webchat_origins(), {A_SITE, B_SITE})

    def test_unset_means_no_origin_is_allowed(self):
        """Not "*" - an agent with nothing configured must not become open."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TENANTS_FILE', None)
            os.environ.pop('WEBCHAT_ALLOWED_ORIGINS', None)
            T.reload_registry()
            self.assertEqual(T.all_webchat_origins(), set())


class TestTheRunningAppFollowsAReload(RegistryCase):
    """The actual defect. Everything below runs against ONE app instance that
    is never restarted, because that is the situation onboarding creates."""

    def app(self):
        from fastapi.testclient import TestClient
        from api.main import create_app
        client = TestClient(create_app())
        self.own_the_env()          # create_app re-read .env over us
        return client

    def preflight(self, client, origin):
        return client.options(
            '/webhooks/acme/webchat/1/session',
            headers={'Origin': origin,
                     'Access-Control-Request-Method': 'POST',
                     'Access-Control-Request-Headers': 'content-type'})

    def test_a_registered_origin_is_allowed(self):
        self.write({'acme': ACME})
        res = self.preflight(self.app(), A_SITE)
        self.assertEqual(res.headers.get('access-control-allow-origin'), A_SITE)

    def test_an_unregistered_origin_is_not(self):
        self.write({'acme': ACME})
        res = self.preflight(self.app(), 'https://somebody-else.example.com')
        self.assertIsNone(res.headers.get('access-control-allow-origin'))

    def test_an_org_ONBOARDED_AFTER_STARTUP_is_allowed(self):
        """No restart. This is what a boot-time allow-list gets wrong, and it
        fails in the browser with nothing in the server log."""
        self.write({'acme': ACME})
        client = self.app()
        self.assertIsNone(
            self.preflight(client, LATE).headers.get('access-control-allow-origin'))

        self.write({'acme': ACME, 'newschool': {'backend_url': 'https://n.example.com',
                                                'api_token': 'tok-new',
                                                'webchat_origins': [LATE]}})
        self.assertEqual(
            self.preflight(client, LATE).headers.get('access-control-allow-origin'),
            LATE, 'a newly onboarded org still cannot be reached from its website')

    def test_an_org_REMOVED_stops_being_allowed(self):
        """Offboarding has to work the same way round."""
        self.write({'acme': ACME, 'beta': BETA})
        client = self.app()
        self.assertEqual(
            self.preflight(client, B_SITE).headers.get('access-control-allow-origin'),
            B_SITE)

        self.write({'acme': ACME})
        self.assertIsNone(
            self.preflight(client, B_SITE).headers.get('access-control-allow-origin'))

    def test_the_reply_to_a_real_request_carries_the_header(self):
        """Preflight is only half of it - the browser also drops the actual
        response unless it names the origin."""
        self.write({'acme': ACME})
        res = self.app().get('/health', headers={'Origin': A_SITE})
        self.assertEqual(res.headers.get('access-control-allow-origin'), A_SITE)

    def test_a_request_with_no_origin_is_untouched(self):
        """WhatsApp and the CRM are server-to-server; CORS must not interfere."""
        self.write({'acme': ACME})
        res = self.app().get('/health')
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.headers.get('access-control-allow-origin'))

    def test_the_agent_never_answers_with_a_wildcard(self):
        self.write({'acme': ACME})
        res = self.preflight(self.app(), A_SITE)
        self.assertNotEqual(res.headers.get('access-control-allow-origin'), '*')


class TestTheWildcardStillWorks(RegistryCase):
    """`WEBCHAT_ALLOWED_ORIGINS=*` is what a local dev .env carries, and the
    old middleware honoured it. Reading the list from the registry instead
    must not quietly turn that into "no origin at all" - that would break the
    dev chat the moment this shipped, with nothing in the log."""

    def app(self):
        from fastapi.testclient import TestClient
        from api.main import create_app
        client = TestClient(create_app())
        self.own_the_env()          # create_app re-read .env over us
        return client

    def test_star_from_the_env_var_allows_any_origin(self):
        with patch.dict(os.environ, {'WEBCHAT_ALLOWED_ORIGINS': '*'}):
            os.environ.pop('TENANTS_FILE', None)
            T.reload_registry()
            res = self.app().get('/health',
                                 headers={'Origin': 'http://localhost:4200'})
        self.assertEqual(res.headers.get('access-control-allow-origin'),
                         'http://localhost:4200')

    def test_star_in_an_org_block_allows_any_origin(self):
        self.write({'acme': {**ACME, 'webchat_origins': ['*']}})
        res = self.app().get('/health', headers={'Origin': 'https://anywhere.example.com'})
        self.assertEqual(res.headers.get('access-control-allow-origin'),
                         'https://anywhere.example.com')

    def test_the_reply_names_the_origin_rather_than_echoing_star(self):
        """Same permission, and it keeps working if credentials are ever
        turned on - a literal "*" would not."""
        self.write({'acme': {**ACME, 'webchat_origins': ['*']}})
        res = self.app().get('/health', headers={'Origin': 'https://anywhere.example.com'})
        self.assertNotEqual(res.headers.get('access-control-allow-origin'), '*')

    def test_a_shared_stack_says_so_out_loud(self):
        """One org's "*" opens web chat for every org on the deployment."""
        with self.assertLogs('infra.tenants', level='WARNING') as logged:
            self.write({'acme': {**ACME, 'webchat_origins': ['*']}, 'beta': BETA})
        self.assertIn('ANY origin', ' '.join(logged.output))

    def test_a_single_org_stack_is_not_nagged(self):
        """There is no second organization to open, so it is just a setting."""
        with patch.object(T.log, 'warning') as warn:
            self.write({'acme': {**ACME, 'webchat_origins': ['*']}})
        warn.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
