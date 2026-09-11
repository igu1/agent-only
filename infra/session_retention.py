"""mem/db.sqlite3 is a CACHE, and this is what makes that true.

It holds the model's working memory - the conversation history agno replays
into each prompt, plus the three facts the turn is TOLD rather than left to
infer (admission_stage, institution_id, institution_verifies). None of it is
the record of anything: every message is already in CmpAgentMessage and every
inquiry in CmpRegistration, both on SQL Server, both permanent.

Left alone it only grows. Nothing in agno or in this codebase ever deletes a
session, so a row written for a chat that ended months ago is still replayed
past on every startup and still holds that parent's name, number and email in
an unencrypted file with no retention policy of its own. Measured here: ~180 KB
per conversation, which is ~3 GB a year at fifty chats a day.

So old sessions are dropped. Nothing is lost that is not also on SQL Server -
the worst case is an assistant that has forgotten a conversation nobody has
touched in a week, which is the same thing that happens when the file is
deleted by hand.

Deliberately NOT touching agno_memories: those are long-term facts about a
parent ("has two children at this school"), they are tiny, and they are the one
thing here that is not a replay of something stored elsewhere.
"""
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

# A week is long enough that no live conversation is ever caught - the longest
# real admission chat runs to minutes, not days - and short enough that the file
# stops being an open-ended store of contact details.
DEFAULT_RETENTION_DAYS = 7

# Reclaiming space costs a full rewrite of the file under an exclusive lock, so
# it is worth doing only when there is real space to reclaim. Row count is the
# wrong signal for that - sixteen long chats free more than a hundred short
# ones - so the trigger is how SPARSE the file actually is. SQLite keeps
# deleted pages on a freelist and reuses them, so a file that is a quarter
# empty is not leaking, it has simply reached its high-water mark; past that
# the rewrite earns its lock.
VACUUM_WHEN_FREE_FRACTION = 0.25


def retention_days() -> int:
    """0 or less disables the sweep entirely."""
    try:
        return int(os.getenv('SESSION_RETENTION_DAYS', DEFAULT_RETENTION_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


def sweep_sessions(db_path: str, *, days: int | None = None) -> int:
    """Drop sessions untouched for `days`. Returns how many went.

    Never raises: a cache that cannot be tidied is not a reason to refuse to
    start serving parents.
    """
    days = retention_days() if days is None else days
    if days <= 0:
        logger.info('session retention disabled (SESSION_RETENTION_DAYS=%s)', days)
        return 0
    if not os.path.exists(db_path):
        return 0

    cutoff = None
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            cur = conn.cursor()
            # agno stores unix seconds; strftime('%s') keeps the comparison in
            # SQLite rather than guessing the column's units in Python.
            cur.execute("SELECT CAST(strftime('%s', 'now', ?) AS INTEGER)",
                        ('-%d days' % days,))
            cutoff = cur.fetchone()[0]
            cur.execute(
                'DELETE FROM agno_sessions WHERE COALESCE(updated_at, created_at, 0) < ?',
                (cutoff,))
            removed = cur.rowcount or 0
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        # A missing table is the normal first-run case, not a fault.
        logger.warning('session retention sweep skipped: %s', e)
        return 0

    if removed:
        logger.info('session retention: removed %d session(s) idle for over %d day(s)',
                    removed, days)
        _vacuum_if_sparse(db_path)
    return removed


def _free_fraction(db_path: str) -> float:
    """How much of the file is pages SQLite is no longer using."""
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            free = conn.execute('PRAGMA freelist_count').fetchone()[0]
            total = conn.execute('PRAGMA page_count').fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return 0.0
    return (free / total) if total else 0.0


def _vacuum_if_sparse(db_path: str) -> None:
    fraction = _free_fraction(db_path)
    if fraction < VACUUM_WHEN_FREE_FRACTION:
        logger.info('session retention: %.0f%% of the file is free - not compacting yet',
                    fraction * 100)
        return
    _vacuum(db_path)


def _vacuum(db_path: str) -> None:
    """Give the freed pages back to the filesystem."""
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.isolation_level = None          # VACUUM cannot run in a transaction
            conn.execute('VACUUM')
        finally:
            conn.close()
        logger.info('session retention: database compacted')
    except sqlite3.Error as e:
        logger.warning('session retention: compaction skipped: %s', e)
