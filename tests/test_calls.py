from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from leadgen.calls import extract_report, outcome_from_ended_reason, process_call, reconcile
from leadgen.classify import CallAnalysis
from leadgen.dialer import run_dialer
from leadgen.reports import weekly_report_email
from leadgen.timeutil import parse_iso, utcnow
from leadgen.vapi import VapiError
from leadgen.web import create_app

PRAGUE = ZoneInfo("Europe/Prague")
THURSDAY_10 = datetime(2026, 10, 22, 10, 0, tzinfo=PRAGUE)

CONVERSATION = """AI: Dobrý den, tady virtuální asistentka Freeheating.
User: Dobrý den, u telefonu Novák, správa budov.
AI: Volám kvůli záložnímu vytápění.
User: To se hodí, v listopadu měníme kotel a potřebujeme teplo na tři týdny.
"""


def analysis(**fields):
    data = dict(
        outcome="hot", summary="Mění kotel v listopadu, chtějí nabídku.", needs="Mobilní kotelna na 3 týdny",
        next_step="Poslat nabídku a zavolat do pátku", urgency="this_month", contact_name="Jan Novák",
        contact_role="správce budov", contact_email="novak@sprava.cz", contact_phone=None, callback_at=None,
    )
    return CallAnalysis(**{**data, **fields})


def report(call_id, ended_reason="customer-ended-call", transcript=CONVERSATION):
    return {
        "type": "end-of-call-report",
        "endedReason": ended_reason,
        "call": {"id": call_id},
        "artifact": {"transcript": transcript, "recordingUrl": "https://rec.example/1.mp3"},
        "analysis": {"summary": "Vapi shrnutí"},
        "startedAt": "2026-10-22T08:00:00.000Z",
        "endedAt": "2026-10-22T08:03:30.000Z",
        "cost": 0.21,
    }


def dial_one(ctx, make_lead, **lead_fields):
    lead = make_lead(**lead_fields)
    assert run_dialer(ctx, THURSDAY_10) == [lead["id"]]
    return ctx.db.get_lead(lead["id"]), ctx.db.active_calls()[-1]


def outbox(ctx):
    folder = Path(ctx.settings.outbox_dir)
    return [message_from_bytes(p.read_bytes(), policy=policy.default) for p in sorted(folder.glob("*.eml"))] if folder.exists() else []


def test_dialer_respects_window_capacity_and_errors(ctx, make_lead):
    leads = [make_lead(fit_score=s) for s in (70, 90, 80)]
    assert run_dialer(ctx, datetime(2026, 10, 22, 12, 30, tzinfo=PRAGUE)) == []  # lunch
    assert run_dialer(ctx, THURSDAY_10) == [leads[1]["id"], leads[2]["id"]]  # best fit first, 2 lines
    assert run_dialer(ctx, THURSDAY_10) == []  # lines busy
    phone, name, variables = ctx.vapi.created[0]
    assert phone == leads[1]["phone"] and variables["company"] == leads[1]["company"]
    assert variables["pitch_hook"] and variables["client_name"] == "Freeheating"
    assert ctx.db.get_lead(leads[1]["id"])["status"] == "calling"

    ctx.config.calling.max_concurrent_calls = 5
    ctx.vapi.fail = VapiError("Vapi 500", status_code=500)
    assert run_dialer(ctx, THURSDAY_10) == []
    requeued = ctx.db.get_lead(leads[0]["id"])
    assert requeued["status"] == "queued" and parse_iso(requeued["next_call_at"]) > THURSDAY_10

    ctx.config.calling.enabled = False
    ctx.vapi.fail = None
    assert run_dialer(ctx, THURSDAY_10 + timedelta(hours=1)) == []


def test_extract_report_from_polled_call_object():
    r = extract_report({
        "id": "abc", "status": "ended", "endedReason": "customer-did-not-answer",
        "artifact": {"recording": {"mono": {"combinedUrl": "https://rec/1"}}},
        "startedAt": "2026-10-22T08:00:00Z", "endedAt": "2026-10-22T08:00:40Z",
    })
    assert (r.call_id, r.recording_url, r.duration_s) == ("abc", "https://rec/1", 40.0)
    assert outcome_from_ended_reason("customer-did-not-answer") == "no_answer"
    assert outcome_from_ended_reason("customer-busy") == "busy"
    assert outcome_from_ended_reason("voicemail") == "voicemail"
    assert outcome_from_ended_reason("twilio-failed-to-connect-call") == "failed"
    assert outcome_from_ended_reason("customer-ended-call") is None


def test_no_answer_is_retried_then_given_up(ctx, make_lead):
    lead, call = dial_one(ctx, make_lead)
    assert process_call(ctx, report(call["vapi_call_id"], "customer-did-not-answer", "")) == "no_answer"
    lead = ctx.db.get_lead(lead["id"])
    retry_at = parse_iso(lead["next_call_at"])
    assert lead["status"] == "queued" and retry_at >= utcnow() + timedelta(hours=3) - timedelta(minutes=1)
    assert ctx.calendar.in_window(retry_at)
    assert ctx.claude.parse_calls == []  # nothing to analyse

    ctx.db.update_lead(lead["id"], attempts=3, status="calling")
    ctx.db.add_call(lead["id"], "call-last", lead["phone"], utcnow())
    process_call(ctx, report("call-last", "customer-busy", ""))
    assert ctx.db.get_lead(lead["id"])["outcome"] == "unreachable"


