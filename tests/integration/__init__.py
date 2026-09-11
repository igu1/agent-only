"""Integration tests: a running .NET API and the real TestAzhar database.

Excluded from `unittest discover tests` on purpose - these need infrastructure
and write rows to a shared database, so a plain suite run must not start them
by accident. Run them by name instead:

    python -m unittest tests.integration.test_confirmation_gate -v
"""


def load_tests(loader, standard_tests, pattern):
    """Discovery protocol: contribute nothing.

    Naming a module directly does not go through this hook, so the explicit
    command above still loads and runs everything here.
    """
    return loader.suiteClass()
