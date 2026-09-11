"""Read the CRM's own configuration: database connection and agent API token.

The tests assert on rows the gateway wrote, so they must look at the database
the gateway is actually using. Reading appsettings.json rather than keeping a
second copy of the string is what stops the two drifting apart - and the
credentials go straight to the driver, never to stdout.

The stored string is Microsoft.Data.SqlClient syntax. ODBC spells several keys
differently and wants yes/no where .NET writes True/False, so it is translated
rather than passed through.
"""
import io
import json
import os
import sys

APPSETTINGS = os.getenv(
    'ITEST_APPSETTINGS',
    r'D:\Campus_Angular_Project\OnlineCampusAngular\server\src\OnlineCampus.Api\appsettings.json')

_YES = ('true', 'yes', '1')


def _pyodbc():
    """pyodbc is a test-only dependency, so it may not be in the venv."""
    extra = os.getenv('ITEST_PYODBC_PATH')
    if extra and extra not in sys.path:
        sys.path.insert(0, extra)
    try:
        import pyodbc
    except ImportError:
        raise unittest_skip(
            'pyodbc is not installed. `pip install pyodbc`, or set '
            'ITEST_PYODBC_PATH to a directory that already contains it.')
    return pyodbc


def unittest_skip(reason):
    import unittest
    return unittest.SkipTest(reason)


def _config():
    with io.open(APPSETTINGS, encoding='utf-8-sig') as fh:
        return json.load(fh)


def _pairs():
    cfg = _config()
    cs = cfg.get('ConnectionStrings') or {}
    if not cs:
        raise unittest_skip('%s has no ConnectionStrings section' % APPSETTINGS)
    raw = list(cs.values())[0]
    out = {}
    for kv in raw.split(';'):
        if '=' in kv:
            k, v = kv.split('=', 1)
            out[k.strip().lower()] = v.strip()
    return out


def odbc_connection_string():
    p = _pairs()
    parts = ['DRIVER={ODBC Driver 17 for SQL Server}',
             'SERVER=%s' % (p.get('server') or p.get('data source') or ''),
             'DATABASE=%s' % (p.get('database') or p.get('initial catalog') or '')]

    if str(p.get('integrated security', '')).lower() in _YES + ('sspi',):
        parts.append('Trusted_Connection=yes')
    else:
        parts.append('UID=%s' % (p.get('user id') or p.get('uid') or ''))
        parts.append('PWD=%s' % (p.get('password') or p.get('pwd') or ''))

    enc = str(p.get('encrypt', '')).lower()
    if enc:
        parts.append('Encrypt=%s' % ('yes' if enc in _YES else 'no'))
    trust = str(p.get('trustservercertificate', 'true')).lower()
    parts.append('TrustServerCertificate=%s' % ('yes' if trust in _YES else 'no'))
    return ';'.join(parts)


def api_token():
    """The shared secret the gateway checks in X-Agent-Token.

    Read from the API's config rather than the agent's .env so a token rotated
    on one side cannot leave the tests authenticating against the other.
    """
    return ((_config().get('AgentGateway') or {}).get('ApiToken') or '').strip()


def database_name():
    p = _pairs()
    return p.get('database') or p.get('initial catalog') or '?'


def connect():
    return _pyodbc().connect(odbc_connection_string(), timeout=20)