def test_hot_lead_sends_instant_email_once(ctx, make_lead):
    lead, call = dial_one(ctx, make_lead)
    ctx.claude.parse_result = analysis()
    payload = report(call["vapi_call_id"])

    assert process_call(ctx, payload) == "hot"
    assert process_call(ctx, payload) is None  # webhook + poller duplicate
    assert len(ctx.claude.parse_calls) == 1

    lead = ctx.db.get_lead(lead["id"])
    assert (lead["status"], lead["outcome"]) == ("done", "hot")
    assert lead["contact_name"] == "Jan Novák" and lead["email"] == "novak@sprava.cz"
    assert lead["notified_at"]
    stored = ctx.db.get_call(call["id"])
    assert stored["needs"] == "Mobilní kotelna na 3 týdny" and stored["recording_url"].endswith("1.mp3")

    emails = outbox(ctx)
    assert len(emails) == 1
    assert "Horký lead" in emails[0]["Subject"] and lead["company"] in emails[0]["Subject"]
    assert emails[0]["To"] == "obchod@freeheating.example"


def test_do_not_call_blocks_company(ctx, make_lead):
    lead, call = dial_one(ctx, make_lead)
    ctx.claude.parse_result = analysis(outcome="do_not_call", needs=None)
    process_call(ctx, report(call["vapi_call_id"]))
    assert ctx.db.get_lead(lead["id"])["status"] == "do_not_call"
    assert ctx.db.is_blocked(None, lead["domain"], None)
    assert outbox(ctx) == []


def test_callback_is_scheduled_at_agreed_time(ctx, make_lead):
    lead, call = dial_one(ctx, make_lead)
    when = (utcnow() + timedelta(days=7)).astimezone(PRAGUE)
    while not ctx.calendar.is_working_day(when.date()):
        when += timedelta(days=1)
    agreed = when.strftime("%Y-%m-%dT14:00")
    ctx.claude.parse_result = analysis(outcome="callback", callback_at=agreed, contact_phone="777 123 456")
    process_call(ctx, report(call["vapi_call_id"]))

    lead = ctx.db.get_lead(lead["id"])
    assert lead["status"] == "queued" and lead["outcome"] == "callback"
    assert ctx.calendar.local(parse_iso(lead["next_call_at"])).strftime("%Y-%m-%dT%H:%M") == agreed
    assert lead["phone"] == "+420777123456"


def test_failed_analysis_is_retried(ctx, make_lead):
    lead, call = dial_one(ctx, make_lead)
    ctx.claude.parse_error = RuntimeError("API down")
    assert process_call(ctx, report(call["vapi_call_id"])) is None
    stored = ctx.db.get_call(call["id"])
    assert (stored["status"], stored["errors"]) == ("in_progress", 1)

    ctx.claude.parse_error = None
    ctx.claude.parse_result = analysis(outcome="not_interested")
    assert process_call(ctx, report(call["vapi_call_id"])) == "not_interested"


def test_reconcile_picks_up_lost_webhook(ctx, make_lead):
    lead, call = dial_one(ctx, make_lead)
    later = parse_iso(call["started_at"]) + timedelta(minutes=5)
    assert reconcile(ctx, later) == 0  # still running at Vapi
    ctx.vapi.calls[call["vapi_call_id"]] = {
        "id": call["vapi_call_id"], "status": "ended", "endedReason": "customer-did-not-answer",
    }
    assert reconcile(ctx, later) == 1
    assert ctx.db.get_lead(lead["id"])["outcome"] == "no_answer"


def test_webhook_and_dashboard(ctx, make_lead):
    lead, call = dial_one(ctx, make_lead)
    ctx.claude.parse_result = analysis(outcome="warm")
    with TestClient(create_app(ctx, start_scheduler=False)) as client:
        body = {"message": report(call["vapi_call_id"])}
        assert client.post("/vapi/webhook?token=spatne", json=body).status_code == 401
        assert client.post("/vapi/webhook", json={"message": {"type": "status-update"}},
                           headers={"x-vapi-secret": "tajne"}).status_code == 200
        assert client.post("/vapi/webhook?token=tajne", json=body).status_code == 200
        assert ctx.db.get_lead(lead["id"])["outcome"] == "warm"

        assert client.get("/").status_code == 401
        page = client.get("/", auth=("admin", "heslo"))
        assert page.status_code == 200 and lead["company"] in page.text
        export = client.get("/export/calls.csv", auth=("admin", "heslo"))
        assert export.content.startswith("﻿".encode()) and lead["company"] in export.content.decode("utf-8-sig")


def test_weekly_report(ctx, make_lead):
    hot, hot_call = dial_one(ctx, make_lead)
    ctx.claude.parse_result = analysis()
    process_call(ctx, report(hot_call["vapi_call_id"]))
    ctx.config.calling.max_concurrent_calls = 5
    _, missed_call = dial_one(ctx, make_lead)
    process_call(ctx, report(missed_call["vapi_call_id"], "customer-did-not-answer", ""))
    make_lead(source="test", company="Testovací SVJ")

    email = weekly_report_email(ctx, THURSDAY_10 - timedelta(days=6), THURSDAY_10 + timedelta(days=1))
    assert "1 horkých" in email.subject
    assert hot["company"] in email.html and "Mobilní kotelna na 3 týdny" in email.html
    csv_text = email.attachments[0].data.decode("utf-8-sig")
    assert csv_text.splitlines()[0].startswith("Datum hovoru;Firma")
    assert len(csv_text.strip().splitlines()) == 3  # header + 2 calls
    assert "Testovací SVJ" not in email.html
