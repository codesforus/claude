from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Candidate:
    """A company found by any source, before deduplication and storage."""

    company: str
    source: str
    phone: str | None = None
    website: str | None = None
    email: str | None = None
    ico: str | None = None
    contact_name: str | None = None
    contact_role: str | None = None
    address: str | None = None
    city: str | None = None
    region: str | None = None
    segment: str | None = None
    source_ref: str | None = None
    fit_score: int | None = None
    fit_reason: str | None = None
    pitch_hook: str | None = None


@dataclass(frozen=True)
class Task:
    """One unit of discovery work: a source searching one segment (or intent) in one region."""

    source: str
    kind: str  # "segment" or "intent"
    ref_id: str
    region: str

    @property
    def key(self) -> str:
        return f"{self.source}:{self.ref_id}:{self.region}"


@dataclass
class TaskStats:
    found: int = 0
    added: int = 0
