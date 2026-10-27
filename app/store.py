"""SQLite storage for appointments and messages.

Timestamps are stored as UTC ISO 8601 so string comparison is safe for
overlap queries. Display conversion happens in tools.py.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS appointments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    start_iso  TEXT NOT NULL,
    end_iso    TEXT NOT NULL,
    service    TEXT NOT NULL,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL,
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appointments_start ON appointments(start_iso);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class SlotTaken(Exception):
    """Raised when a booking would overlap an existing appointment."""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _open(self):
        """One connection per operation: committed on success, always closed."""
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _iso(moment: datetime) -> str:
        """UTC ISO 8601. Comparison is lexicographic, so offsets must not vary."""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()

    def has_overlap(self, start: datetime, end: datetime) -> bool:
        with self._open() as conn:
            row = conn.execute(
                "SELECT id FROM appointments WHERE start_iso < ? AND end_iso > ? LIMIT 1",
                (self._iso(end), self._iso(start)),
            ).fetchone()
        return row is not None

    def book(
        self,
        start_utc: datetime,
        end_utc: datetime,
        service: str,
        name: str,
        phone: str,
        notes: str = "",
    ) -> dict:
        start_iso = self._iso(start_utc)
        end_iso = self._iso(end_utc)

        with self._open() as conn:
            clash = conn.execute(
                "SELECT id FROM appointments WHERE start_iso < ? AND end_iso > ? LIMIT 1",
                (end_iso, start_iso),
            ).fetchone()
            if clash:
                raise SlotTaken(f"appointment {clash['id']} already occupies that time")

            cursor = conn.execute(
                "INSERT INTO appointments (start_iso, end_iso, service, name, phone, notes, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    start_iso,
                    end_iso,
                    service,
                    name,
                    phone,
                    notes,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            appointment_id = cursor.lastrowid

        return {
            "id": appointment_id,
            "start_iso": start_iso,
            "end_iso": end_iso,
            "service": service,
            "name": name,
            "phone": phone,
            "notes": notes,
        }

    def find_by_phone(self, phone: str) -> list[dict]:
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE phone = ? AND start_iso > ? ORDER BY start_iso",
                (phone, self._iso(datetime.now(timezone.utc))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_upcoming(self, tz) -> list[dict]:
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE start_iso > ? ORDER BY start_iso",
                (self._iso(datetime.now(timezone.utc)),),
            ).fetchall()

        appointments = []
        for row in rows:
            start = datetime.fromisoformat(row["start_iso"]).astimezone(tz)
            appointments.append(
                {
                    "id": row["id"],
                    "start": start.isoformat(),
                    "label": f"{start:%A} {start.day} {start:%B} at {start:%H:%M}",
                    "service": row["service"],
                    "name": row["name"],
                    "phone": row["phone"],
                }
            )
        return appointments

    def get(self, appointment_id: int) -> dict | None:
        with self._open() as conn:
            row = conn.execute(
                "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
            ).fetchone()
        return dict(row) if row else None

    def cancel(self, appointment_id: int, phone: str) -> dict | None:
        with self._open() as conn:
            row = conn.execute(
                "SELECT * FROM appointments WHERE id = ? AND phone = ?",
                (appointment_id, phone),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM appointments WHERE id = ?", (appointment_id,))
        return dict(row)

    def add_message(self, name: str, phone: str, reason: str) -> dict:
        with self._open() as conn:
            cursor = conn.execute(
                "INSERT INTO messages (name, phone, reason, created_at) VALUES (?, ?, ?, ?)",
                (name, phone, reason, datetime.now(timezone.utc).isoformat()),
            )
            message_id = cursor.lastrowid
        return {"id": message_id, "name": name, "phone": phone, "reason": reason}
