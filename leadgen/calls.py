"""What happens after a call ends: work out the outcome, update the lead, alert the client.

Reports arrive either through the Vapi webhook or from polling Vapi (reconcile), so
processing is idempotent: whichever comes first claims the call, the other is ignored.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from leadgen.classify import CallAnalysis, analyze_call
from leadgen.context import Context
from leadgen.phones import normalize_email, normalize_phone
from leadgen.reports import send_instant_alerts
from leadgen.timeutil import iso, parse_iso, utcnow
from leadgen.vapi import VapiError

log = logging.getLogger(__name__)

# Outcomes known from how the call ended, without needing to read the transcript.
NO_CONTACT = {"no_answer", "busy", "voicemail", "wrong_number"}
RETRY = {"no_answer", "busy", "voicemail", "failed"}
MAX_PROCESSING_ERRORS = 10
EXTRA_CALLBACK_ATTEMPTS = 2


@dataclass
class CallReport:
    call_id: str | None
    status: str | None
    ended_reason: str | None
    transcript: str
    summary: str | None
    recording_url: str | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_s: float | None
    cost: float | None


def _first(*values):
    return next((v for v in values if v), None)


def extract_report(payload: dict) -> CallReport:
    """Accepts a webhook `message` (end-of-call-report) or a call object from GET /call/{id}."""
    call = payload.get("call") or payload
    artifact = payload.get("artifact") or call.get("artifact") or {}
    analysis = payload.get("analysis") or call.get("analysis") or {}
    recording = artifact.get("recording") or {}
    started = parse_iso(_first(payload.get("startedAt"), call.get("startedAt")))
    ended = parse_iso(_first(payload.get("endedAt"), call.get("endedAt")))
    duration = _first(payload.get("durationSeconds"), call.get("durationSeconds"))
    if duration is None and started and ended:
        duration = (ended - started).total_seconds()
    return CallReport(
        call_id=_first(call.get("id"), payload.get("id")),
        status=call.get("status"),
        ended_reason=_first(payload.get("endedReason"), call.get("endedReason")),
        transcript=_first(artifact.get("transcript"), payload.get("transcript"), call.get("transcript")) or "",
        summary=_first(analysis.get("summary"), payload.get("summary"), call.get("summary")),
        recording_url=_first(
            artifact.get("recordingUrl"),
            (recording.get("mono") or {}).get("combinedUrl"),
            recording.get("stereoUrl"),
            payload.get("recordingUrl"),
            call.get("recordingUrl"),
        ),
        started_at=started,
        ended_at=ended,
        duration_s=float(duration) if duration is not None else None,
        cost=_first(payload.get("cost"), call.get("cost")),
    )


def outcome_from_ended_reason(reason: str | None) -> str | None:
    """Outcome implied by Vapi's endedReason; None when a conversation may have happened."""
    r = (reason or "").lower()
    if "did-not-answer" in r or "no-answer" in r:
        return "no_answer"
    if "busy" in r:
        return "busy"
    if "voicemail" in r:
        return "voicemail"
    if "misdialed" in r or ("invalid" in r and "number" in r):
        return "wrong_number"
    if any(k in r for k in ("error", "failed", "rejected", "not-found", "fault")):
        return "failed"
    return None


def has_conversation(transcript: str) -> bool:
    lines = [ln.strip() for ln in transcript.splitlines() if ln.strip()]
    customer = [ln.split(":", 1)[1] for ln in lines if ln.lower().startswith(("user:", "customer:"))]
    if customer:
        return sum(len(ln.split()) for ln in customer) >= 4
    return len(transcript.strip()) > 300


def _callback_time(ctx: Context, analysis: CallAnalysis | None, now: datetime) -> datetime:
    when = None
    if analysis and analysis.callback_at:
        try:
            local = datetime.fromisoformat(analysis.callback_at)
            when = local if local.tzinfo else local.replace(tzinfo=ctx.calendar.tz)
        except ValueError:
            log.warning("neplatný čas zpětného volání: %r", analysis.callback_at)
    if when is None or when <= now + timedelta(minutes=10):
        when = now + timedelta(hours=24)
    return ctx.calendar.next_slot(when)


