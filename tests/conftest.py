from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from leadgen.context import Context
from leadgen.db import Database
from leadgen.settings import Settings, load_config

ROOT = Path(__file__).resolve().parents[1]


class FakeVapi:
    def __init__(self):
        self.created: list[tuple[str, str, dict]] = []
        self.calls: dict[str, dict] = {}
        self.fail: Exception | None = None

    def create_call(self, phone, customer_name, variables):
        if self.fail:
            raise self.fail
        call_id = f"call-{len(self.created) + 1}"
        self.created.append((phone, customer_name, variables))
        return {"id": call_id}

    def get_call(self, call_id):
        return self.calls.get(call_id, {"id": call_id, "status": "in-progress"})


class FakeClaude:
    """Returns queued `create` responses in order and a fixed `parse` result."""

    def __init__(self):
        self.responses: list = []
        self.create_calls: list[dict] = []
        self.parse_result = None
        self.parse_calls: list[dict] = []
        self.parse_error: Exception | None = None

    def create(self, **kwargs):
        self.create_calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)

    def parse(self, output_format, **kwargs):
        self.parse_calls.append(kwargs)
        if self.parse_error:
            raise self.parse_error
        return self.parse_result


def block(type_, **fields):
    return SimpleNamespace(type=type_, **fields)


def response(stop_reason, *content, web_searches=0):
    usage = SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=web_searches))
    return SimpleNamespace(stop_reason=stop_reason, content=list(content), usage=usage)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # keep a developer's .env out of the tests
    base = Settings.from_env()
    return dataclasses.replace(
        base,
        database_path=str(tmp_path / "leads.db"),
        outbox_dir=str(tmp_path / "outbox"),
        config_path=str(ROOT / "config.example.yaml"),
        smtp_host="",
        report_to=["obchod@freeheating.example"],
        report_cc=[],
        test_email=["test@agentura.example"],
        webhook_secret="tajne",
        admin_password="heslo",
        public_base_url="",
    )


@pytest.fixture
def ctx(settings):
    config = load_config(settings.config_path)
    config.calling.enabled = True
    return Context(
        settings=settings,
        config=config,
        db=Database(settings.database_path),
        _claude=FakeClaude(),
        _vapi=FakeVapi(),
    )


@pytest.fixture
def make_lead(ctx):
    counter = iter(range(100, 1000))

    def make(**fields):
        n = next(counter)
        data = {
            "company": f"Správa domů {n} s.r.o.",
            "phone": f"+420602000{n}",
            "domain": f"sprava{n}.cz",
            "website": f"https://sprava{n}.cz",
            "segment": "spravci",
            "region": "Hlavní město Praha",
            "source": "ai_search",
            "fit_score": 80,
            "pitch_hook": "Spravujete 30 domů s vlastní kotelnou.",
            "status": "queued",
            **fields,
        }
        return ctx.db.get_lead(ctx.db.add_lead(data))

    return make
