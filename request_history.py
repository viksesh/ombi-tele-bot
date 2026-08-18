"""Per-user request history, stored in a small SQLite database.

Ombi can't answer "what did *I* request?" for us: every request is submitted
under one of the configured Ombi service users (OMBI_REQUEST_USER, or
OMBI_AUTO_APPROVE_USER for auto-approved ones), so Ombi has no idea which
Telegram user is behind a request. This module keeps that mapping locally.

Only recent history is kept - rows older than REQUEST_HISTORY_DAYS are pruned
(set it to 0 to keep everything). The database is a single file so it survives
restarts; point REQUEST_HISTORY_DB at a mounted volume in Docker.

Every function here is best-effort: history is a convenience feature, so a
storage failure is logged and swallowed rather than breaking a request.
"""

import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip('"\' ') or default)
    except (TypeError, ValueError):
        logger.warning(f"Invalid {name} value, falling back to {default}")
        return default


DB_PATH = (os.getenv('REQUEST_HISTORY_DB', '').strip('"\' ') or
           os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'requests.db'))

# How much history to keep, in days. 0 disables pruning (keep everything).
RETENTION_DAYS = _env_int('REQUEST_HISTORY_DAYS', 60)

# Cap on how many entries a single user's history view returns.
MAX_ENTRIES = 100

_init_lock = threading.Lock()
_initialized = False


def _connect() -> sqlite3.Connection:
    """Open a connection, creating the schema on first use."""
    global _initialized

    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    if not _initialized:
        with _init_lock:
            if not _initialized:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS requests (
                        user_id       INTEGER NOT NULL,
                        item_type     TEXT    NOT NULL,
                        item_id       TEXT    NOT NULL,
                        title         TEXT    NOT NULL,
                        year          INTEGER,
                        poster        TEXT,
                        status        TEXT    NOT NULL,
                        auto_approved INTEGER NOT NULL DEFAULT 0,
                        requested_at  INTEGER NOT NULL,
                        PRIMARY KEY (user_id, item_type, item_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_requests_user_time
                        ON requests (user_id, requested_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_requests_time
                        ON requests (requested_at);
                    """
                )
                conn.commit()
                _initialized = True
                logger.info(f"Request history database ready at {DB_PATH} "
                            f"(retention: {RETENTION_DAYS or 'unlimited'} days)")
    return conn


def _cutoff() -> int:
    """Epoch seconds before which history is dropped (0 = keep everything)."""
    if RETENTION_DAYS <= 0:
        return 0
    return int(time.time()) - RETENTION_DAYS * 86400


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec='seconds')


def record_request(user_id: int, item_type: str, item_id, title: str,
                   year: int = None, poster: str = None,
                   status: str = 'requested', auto_approved: bool = False) -> None:
    """Store (or refresh) a user's request for an item.

    Re-requesting the same title just updates the existing row, so the history
    view shows one entry per title with the most recent timestamp.
    """
    try:
        # `closing` releases the handle; the inner `conn` commits the transaction
        with closing(_connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO requests
                    (user_id, item_type, item_id, title, year, poster, status,
                     auto_approved, requested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, item_type, item_id) DO UPDATE SET
                    title = excluded.title,
                    year = excluded.year,
                    poster = excluded.poster,
                    status = excluded.status,
                    auto_approved = excluded.auto_approved,
                    requested_at = excluded.requested_at
                """,
                (int(user_id), item_type, str(item_id), title or 'Unknown',
                 year or None, poster or None, status,
                 1 if auto_approved else 0, int(time.time())),
            )
            _prune(conn)
    except Exception as e:
        logger.warning(f"Could not record request history for user {user_id}: {e}")


def list_requests(user_id: int, limit: int = MAX_ENTRIES) -> list:
    """Return a user's recent requests, newest first.

    Each entry mirrors the mini app's item shape: id, type, title, year,
    poster, status, autoApproved, requestedAt (ISO-8601 UTC).
    """
    try:
        with closing(_connect()) as conn, conn:
            _prune(conn)
            rows = conn.execute(
                """
                SELECT item_type, item_id, title, year, poster, status,
                       auto_approved, requested_at
                FROM requests
                WHERE user_id = ? AND requested_at >= ?
                ORDER BY requested_at DESC
                LIMIT ?
                """,
                (int(user_id), _cutoff(), int(limit)),
            ).fetchall()
    except Exception as e:
        logger.warning(f"Could not read request history for user {user_id}: {e}")
        return []

    return [
        {
            'id': row['item_id'],
            'type': row['item_type'],
            'title': row['title'],
            'year': row['year'],
            'poster': row['poster'],
            'status': row['status'],
            'autoApproved': bool(row['auto_approved']),
            'requestedAt': _iso(row['requested_at']),
        }
        for row in rows
    ]


def _prune(conn: sqlite3.Connection) -> None:
    """Delete rows past the retention window (no-op when retention is off)."""
    cutoff = _cutoff()
    if not cutoff:
        return
    deleted = conn.execute("DELETE FROM requests WHERE requested_at < ?", (cutoff,)).rowcount
    if deleted:
        logger.info(f"Pruned {deleted} request history entries older than {RETENTION_DAYS} days")
