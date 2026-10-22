"""SQLite storage for leads, calls, the do-not-call list and discovery runs.

Lead lifecycle (leads.status):
    new        found, waiting for AI qualification
    queued     qualified, will be called once next_call_at has passed
    calling    a call is in flight
    done       finished (outcome says how: hot, warm, not_interested, unreachable, ...)
    rejected   not a fit, no usable phone, competitor, ...
    do_not_call  asked not to be contacted again
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from leadgen.timeutil import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    ico TEXT,
    website TEXT,
    domain TEXT,
    phone TEXT,
    email TEXT,
    contact_name TEXT,
    contact_role TEXT,
    address TEXT,
    city TEXT,
    region TEXT,
    segment TEXT,
    source TEXT NOT NULL,
    source_ref TEXT,
    fit_score INTEGER,
    fit_reason TEXT,
    pitch_hook TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    outcome TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_call_at TEXT,
    last_called_at TEXT,
    notified_at TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_leads_phone ON leads(phone) WHERE phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_leads_domain ON leads(domain);
CREATE INDEX IF NOT EXISTS ix_leads_ico ON leads(ico);
CREATE INDEX IF NOT EXISTS ix_leads_status ON leads(status, next_call_at);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    vapi_call_id TEXT UNIQUE,
    phone TEXT,
    status TEXT NOT NULL DEFAULT 'in_progress',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    ended_reason TEXT,
    duration_s REAL,
    cost REAL,
    outcome TEXT,
    summary TEXT,
    needs TEXT,
    next_step TEXT,
    transcript TEXT,
    recording_url TEXT,
    analysis_json TEXT,
    errors INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_calls_status ON calls(status);
CREATE INDEX IF NOT EXISTS ix_calls_started ON calls(started_at);

CREATE TABLE IF NOT EXISTS blocklist (
    value TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    found INTEGER NOT NULL DEFAULT 0,
    added INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_task ON discovery_runs(task_key, started_at);
"""

LEAD_COLUMNS = {
    "company", "ico", "website", "domain", "phone", "email", "contact_name", "contact_role",
    "address", "city", "region", "segment", "source", "source_ref", "fit_score", "fit_reason",
    "pitch_hook", "status", "outcome", "attempts", "next_call_at", "last_called_at",
    "notified_at", "note",
}
CALL_COLUMNS = {
    "status", "ended_at", "ended_reason", "duration_s", "cost", "outcome", "summary", "needs",
    "next_step", "transcript", "recording_url", "analysis_json", "errors",
}

CALL_REPORT_SELECT = """
SELECT c.*, l.company, l.ico, l.website, l.email, l.contact_name, l.contact_role, l.city,
       l.region, l.segment, l.source, l.fit_score, l.fit_reason, l.attempts, l.next_call_at,
       l.status AS lead_status
FROM calls c JOIN leads l ON l.id = c.lead_id
"""


