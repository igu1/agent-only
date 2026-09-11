"""The registry must load organizations, and ONLY organizations.

The guard used to be `isinstance(cfg, dict)`, which is true of a nested
comment block as well as a tenant. tenants.example.json documents its own
fields in exactly that shape, so shipping it loaded five "organizations"
instead of two: /infra/reload-tenants reported the wrong count, and
/webhooks/_field_notes/webchat/... answered instead of returning 404.

A leading underscore now marks documentation. These tests keep the example
file loadable and keep the count honest - the count is what a deploy team
reads back to confirm an onboarding worked.

    python -m unittest discover -s tests -p "test_tenant_registry.py"
"""
import io
import json
import os
import tempfile
import unittest

import infra.tenants as T


class RegistryCase(unittest.TestCase):

    def load(self, data):
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        with io.open(path, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(data))
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        old = os.environ.get('TENANTS_FILE')
        os.environ['TENANTS_FILE'] = path
        self.addCleanup(lambda: os.environ.__setitem__('TENANTS_FILE', old)
                        if old is not None else os.environ.pop('TENANTS_FILE', None))
        return T.reload_registry()


ORG = {'backend_url': 'https://x.example.com', 'api_token': 'tok'}


class TestDocumentationIsNotATenant(RegistryCase):

    def test_a_string_comment_is_skipped(self):
        self.load({'_comment': 'hello', 'acme': ORG})
        self.assertFalse(T.is_known_org('_comment'))

    def test_a_NESTED_comment_is_skipped(self):
        """The actual bug: a dict-shaped note passed the isinstance guard."""
        self.load({'_field_notes': {'backend_url': 'explanation'}, 'acme': ORG})
        self.assertFalse(T.is_known_org('_field_notes'),
                         'a comment block was served as an organization')

    def test_the_count_is_honest(self):
        """What an operator reads back after onboarding: two orgs + default."""
        self.assertEqual(
            self.load({'_comment': 'x', '_notes': {'a': 'b'},
                       'acme': ORG, 'beta': ORG}), 3)

    def test_real_orgs_still_load(self):
        self.load({'_notes': {'a': 'b'}, 'acme': ORG})
        self.assertTrue(T.is_known_org('acme'))


class TestTheShippedExampleIsUsable(RegistryCase):
    """The file a deploy team copies must load as written."""

    def example(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'tenants.example.json')
        if not os.path.exists(path):
            self.skipTest('tenants.example.json not found')
        return json.load(io.open(path, encoding='utf-8'))

    def test_it_is_valid_json(self):
        self.assertIsInstance(self.example(), dict)

    def test_it_loads_exactly_its_two_organizations(self):
        self.assertEqual(self.load(self.example()), 3)   # + default

    def test_both_documented_orgs_resolve(self):
        self.load(self.example())
        for org in ('sanabl', 'alnoor'):
            with self.subTest(org=org):
                self.assertTrue(T.is_known_org(org))

    def test_the_orgs_are_genuinely_separate(self):
        """The whole point of the registry: one org cannot reach another's
        backend, token or documents."""
        self.load(self.example())
        seen = {}
        for org in ('sanabl', 'alnoor'):
            T.set_org(org)
            seen[org] = T.get_tenant()
        a, b = seen['sanabl'], seen['alnoor']
        for field in ('backend_url', 'api_token', 'qdrant_prefix'):
            with self.subTest(field=field):
                self.assertNotEqual(a[field], b[field],
                                    '%s is shared between organizations' % field)

    def test_phone_number_ids_are_strings(self):
        """Meta sends them as strings; an int here would never match."""
        self.load(self.example())
        T.set_org('alnoor')
        ids = T.get_tenant()['phone_number_ids']
        self.assertTrue(ids)
        for pnid in ids:
            self.assertIsInstance(pnid, str)


class TestChannelNamesAreReserved(RegistryCase):
    """Each is a literal segment in an org-prefixed URL
    (/webhooks/<org>/whatsapp/<id>/), so an org with that key would make the
    route ambiguous."""

    def test_none_of_them_can_be_an_org(self):
        for name in ('webchat', 'whatsapp', 'telegram', 'instagram'):
            with self.subTest(name=name):
                self.load({name: ORG, 'acme': ORG})
                self.assertFalse(T.is_known_org(name),
                                 '%r was accepted as an organization' % name)

    def test_a_normal_org_alongside_them_still_loads(self):
        self.load({'whatsapp': ORG, 'acme': ORG})
        self.assertTrue(T.is_known_org('acme'))


class TestTheDefaultAlwaysExists(RegistryCase):

    def test_default_survives_a_registry(self):
        """It is the fallback when no org can be resolved - losing it would
        make an unroutable request fail in a far less obvious way."""
        self.load({'acme': ORG})
        self.assertTrue(T.is_known_org('default'))

    def test_webchat_can_never_be_an_org_key(self):
        """It would collide with the non-org webchat route."""
        self.load({'webchat': ORG, 'acme': ORG})
        self.assertFalse(T.is_known_org('webchat'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
