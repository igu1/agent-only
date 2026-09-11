"""mem/db.sqlite3 is a cache, and these are the tests that keep it one.

Nothing in agno or in this codebase ever expired a session, so the file grew for
the life of the deployment: measured at ~180 KB per conversation, which is ~3 GB
a year at fifty chats a day, and every one of those rows holds a parent's name,
number and email in an unencrypted file.

Dropping them is safe precisely because none of it is a record: the messages are
in CmpAgentMessage and the inquiry in CmpRegistration, both on SQL Server. The
worst case is an assistant that has forgotten a conversation nobody has touched
in a week.

    python -m unittest discover -s tests -p "test_session_retention.py"
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from infra.session_retention import (
    DEFAULT_RETENTION_DAYS, VACUUM_WHEN_FREE_FRACTION, retention_days,
    sweep_sessions,
)


class SweepCase(unittest.TestCase):
    """A throwaway database shaped like agno's."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.sqlite3')
        os.close(fd)
        conn = sqlite3.connect(self.path)
        conn.execute("""CREATE TABLE agno_sessions (
                            session_id TEXT, runs TEXT,
                            created_at INTEGER, updated_at INTEGER)""")
        conn.execute("""CREATE TABLE agno_memories (
                            memory_id TEXT, memory TEXT, updated_at INTEGER)""")
        conn.commit()
        conn.close()

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def add(self, session_id, days_ago, *, updated=True):
        conn = sqlite3.connect(self.path)
        conn.execute("SELECT CAST(strftime('%s','now',?) AS INTEGER)",
                     ('-%d days' % days_ago,))
        stamp = conn.execute("SELECT CAST(strftime('%s','now',?) AS INTEGER)",
                             ('-%d days' % days_ago,)).fetchone()[0]
        conn.execute('INSERT INTO agno_sessions VALUES (?,?,?,?)',
                     (session_id, 'x' * 100,
                      stamp, stamp if updated else None))
        conn.commit()
        conn.close()

    def ids(self):
        conn = sqlite3.connect(self.path)
        rows = [r[0] for r in conn.execute('SELECT session_id FROM agno_sessions')]
        conn.close()
        return sorted(rows)


class TestOnlyStaleSessionsGo(SweepCase):

    def test_an_old_session_is_removed(self):
        self.add('old', days_ago=30)
        self.assertEqual(sweep_sessions(self.path, days=7), 1)
        self.assertEqual(self.ids(), [])

    def test_a_live_session_is_kept(self):
        """The one thing this must never do is forget an ongoing chat."""
        self.add('today', days_ago=0)
        self.assertEqual(sweep_sessions(self.path, days=7), 0)
        self.assertEqual(self.ids(), ['today'])

    def test_the_boundary_is_respected(self):
        self.add('three', days_ago=3)
        self.add('ten', days_ago=10)
        sweep_sessions(self.path, days=7)
        self.assertEqual(self.ids(), ['three'])

    def test_a_session_with_no_updated_at_falls_back_to_created(self):
        """agno leaves updated_at null until the second turn - a one-message
        chat must still age out rather than live forever."""
        self.add('once', days_ago=30, updated=False)
        sweep_sessions(self.path, days=7)
        self.assertEqual(self.ids(), [])

    def test_memories_are_left_alone(self):
        """Long-term facts about a parent, tiny, and the one thing here that is
        not a replay of something already on SQL Server."""
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO agno_memories VALUES ('m1','has two children',0)")
        conn.commit()
        conn.close()
        sweep_sessions(self.path, days=7)
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM agno_memories').fetchone()[0], 1)
        conn.close()


class TestItNeverBreaksStartup(SweepCase):

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(sweep_sessions(self.path + '.nope'), 0)

    def test_a_database_without_the_table_is_not_an_error(self):
        """First run: agno creates the tables after this has already swept."""
        conn = sqlite3.connect(self.path)
        conn.execute('DROP TABLE agno_sessions')
        conn.commit()
        conn.close()
        self.assertEqual(sweep_sessions(self.path, days=7), 0)

    def test_a_corrupt_file_is_not_an_error(self):
        with open(self.path, 'wb') as fh:
            fh.write(b'not a database')
        self.assertEqual(sweep_sessions(self.path, days=7), 0)

    def test_it_says_what_it_removed(self):
        self.add('old', days_ago=30)
        with self.assertLogs('infra.session_retention', level='INFO') as log:
            sweep_sessions(self.path, days=7)
        self.assertIn('removed 1 session', '\n'.join(log.output))


class TestTheFileActuallyShrinks(SweepCase):
    """Deleting rows is not the same as reclaiming disk. SQLite parks freed
    pages on a freelist and reuses them, so without a compaction the file sits
    at its high-water mark for ever - which is most of what this module exists
    to prevent."""

    def bulk(self, count, days_ago):
        conn = sqlite3.connect(self.path)
        stamp = conn.execute("SELECT CAST(strftime('%s','now',?) AS INTEGER)",
                             ('-%d days' % days_ago,)).fetchone()[0]
        conn.executemany('INSERT INTO agno_sessions VALUES (?,?,?,?)',
                         [('s%d' % i, 'x' * 20000, stamp, stamp)
                          for i in range(count)])
        conn.commit()
        conn.close()

    def test_a_mostly_empty_file_is_compacted(self):
        self.bulk(200, days_ago=30)
        self.bulk(2, days_ago=0)
        before = os.path.getsize(self.path)
        sweep_sessions(self.path, days=7)
        self.assertLess(os.path.getsize(self.path), before * 0.75,
                        'the rows went but the megabytes did not')

    def test_the_survivors_are_intact_afterwards(self):
        """A compaction rewrites the whole file - the live chat must survive it."""
        self.bulk(200, days_ago=30)
        self.add('live', days_ago=0)
        sweep_sessions(self.path, days=7)
        self.assertEqual(self.ids(), ['live'])

    def test_a_barely_touched_file_is_left_alone(self):
        """VACUUM takes an exclusive lock and rewrites everything; a file that
        is nearly full has nothing to give back and should not pay for it."""
        self.bulk(200, days_ago=0)
        self.add('one', days_ago=30)
        with self.assertLogs('infra.session_retention', level='INFO') as log:
            sweep_sessions(self.path, days=7)
        self.assertIn('not compacting yet', chr(10).join(log.output))

    def test_the_threshold_is_a_quarter(self):
        self.assertEqual(VACUUM_WHEN_FREE_FRACTION, 0.25)


class TestItCanBeTurnedOff(SweepCase):

    def test_zero_days_keeps_everything(self):
        self.add('ancient', days_ago=999)
        self.assertEqual(sweep_sessions(self.path, days=0), 0)
        self.assertEqual(self.ids(), ['ancient'])

    def test_the_default_is_a_week(self):
        self.assertEqual(DEFAULT_RETENTION_DAYS, 7)

    def test_the_env_var_wins(self):
        with patch.dict(os.environ, {'SESSION_RETENTION_DAYS': '30'}):
            self.assertEqual(retention_days(), 30)

    def test_a_nonsense_env_var_falls_back(self):
        with patch.dict(os.environ, {'SESSION_RETENTION_DAYS': 'soon'}):
            self.assertEqual(retention_days(), DEFAULT_RETENTION_DAYS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
