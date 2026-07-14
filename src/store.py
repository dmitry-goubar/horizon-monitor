"""
Structured event store (SQLite) — the durable, queryable source of truth.

ChromaDB (src/rag.py) handles *semantic* search ("anything about deployments").
This store handles *exact* and *temporal* questions that vector search is bad at —
"everything John said between 9 and 11am", "the last 20 events", counts per
speaker/channel. Both stores are keyed on the same MessageEvent.doc_id(), so a row
here and a vector there refer to the same message.

It is also the dedup gate: ingest() uses INSERT OR IGNORE on the doc_id primary key
and returns only the rows that were genuinely new, so callers embed/notify each
message exactly once even though the poller re-reads the same screen repeatedly.

SQLite is opened with check_same_thread=False because the tray drains events on a
different thread than the monitor loop; writes are serialised through one connection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import MessageEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    doc_id           TEXT PRIMARY KEY,
    observed_at      TEXT NOT NULL,   -- ISO-8601 capture time (UTC)
    chat_time        TEXT,            -- on-screen timestamp, verbatim
    speaker          TEXT,
    message          TEXT,
    app              TEXT,
    channel          TEXT,
    window_title     TEXT,
    directed_at_user INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_observed_at ON events(observed_at);
CREATE INDEX IF NOT EXISTS idx_events_speaker     ON events(speaker);
CREATE INDEX IF NOT EXISTS idx_events_channel     ON events(channel);
"""


class EventStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def ingest(self, events: list[MessageEvent]) -> list[MessageEvent]:
        """Insert events, skipping any already stored (by doc_id).

        Returns the subset that were genuinely new — callers should embed and
        notify only on these.
        """
        assert self._conn is not None, "call connect() first"
        new: list[MessageEvent] = []
        for e in events:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO events
                   (doc_id, observed_at, chat_time, speaker, message, app,
                    channel, window_title, directed_at_user)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    e.doc_id(),
                    e.timestamp.isoformat(),
                    e.chat_time,
                    e.speaker,
                    e.message,
                    e.app,
                    e.channel,
                    e.window_title,
                    int(e.directed_at_user),
                ),
            )
            if cur.rowcount:
                new.append(e)
        self._conn.commit()
        return new

    def count(self) -> int:
        if self._conn is None:
            return 0
        return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def recent(self, limit: int = 20) -> list[dict]:
        """Most recently captured events, newest first."""
        assert self._conn is not None, "call connect() first"
        rows = self._conn.execute(
            "SELECT * FROM events ORDER BY observed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def query_range(
        self,
        start_iso: str,
        end_iso: str,
        speaker: str | None = None,
        channel: str | None = None,
    ) -> list[dict]:
        """Events captured within [start_iso, end_iso), optionally filtered by
        speaker/channel (case-insensitive substring), oldest first."""
        assert self._conn is not None, "call connect() first"
        sql = "SELECT * FROM events WHERE observed_at >= ? AND observed_at < ?"
        params: list = [start_iso, end_iso]
        if speaker:
            sql += " AND lower(speaker) LIKE ?"
            params.append(f"%{speaker.lower()}%")
        if channel:
            sql += " AND lower(channel) LIKE ?"
            params.append(f"%{channel.lower()}%")
        sql += " ORDER BY observed_at ASC"
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def search(self, text: str, limit: int = 50, channel: str | None = None) -> list[dict]:
        """Substring (LIKE) search over message + speaker — the free, exact-lookup
        complement to RAG semantic search (src/rag.py). Great for finding a literal
        string (an email address, a table name, "distro") that vector search would
        only approximate. Newest first; optional channel filter."""
        assert self._conn is not None, "call connect() first"
        like = f"%{text.lower()}%"
        sql = ("SELECT * FROM events WHERE (lower(message) LIKE ? OR lower(speaker) LIKE ?)")
        params: list = [like, like]
        if channel:
            sql += " AND lower(channel) LIKE ?"
            params.append(f"%{channel.lower()}%")
        sql += " ORDER BY observed_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def expire_older_than(self, cutoff_iso: str) -> int:
        """Delete events captured before cutoff_iso (for N-day retention).

        observed_at is ISO-8601 UTC, which sorts lexically, so a string `<`
        comparison is correct. Returns the number of rows removed.
        """
        assert self._conn is not None, "call connect() first"
        cur = self._conn.execute(
            "DELETE FROM events WHERE observed_at < ?", (cutoff_iso,)
        )
        self._conn.commit()
        return cur.rowcount

    def purge_all(self) -> int:
        """Delete every stored event (the one-shot 'purge all'). Returns rows removed."""
        assert self._conn is not None, "call connect() first"
        cur = self._conn.execute("DELETE FROM events")
        self._conn.commit()
        return cur.rowcount

    def stats(self) -> dict:
        """Summary for the Collect tab: total, capture span, and per-channel counts."""
        if self._conn is None:
            return {"total": 0, "first": None, "last": None, "channels": []}
        total = self.count()
        first, last = self._conn.execute(
            "SELECT MIN(observed_at), MAX(observed_at) FROM events"
        ).fetchone()
        chans = self._conn.execute(
            "SELECT CASE WHEN channel='' THEN '(unknown)' ELSE channel END AS ch, "
            "COUNT(*) AS n FROM events GROUP BY ch ORDER BY n DESC"
        ).fetchall()
        return {
            "total": total,
            "first": first,
            "last": last,
            "channels": [(r[0], r[1]) for r in chans],
        }

    def speakers(self) -> list[str]:
        assert self._conn is not None, "call connect() first"
        rows = self._conn.execute(
            "SELECT DISTINCT speaker FROM events WHERE speaker <> '' ORDER BY speaker"
        ).fetchall()
        return [r[0] for r in rows]

    def channels(self) -> list[str]:
        assert self._conn is not None, "call connect() first"
        rows = self._conn.execute(
            "SELECT DISTINCT channel FROM events WHERE channel <> '' ORDER BY channel"
        ).fetchall()
        return [r[0] for r in rows]

    def channels_with_app(self) -> list[tuple[str, str]]:
        """Distinct channels paired with the app they came from, for the Collect picker.

        A channel almost always belongs to one app; if it somehow spans apps, the most
        frequent app wins. Returns (channel, app) sorted by channel.
        """
        assert self._conn is not None, "call connect() first"
        rows = self._conn.execute(
            "SELECT channel, app, COUNT(*) AS n FROM events "
            "WHERE channel <> '' GROUP BY channel, app ORDER BY channel, n DESC"
        ).fetchall()
        dominant: dict[str, str] = {}
        for ch, app, _n in rows:
            if ch not in dominant:                 # first row per channel = most frequent app
                dominant[ch] = app or "unknown"
        return sorted(dominant.items())

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
