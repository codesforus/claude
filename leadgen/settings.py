"""Configuration.

Secrets and deployment details come from environment variables (.env),
business rules (who to look for, when to call, what to report) from config.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator


class Segment(BaseModel):
    id: str
    name: str
    description: str
    places_queries: list[str] = []
    enabled: bool = True


class IntentSearch(BaseModel):
    """Country-wide search for companies showing a concrete need right now."""

    id: str
    description: str
    enabled: bool = True


class ClientCfg(BaseModel):
    name: str
    website: str = ""
    offer: str
    ideal_customer: str
    exclude: str = ""


class DiscoveryCfg(BaseModel):
    enabled: bool = True
    sources: list[str] = ["ai_search"]
    schedule: str = "0 6 * * 1-5"
    tasks_per_run: int = 3
    companies_per_task: int = 12
    target_queue_size: int = 150
    min_fit_score: int = 60
    effort: str = "high"
    max_web_searches: int = 20
    regions: list[str]
    intent_searches: list[IntentSearch] = []

    @field_validator("sources")
    @classmethod
    def _known_sources(cls, v: list[str]) -> list[str]:
        unknown = set(v) - {"ai_search", "google_places"}
        if unknown:
            raise ValueError(f"neznámé zdroje: {sorted(unknown)}")
        return v


class CallingCfg(BaseModel):
    # Off by default so nothing dials until someone deliberately switches it on.
    enabled: bool = False
    windows: list[str] = ["09:00-11:45", "13:00-16:30"]
    weekdays: list[int] = [1, 2, 3, 4, 5]
    skip_public_holidays: bool = True
    max_concurrent_calls: int = 2
    max_calls_per_day: int = 60
    max_attempts: int = 3
    retry_after_hours: list[float] = [3, 24, 48]
    stale_call_minutes: int = 60
    first_message: str | None = None


class ReportingCfg(BaseModel):
    weekly_cron: str = "30 7 * * 1"
    instant_outcomes: list[str] = ["hot"]


class AppConfig(BaseModel):
    timezone: str = "Europe/Prague"
    client: ClientCfg
    segments: list[Segment]
    discovery: DiscoveryCfg
    calling: CallingCfg = Field(default_factory=CallingCfg)
    reporting: ReportingCfg = Field(default_factory=ReportingCfg)

    @property
    def enabled_segments(self) -> list[Segment]:
        return [s for s in self.segments if s.enabled]

    def segment(self, segment_id: str | None) -> Segment | None:
        return next((s for s in self.segments if s.id == segment_id), None)

    def segment_name(self, segment_id: str | None) -> str:
        seg = self.segment(segment_id)
        return seg.name if seg else (segment_id or "")


def load_config(path: str | Path) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        return AppConfig.model_validate(yaml.safe_load(f))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "ano"}


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in _env(name).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    claude_model: str
    vapi_api_key: str
    vapi_assistant_id: str
    vapi_phone_number_id: str
    vapi_override_server_url: bool
    public_base_url: str
    webhook_secret: str
    google_maps_api_key: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_security: str
    report_to: list[str]
    report_cc: list[str]
    test_email: list[str]
    admin_user: str
    admin_password: str
    database_path: str
    config_path: str
    outbox_dir: str
    scheduler_enabled: bool

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        data_dir = _env("DATA_DIR", "data")
        return cls(
            claude_model=_env("CLAUDE_MODEL", "claude-opus-5-5"),
            vapi_api_key=_env("VAPI_API_KEY"),
            vapi_assistant_id=_env("VAPI_ASSISTANT_ID"),
            vapi_phone_number_id=_env("VAPI_PHONE_NUMBER_ID"),
            vapi_override_server_url=_env_bool("VAPI_OVERRIDE_SERVER_URL", True),
            public_base_url=_env("PUBLIC_BASE_URL").rstrip("/"),
            webhook_secret=_env("WEBHOOK_SECRET"),
            google_maps_api_key=_env("GOOGLE_MAPS_API_KEY"),
            smtp_host=_env("SMTP_HOST"),
            smtp_port=int(_env("SMTP_PORT", "587")),
            smtp_user=_env("SMTP_USER"),
            smtp_password=_env("SMTP_PASSWORD"),
            smtp_from=_env("SMTP_FROM"),
            smtp_security=_env("SMTP_SECURITY", "starttls").lower(),
            report_to=_env_list("REPORT_TO"),
            report_cc=_env_list("REPORT_CC"),
            test_email=_env_list("TEST_EMAIL") or _env_list("REPORT_CC"),
            admin_user=_env("ADMIN_USER", "admin"),
            admin_password=_env("ADMIN_PASSWORD"),
            database_path=_env("DATABASE_PATH", str(Path(data_dir) / "leads.db")),
            config_path=_env("CONFIG_PATH", "config.yaml"),
            outbox_dir=_env("OUTBOX_DIR", str(Path(data_dir) / "outbox")),
            scheduler_enabled=_env_bool("SCHEDULER_ENABLED", True),
        )

    @property
    def webhook_url(self) -> str | None:
        """URL Vapi posts end-of-call reports to; None when not publicly reachable."""
        if not self.public_base_url:
            return None
        url = f"{self.public_base_url}/vapi/webhook"
        return f"{url}?token={self.webhook_secret}" if self.webhook_secret else url
