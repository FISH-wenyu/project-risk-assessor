from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

BUSY_TIMEOUT_MS = 5000
SAFE_JOURNAL_MODES = {"wal", "delete", "truncate", "persist", "memory"}


def connect(db_path: str | Path, *, row_factory: bool = False) -> sqlite3.Connection:
    """Open a local SQLite connection with the project's concurrency settings.

    `busy_timeout` is a per-connection setting, so every connection to the
    shared local database must go through here. Without it SQLite raises
    `database is locked` immediately instead of waiting for a competing writer.
    """
    conn = sqlite3.connect(db_path)
    if row_factory:
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


class SQLiteStoreMixin:
    """Shared local-SQLite plumbing for stores that share `risk_agent.db`.

    Any new local store must use this instead of calling `sqlite3.connect`
    directly, so concurrency settings stay uniform across the whole file.
    """

    db_path: Path
    journal_mode: str = ""
    wal_degraded_reason: str | None = None

    def _prepare_database(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._negotiate_journal_mode()

    def _connect(self, *, row_factory: bool = False) -> sqlite3.Connection:
        return connect(self.db_path, row_factory=row_factory)

    def _request_wal_mode(self, conn: sqlite3.Connection) -> str:
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        return str(row[0] if row else "").lower()

    def _negotiate_journal_mode(self) -> None:
        """Try to put the database file into WAL mode, once per store.

        Unlike `busy_timeout`, `journal_mode` is a persistent property of the
        database file, so negotiating it at construction is enough. Filesystems
        that cannot support WAL degrade to another safe rollback mode and record
        the reason instead of failing startup.
        """
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            negotiation_error = ""
            try:
                mode = self._request_wal_mode(conn)
            except sqlite3.DatabaseError as exc:
                negotiation_error = type(exc).__name__
                row = conn.execute("PRAGMA journal_mode").fetchone()
                mode = str(row[0] if row else "").lower()
            if mode not in SAFE_JOURNAL_MODES:
                raise RuntimeError(
                    f"SQLite returned unsafe journal mode: {mode or 'unknown'}"
                )
            self.journal_mode = mode
            if mode == "wal":
                self.wal_degraded_reason = None
            elif negotiation_error:
                self.wal_degraded_reason = (
                    f"wal_negotiation_error:{negotiation_error}:{mode}"
                )
            else:
                self.wal_degraded_reason = f"wal_not_enabled:{mode}"
