"""Background jobs: discovery, qualification, dialing, call reconciliation, emails."""

from __future__ import annotations

import logging
import re
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from leadgen.calls import reconcile
from leadgen.context import Context
from leadgen.dialer import run_dialer
from leadgen.discovery import run_discovery
from leadgen.qualify import qualify_pending
from leadgen.reports import send_instant_alerts, send_weekly_report

log = logging.getLogger(__name__)

_CRON_WEEKDAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def cron_trigger(expr: str, timezone: str) -> CronTrigger:
    """Standard crontab semantics (0/7 = Sunday). APScheduler 3.x's own from_crontab
    treats 0 as Monday, which would shift "1-5" to Tuesday–Saturday."""
    minute, hour, day, month, weekday = expr.split()
    weekday = re.sub(r"(?<![/\d])[0-7](?!\d)", lambda m: _CRON_WEEKDAYS[int(m.group())], weekday)
    return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=weekday, timezone=timezone)


def _guarded(ctx: Context, name: str, fn: Callable[[Context], object]) -> Callable[[], None]:
    def run() -> None:
        try:
            fn(ctx)
        except Exception:
            log.exception("úloha %s selhala", name)

    return run


def build_scheduler(ctx: Context) -> BackgroundScheduler:
    tz = ctx.config.timezone
    scheduler = BackgroundScheduler(
        timezone=tz,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600},
    )
    jobs = [
        ("dialer", run_dialer, IntervalTrigger(minutes=2)),
        ("reconcile", reconcile, IntervalTrigger(minutes=3)),
        ("alerts", send_instant_alerts, IntervalTrigger(minutes=5)),
        ("qualify", qualify_pending, IntervalTrigger(minutes=30)),
        ("weekly_report", send_weekly_report, cron_trigger(ctx.config.reporting.weekly_cron, tz)),
    ]
    if ctx.config.discovery.enabled:
        jobs.append(("discovery", run_discovery, cron_trigger(ctx.config.discovery.schedule, tz)))
    for name, fn, trigger in jobs:
        scheduler.add_job(_guarded(ctx, name, fn), trigger, id=name, name=name)
    return scheduler
