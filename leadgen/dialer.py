"""Starts Vapi calls for due leads, within calling hours and capacity limits."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from leadgen.context import Context
from leadgen.timeutil import iso, utcnow
from leadgen.vapi import VapiError

log = logging.getLogger(__name__)


def call_variables(ctx: Context, lead: dict) -> dict[str, str]:
    """Values for {{placeholders}} in the Vapi assistant prompt."""
    previous = ctx.db.last_call_for_lead(lead["id"]) if lead["attempts"] else None
    return {
        "client_name": ctx.config.client.name,
        "company": lead["company"],
        "contact_name": lead.get("contact_name") or "",
        "contact_role": lead.get("contact_role") or "",
        "city": lead.get("city") or lead.get("region") or "",
        "segment": ctx.config.segment_name(lead.get("segment")),
        "pitch_hook": lead.get("pitch_hook") or "",
        "is_callback": "ano" if lead.get("outcome") == "callback" else "ne",
        "previous_call_summary": (previous or {}).get("summary") or "",
    }


def due_capacity(ctx: Context, now: datetime) -> int:
    cfg = ctx.config.calling
    if not cfg.enabled or not ctx.calendar.in_window(now):
        return 0
    active = len(ctx.db.active_calls())
    today = ctx.db.count_calls_since(ctx.calendar.day_start(now))
    return max(0, min(cfg.max_concurrent_calls - active, cfg.max_calls_per_day - today))


def run_dialer(ctx: Context, now: datetime | None = None) -> list[int]:
    now = now or utcnow()
    slots = due_capacity(ctx, now)
    if slots <= 0:
        return []

    started: list[int] = []
    for lead in ctx.db.due_leads(now, slots):
        if ctx.db.is_blocked(lead["phone"], lead["domain"], lead["ico"]):
            ctx.db.update_lead(lead["id"], status="do_not_call", next_call_at=None)
            continue
        if not ctx.db.claim_lead_for_call(lead["id"]):
            continue
        try:
            call = ctx.vapi.create_call(lead["phone"], lead["company"], call_variables(ctx, lead))
        except VapiError as e:
            if e.status_code == 400 and "number" in str(e).lower():
                log.warning("Vapi odmítlo číslo leadu %s: %s", lead["id"], e)
                ctx.db.update_lead(lead["id"], status="rejected", note=f"Vapi odmítlo číslo: {e}"[:500])
                continue
            # Outage or misconfiguration: put the lead back and stop this round.
            log.error("hovor se nepodařilo spustit: %s", e)
            ctx.db.update_lead(lead["id"], status="queued", next_call_at=iso(now + timedelta(minutes=30)))
            break
        ctx.db.add_call(lead["id"], call["id"], lead["phone"], now)
        ctx.db.update_lead(lead["id"], attempts=lead["attempts"] + 1, last_called_at=iso(now))
        log.info("volám %s (%s), lead %s", lead["company"], lead["phone"], lead["id"])
        started.append(lead["id"])
    return started
