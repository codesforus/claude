"""Wires settings, config and clients together; everything else receives a Context."""

from __future__ import annotations

from dataclasses import dataclass, field

from leadgen.ai import Claude
from leadgen.db import Database
from leadgen.notify import Mailer
from leadgen.settings import AppConfig, Settings, load_config
from leadgen.timeutil import CallingCalendar
from leadgen.vapi import VapiClient


@dataclass
class Context:
    settings: Settings
    config: AppConfig
    db: Database
    _claude: Claude | None = field(default=None, repr=False)
    _vapi: VapiClient | None = field(default=None, repr=False)
    _mailer: Mailer | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.calendar = CallingCalendar(self.config.calling, self.config.timezone)

    @property
    def claude(self) -> Claude:
        if self._claude is None:
            self._claude = Claude(self.settings.claude_model)
        return self._claude

    @property
    def vapi(self) -> VapiClient:
        if self._vapi is None:
            s = self.settings
            self._vapi = VapiClient(
                s.vapi_api_key,
                s.vapi_assistant_id,
                s.vapi_phone_number_id,
                server_url=s.webhook_url if s.vapi_override_server_url else None,
                first_message=self.config.calling.first_message,
            )
        return self._vapi

    @property
    def mailer(self) -> Mailer:
        if self._mailer is None:
            self._mailer = Mailer(self.settings)
        return self._mailer


def build_context(settings: Settings | None = None) -> Context:
    settings = settings or Settings.from_env()
    return Context(
        settings=settings,
        config=load_config(settings.config_path),
        db=Database(settings.database_path),
    )