def _assignments(fields: dict[str, Any], allowed: set[str]) -> tuple[str, list[Any]]:
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"neznámé sloupce: {sorted(unknown)}")
    return ", ".join(f"{k} = ?" for k in fields), list(fields.values())


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as c:
            c.executescript(SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=15000")
        return c

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        c = self._connect()
        try:
            with c:
                yield c
        finally:
            c.close()

    def _all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._tx() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    # --- leads -------------------------------------------------------------

    def add_lead(self, fields: dict[str, Any]) -> int:
        _assignments(fields, LEAD_COLUMNS)
        now = iso(utcnow())
        data = {**fields, "created_at": now, "updated_at": now}
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        with self._tx() as c:
            cur = c.execute(f"INSERT INTO leads ({cols}) VALUES ({marks})", list(data.values()))
            return int(cur.lastrowid)

    def get_lead(self, lead_id: int) -> dict | None:
        return self._one("SELECT * FROM leads WHERE id = ?", (lead_id,))

    def update_lead(self, lead_id: int, **fields: Any) -> None:
        sets, values = _assignments(fields, LEAD_COLUMNS)
        with self._tx() as c:
            c.execute(
                f"UPDATE leads SET {sets}, updated_at = ? WHERE id = ?",
                [*values, iso(utcnow()), lead_id],
            )

    def claim_lead_for_call(self, lead_id: int) -> bool:
        """Atomically move a queued lead to 'calling'; False if someone else got it first."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE leads SET status = 'calling', updated_at = ? WHERE id = ? AND status = 'queued'",
                (iso(utcnow()), lead_id),
            )
            return cur.rowcount == 1

    def find_duplicate(self, phone: str | None, domain: str | None, ico: str | None) -> dict | None:
        clauses, params = [], []
        for col, value in (("phone", phone), ("domain", domain), ("ico", ico)):
            if value:
                clauses.append(f"{col} = ?")
                params.append(value)
        if not clauses:
            return None
        return self._one(f"SELECT * FROM leads WHERE {' OR '.join(clauses)} LIMIT 1", params)

    def leads_by_status(self, status: str, limit: int = 100) -> list[dict]:
        return self._all(
            "SELECT * FROM leads WHERE status = ? ORDER BY id LIMIT ?", (status, limit)
        )

    def due_leads(self, now: datetime, limit: int) -> list[dict]:
        """Leads to call now: agreed callbacks first, then fresh leads by fit, then retries."""
        return self._all(
            """
            SELECT * FROM leads
            WHERE status = 'queued' AND phone IS NOT NULL
              AND (next_call_at IS NULL OR next_call_at <= ?)
            ORDER BY CASE WHEN outcome = 'callback' THEN 0 ELSE 1 END,
                     attempts, fit_score DESC, id
            LIMIT ?
            """,
            (iso(now), limit),
        )

    def recent_leads(self, limit: int = 200, status: str | None = None) -> list[dict]:
        if status:
            return self._all(
                "SELECT * FROM leads WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            )
        return self._all("SELECT * FROM leads ORDER BY updated_at DESC LIMIT ?", (limit,))

    def all_leads(self) -> list[dict]:
        return self._all("SELECT * FROM leads ORDER BY id")

    def status_counts(self) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS n FROM leads GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def outcome_counts(self) -> dict[str, int]:
        rows = self._all(
            "SELECT outcome, COUNT(*) AS n FROM leads WHERE outcome IS NOT NULL GROUP BY outcome"
        )
        return {r["outcome"]: r["n"] for r in rows}

    def count_leads_created(self, start: datetime, end: datetime) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM leads WHERE created_at >= ? AND created_at < ? AND source != 'test'",
            (iso(start), iso(end)),
        )
        return row["n"] if row else 0

    def known_companies(self, segment: str | None, region: str | None, limit: int = 80) -> list[str]:
        """Names already in the database, so the AI researcher doesn't propose them again."""
        clauses, params = [], []
        if segment:
            clauses.append("segment = ?")
            params.append(segment)
        if region:
            clauses.append("region = ?")
            params.append(region)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._all(
            f"SELECT company FROM leads {where} ORDER BY id DESC LIMIT ?", [*params, limit]
        )
        return [r["company"] for r in rows]

    def leads_to_alert(self, outcomes: list[str]) -> list[dict]:
        if not outcomes:
            return []
        marks = ", ".join("?" for _ in outcomes)
        return self._all(
            f"SELECT * FROM leads WHERE outcome IN ({marks}) AND notified_at IS NULL ORDER BY updated_at",
            outcomes,
        )

    def orphaned_calling_leads(self, updated_before: datetime) -> list[dict]:
        """Leads stuck in 'calling' without a live call (e.g. crash right after dialing)."""
        return self._all(
            """
            SELECT l.* FROM leads l
            WHERE l.status = 'calling' AND l.updated_at < ?
              AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.lead_id = l.id
                              AND c.status IN ('in_progress', 'processing'))
            """,
            (iso(updated_before),),
        )

    def upcoming_callbacks(self, start: datetime, end: datetime) -> list[dict]:
        return self._all(
            """
            SELECT * FROM leads
            WHERE status = 'queued' AND outcome = 'callback'
              AND next_call_at >= ? AND next_call_at < ?
            ORDER BY next_call_at
            """,
            (iso(start), iso(end)),
        )

    # --- do-not-call list ----------------------------------------------------

    def block(self, value: str, kind: str, reason: str = "") -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO blocklist (value, kind, reason, created_at) VALUES (?, ?, ?, ?)",
                (value, kind, reason, iso(utcnow())),
            )

    def is_blocked(self, phone: str | None, domain: str | None, ico: str | None) -> bool:
        values = [v for v in (phone, domain, ico) if v]
        if not values:
            return False
        marks = ", ".join("?" for _ in values)
        return self._one(f"SELECT 1 FROM blocklist WHERE value IN ({marks})", values) is not None

    # --- calls -------------------------------------------------------------

    def add_call(self, lead_id: int, vapi_call_id: str, phone: str, started_at: datetime) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO calls (lead_id, vapi_call_id, phone, started_at) VALUES (?, ?, ?, ?)",
                (lead_id, vapi_call_id, phone, iso(started_at)),
            )
            return int(cur.lastrowid)

    def get_call(self, call_id: int) -> dict | None:
        return self._one("SELECT * FROM calls WHERE id = ?", (call_id,))

    def get_call_by_vapi_id(self, vapi_call_id: str) -> dict | None:
        return self._one("SELECT * FROM calls WHERE vapi_call_id = ?", (vapi_call_id,))

    def claim_call_for_processing(self, call_id: int) -> bool:
        """Webhook and poller can both see the same ended call; only one may process it."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE calls SET status = 'processing' WHERE id = ? AND status = 'in_progress'",
                (call_id,),
            )
            return cur.rowcount == 1

    def release_interrupted_calls(self) -> int:
        """At startup nothing can still be processing; a restart interrupted those calls."""
        with self._tx() as c:
            return c.execute("UPDATE calls SET status = 'in_progress' WHERE status = 'processing'").rowcount

    def update_call(self, call_id: int, **fields: Any) -> None:
        sets, values = _assignments(fields, CALL_COLUMNS)
        with self._tx() as c:
            c.execute(f"UPDATE calls SET {sets} WHERE id = ?", [*values, call_id])

    def active_calls(self) -> list[dict]:
        return self._all("SELECT * FROM calls WHERE status = 'in_progress' ORDER BY started_at")

    def count_calls_since(self, since: datetime) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM calls WHERE started_at >= ?", (iso(since),))
        return row["n"] if row else 0

    def calls_between(self, start: datetime, end: datetime) -> list[dict]:
        return self._all(
            CALL_REPORT_SELECT + " WHERE c.started_at >= ? AND c.started_at < ? ORDER BY c.started_at",
            (iso(start), iso(end)),
        )

    def last_call_for_lead(self, lead_id: int) -> dict | None:
        return self._one(
            CALL_REPORT_SELECT + " WHERE c.lead_id = ? ORDER BY c.started_at DESC, c.id DESC LIMIT 1",
            (lead_id,),
        )

    def recent_calls(self, limit: int = 50) -> list[dict]:
        return self._all(CALL_REPORT_SELECT + " ORDER BY c.started_at DESC LIMIT ?", (limit,))

    # --- discovery runs ----------------------------------------------------

    def start_run(self, task_key: str) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO discovery_runs (task_key, started_at) VALUES (?, ?)",
                (task_key, iso(utcnow())),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, found: int, added: int, error: str | None = None) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE discovery_runs SET finished_at = ?, found = ?, added = ?, error = ? WHERE id = ?",
                (iso(utcnow()), found, added, error, run_id),
            )

    def last_run_times(self) -> dict[str, str]:
        rows = self._all(
            "SELECT task_key, MAX(started_at) AS last FROM discovery_runs GROUP BY task_key"
        )
        return {r["task_key"]: r["last"] for r in rows}