def apply_outcome(
    ctx: Context, lead: dict, outcome: str, analysis: CallAnalysis | None, now: datetime
) -> None:
    cfg = ctx.config.calling
    attempts = lead["attempts"]
    updates: dict = {"outcome": outcome, "next_call_at": None}

    if analysis:
        for column, value in (
            ("contact_name", analysis.contact_name),
            ("contact_role", analysis.contact_role),
            ("email", normalize_email(analysis.contact_email)),
        ):
            if value:
                updates[column] = value

    if outcome in RETRY:
        if attempts >= cfg.max_attempts:
            updates.update(status="done", outcome="unreachable")
        else:
            hours = cfg.retry_after_hours[min(max(attempts - 1, 0), len(cfg.retry_after_hours) - 1)]
            updates.update(status="queued", next_call_at=iso(ctx.calendar.next_slot(now + timedelta(hours=hours))))
    elif outcome == "callback":
        if attempts >= cfg.max_attempts + EXTRA_CALLBACK_ATTEMPTS:
            updates.update(status="done", outcome="unreachable")
        else:
            updates.update(status="queued", next_call_at=iso(_callback_time(ctx, analysis, now)))
    elif outcome == "do_not_call":
        updates["status"] = "do_not_call"
        for value, kind in ((lead["phone"], "phone"), (lead["domain"], "domain"), (lead["ico"], "ico")):
            if value:
                ctx.db.block(value, kind, f"nepřeje si kontakt ({lead['company']})")
    else:
        updates["status"] = "done"

    ctx.db.update_lead(lead["id"], **updates)

    new_phone = normalize_phone(analysis.contact_phone) if analysis else None
    if outcome == "callback" and new_phone and new_phone != lead["phone"]:
        try:
            ctx.db.update_lead(lead["id"], phone=new_phone)
        except sqlite3.IntegrityError:
            log.info("lead %s: nové číslo %s už patří jinému leadu", lead["id"], new_phone)


def _process(ctx: Context, call: dict, report: CallReport) -> str:
    lead = ctx.db.get_lead(call["lead_id"])
    now = utcnow()
    implied = outcome_from_ended_reason(report.ended_reason)
    analysis = None
    if implied in NO_CONTACT:
        outcome = implied
    elif not has_conversation(report.transcript):
        outcome = implied or "no_answer"
    else:
        started = report.started_at or parse_iso(call["started_at"]) or now
        analysis = analyze_call(ctx, lead, report.transcript, started, report.summary)
        outcome = "no_answer" if analysis.outcome == "no_conversation" else analysis.outcome

    ctx.db.update_call(
        call["id"],
        status="ended",
        ended_at=iso(report.ended_at or now),
        ended_reason=report.ended_reason,
        duration_s=report.duration_s,
        cost=report.cost,
        outcome=outcome,
        summary=analysis.summary if analysis else report.summary,
        needs=analysis.needs if analysis else None,
        next_step=analysis.next_step if analysis else None,
        transcript=report.transcript,
        recording_url=report.recording_url,
        analysis_json=analysis.model_dump_json() if analysis else None,
    )
    apply_outcome(ctx, lead, outcome, analysis, now)
    log.info("hovor %s (%s): %s", call["vapi_call_id"], lead["company"], outcome)
    return outcome


def process_call(ctx: Context, payload: dict) -> str | None:
    """Process one ended call. Returns the outcome, or None if there was nothing to do."""
    report = extract_report(payload)
    if not report.call_id:
        return None
    call = ctx.db.get_call_by_vapi_id(report.call_id)
    if call is None:
        log.info("hovor %s nepatří tomuto systému, ignoruji", report.call_id)
        return None
    if not ctx.db.claim_call_for_processing(call["id"]):
        return None

    try:
        outcome = _process(ctx, call, report)
    except Exception:
        errors = call["errors"] + 1
        log.exception("vyhodnocení hovoru %s selhalo (pokus %d)", report.call_id, errors)
        if errors < MAX_PROCESSING_ERRORS:
            ctx.db.update_call(call["id"], status="in_progress", errors=errors)
        else:
            ctx.db.update_call(call["id"], status="ended", errors=errors, outcome="error",
                               transcript=report.transcript, recording_url=report.recording_url)
            ctx.db.update_lead(call["lead_id"], status="done", outcome="error",
                               note="hovor se nepodařilo vyhodnotit, zkontrolujte nahrávku")
        return None

    if outcome in ctx.config.reporting.instant_outcomes:
        send_instant_alerts(ctx)
    return outcome


def reconcile(ctx: Context, now: datetime | None = None) -> int:
    """Safety net for lost webhooks: ask Vapi about calls we still consider running."""
    now = now or utcnow()
    stale_after = timedelta(minutes=ctx.config.calling.stale_call_minutes)
    processed = 0
    for call in ctx.db.active_calls():
        age = now - parse_iso(call["started_at"])
        if age < timedelta(minutes=2):
            continue
        try:
            data = ctx.vapi.get_call(call["vapi_call_id"])
        except VapiError as e:
            log.warning("stav hovoru %s nelze zjistit: %s", call["vapi_call_id"], e)
            if age < stale_after:
                continue
            data = {"id": call["vapi_call_id"], "status": "ended", "endedReason": "report-not-found"}
        if data.get("status") != "ended":
            if age < stale_after:
                continue
            data = {**data, "status": "ended", "endedReason": data.get("endedReason") or "report-not-found"}
        if process_call(ctx, data):
            processed += 1

    for lead in ctx.db.orphaned_calling_leads(now - timedelta(hours=1)):
        log.warning("lead %s visel ve stavu 'calling', vracím do fronty", lead["id"])
        ctx.db.update_lead(lead["id"], status="queued", next_call_at=iso(ctx.calendar.next_slot(now)))
    return processed
