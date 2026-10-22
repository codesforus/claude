"""Emails for the client: instant alert for a hot lead, weekly summary with a CSV export."""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
from collections import Counter
from datetime import datetime, timedelta
from html import escape

from leadgen.context import Context
from leadgen.notify import Attachment, Email
from leadgen.phones import format_phone
from leadgen.timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)

# The webhook thread and the periodic job may both try to alert; one email per lead.
_alert_lock = threading.Lock()

OUTCOME_LABELS = {
    "hot": "Horký zájem",
    "warm": "Vlažný zájem",
    "callback": "Zavolat znovu",
    "not_interested": "Bez zájmu",
    "do_not_call": "Nepřeje si kontakt",
    "wrong_number": "Špatné číslo",
    "no_answer": "Nezvedli",
    "busy": "Obsazeno",
    "voicemail": "Hlasová schránka",
    "failed": "Technická chyba",
    "unreachable": "Nedovoláno (vyčerpány pokusy)",
    "error": "Chyba vyhodnocení",
}
CONVERSATION_OUTCOMES = {"hot", "warm", "callback", "not_interested", "do_not_call", "wrong_number"}
URGENCY_LABELS = {"immediate": "ihned", "this_month": "tento měsíc", "later": "později", "unknown": "neuvedeno"}

STYLE_TABLE = "border-collapse:collapse;width:100%;font-size:14px"
STYLE_TH = "text-align:left;padding:6px 8px;border-bottom:2px solid #ccc;background:#f5f5f5"
STYLE_TD = "padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top"


def label(outcome: str | None) -> str:
    return OUTCOME_LABELS.get(outcome or "", outcome or "")


def _local(ctx: Context, value: str | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    dt = parse_iso(value)
    return ctx.calendar.local(dt).strftime(fmt) if dt else ""


def _link(url: str | None, text: str) -> str:
    return f'<a href="{escape(url)}">{escape(text)}</a>' if url else ""


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Cells are expected to be HTML-escaped already."""
    head = "".join(f'<th style="{STYLE_TH}">{escape(h)}</th>' for h in headers)
    body = "".join(
        "<tr>" + "".join(f'<td style="{STYLE_TD}">{cell}</td>' for cell in row) + "</tr>" for row in rows
    )
    return f'<table style="{STYLE_TABLE}"><tr>{head}</tr>{body}</table>'


def _page(title: str, body: str) -> str:
    return (
        '<html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;max-width:900px">'
        f"<h2>{escape(title)}</h2>{body}"
        '<p style="color:#888;font-size:12px;margin-top:32px">Automaticky generováno systémem AI obchodníka.</p>'
        "</body></html>"
    )


def _urgency(call: dict) -> str:
    try:
        return URGENCY_LABELS.get(json.loads(call.get("analysis_json") or "{}").get("urgency"), "")
    except ValueError:
        return ""


# --- instant alert -------------------------------------------------------------


def instant_alert_email(ctx: Context, lead: dict, call: dict | None) -> Email:
    call = call or {}
    outcome_label = label(lead["outcome"])
    prefix = "🔥 Horký lead" if lead["outcome"] == "hot" else outcome_label
    subject = f"{prefix}: {lead['company']}"
    if call.get("needs"):
        subject += f" – {call['needs'][:80]}"

    rows = [
        ("Co potřebují", escape(call.get("needs") or "")),
        ("Doporučený další krok", escape(call.get("next_step") or "")),
        ("Naléhavost", escape(_urgency(call))),
        ("Kontaktní osoba", escape(" – ".join(x for x in (lead.get("contact_name"), lead.get("contact_role")) if x))),
        ("Telefon", f'<a href="tel:{escape(lead["phone"] or "")}">{escape(format_phone(lead["phone"]))}</a>'),
        ("E-mail", _link(f"mailto:{lead['email']}", lead["email"]) if lead.get("email") else ""),
        ("Web", _link(lead.get("website"), lead.get("website") or "")),
        ("IČO", escape(lead.get("ico") or "")),
        ("Adresa", escape(lead.get("address") or lead.get("city") or lead.get("region") or "")),
        ("Segment", escape(ctx.config.segment_name(lead.get("segment")))),
        ("Hovor", escape(_local(ctx, call.get("started_at")))),
        ("Nahrávka", _link(call.get("recording_url"), "poslechnout")),
    ]
    details = "".join(
        f'<tr><td style="{STYLE_TD};font-weight:bold;width:200px">{escape(k)}</td><td style="{STYLE_TD}">{v}</td></tr>'
        for k, v in rows
        if v
    )
    body = (
        f"<p>AI obchodník právě dokončil hovor s výsledkem <b>{escape(outcome_label)}</b>. "
        "Ozvěte se jim prosím co nejdřív.</p>"
        f'<table style="{STYLE_TABLE}">{details}</table>'
        f"<h3>Shrnutí hovoru</h3><p>{escape(call.get('summary') or '')}</p>"
        f"<h3>Proč jsme jim volali</h3><p>{escape(lead.get('fit_reason') or '')}</p>"
        f'<h3>Přepis</h3><pre style="white-space:pre-wrap;font-size:13px;background:#fafafa;padding:12px">'
        f"{escape(call.get('transcript') or '')}</pre>"
    )
    text = (
        f"{subject}\n\nCo potřebují: {call.get('needs') or ''}\nDalší krok: {call.get('next_step') or ''}\n"
        f"Kontakt: {lead.get('contact_name') or ''} {lead.get('contact_role') or ''}\n"
        f"Telefon: {format_phone(lead['phone'])}\nE-mail: {lead.get('email') or ''}\n"
        f"Nahrávka: {call.get('recording_url') or ''}\n\nShrnutí: {call.get('summary') or ''}\n"
    )
    return Email(subject=subject, html=_page(lead["company"], body), text=text)


def send_instant_alerts(ctx: Context) -> int:
    """Email every lead whose outcome needs immediate attention and hasn't been sent yet."""
    with _alert_lock:
        return _send_pending_alerts(ctx)


def _send_pending_alerts(ctx: Context) -> int:
    sent = 0
    for lead in ctx.db.leads_to_alert(ctx.config.reporting.instant_outcomes):
        email = instant_alert_email(ctx, lead, ctx.db.last_call_for_lead(lead["id"]))
        try:
            if lead["source"] == "test":
                email.subject = f"[TEST] {email.subject}"
                ctx.mailer.send(email, to=ctx.settings.test_email, cc=[])
            else:
                ctx.mailer.send(email)
        except Exception:
            log.exception("upozornění na lead %s se nepodařilo odeslat", lead["id"])
            break  # SMTP problem; the next run retries
        ctx.db.update_lead(lead["id"], notified_at=iso(utcnow()))
        sent += 1
    return sent


# --- weekly report -------------------------------------------------------------

CSV_COLUMNS = [
    ("Datum hovoru", lambda ctx, c: _local(ctx, c["started_at"])),
    ("Firma", lambda ctx, c: c["company"]),
    ("IČO", lambda ctx, c: c.get("ico") or ""),
    ("Telefon", lambda ctx, c: format_phone(c["phone"])),
    ("Kontaktní osoba", lambda ctx, c: c.get("contact_name") or ""),
    ("Funkce", lambda ctx, c: c.get("contact_role") or ""),
    ("E-mail", lambda ctx, c: c.get("email") or ""),
    ("Web", lambda ctx, c: c.get("website") or ""),
    ("Segment", lambda ctx, c: ctx.config.segment_name(c.get("segment"))),
    ("Výsledek", lambda ctx, c: label(c.get("outcome"))),
    ("Co potřebují", lambda ctx, c: c.get("needs") or ""),
    ("Další krok", lambda ctx, c: c.get("next_step") or ""),
    ("Shrnutí", lambda ctx, c: c.get("summary") or ""),
    ("Délka (s)", lambda ctx, c: f"{c['duration_s']:.0f}" if c.get("duration_s") else ""),
    ("Nahrávka", lambda ctx, c: c.get("recording_url") or ""),
]


def calls_csv(ctx: Context, calls: list[dict]) -> bytes:
    """Semicolon-separated with BOM, so Czech Excel opens it correctly with a double-click."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([name for name, _ in CSV_COLUMNS])
    for call in calls:
        writer.writerow([fn(ctx, call) for _, fn in CSV_COLUMNS])
    return buf.getvalue().encode("utf-8-sig")


def _lead_rows(ctx: Context, calls: list[dict]) -> list[list[str]]:
    return [
        [
            f"<b>{escape(c['company'])}</b><br>{escape(ctx.config.segment_name(c.get('segment')))}",
            escape(" – ".join(x for x in (c.get("contact_name"), c.get("contact_role")) if x)),
            escape(format_phone(c["phone"])) + (f"<br>{escape(c['email'])}" if c.get("email") else ""),
            escape(c.get("needs") or c.get("summary") or ""),
            escape(c.get("next_step") or ""),
            _link(c.get("recording_url"), "nahrávka"),
        ]
        for c in calls
    ]


def weekly_report_email(ctx: Context, start: datetime, end: datetime) -> Email:
    client = ctx.config.client.name
    calls = [c for c in ctx.db.calls_between(start, end) if c["source"] != "test"]
    ended = [c for c in calls if c["status"] == "ended"]
    outcomes = Counter(c["outcome"] for c in ended)
    hot = [c for c in ended if c["outcome"] == "hot"]
    warm = [c for c in ended if c["outcome"] == "warm"]
    conversations = sum(outcomes[o] for o in CONVERSATION_OUTCOMES)
    callbacks = ctx.db.upcoming_callbacks(end, end + timedelta(days=7))
    queued = ctx.db.status_counts().get("queued", 0)
    new_leads = ctx.db.count_leads_created(start, end)
    period = f"{ctx.calendar.local(start):%d.%m.} – {ctx.calendar.local(end - timedelta(seconds=1)):%d.%m.%Y}"

    kpis = [
        ("Nově nalezené firmy", new_leads),
        ("Hovory", len(calls)),
        ("Dovoláno (rozhovor)", conversations),
        ("Horký zájem", len(hot)),
        ("Vlažný zájem", len(warm)),
        ("Ve frontě k volání", queued),
    ]
    kpi_html = "".join(
        f'<td style="padding:10px;text-align:center;border:1px solid #eee">'
        f'<div style="font-size:26px;font-weight:bold">{value}</div>'
        f'<div style="font-size:12px;color:#666">{escape(name)}</div></td>'
        for name, value in kpis
    )
    headers = ["Firma", "Kontakt", "Telefon / e-mail", "Co potřebují", "Další krok", ""]
    body = f'<p>Období: <b>{escape(period)}</b></p><table style="{STYLE_TABLE}"><tr>{kpi_html}</tr></table>'
    body += "<h3>🔥 Horký zájem</h3>" + (_table(headers, _lead_rows(ctx, hot)) if hot else "<p>Tento týden žádný.</p>")
    body += "<h3>Vlažný zájem</h3>" + (_table(headers, _lead_rows(ctx, warm)) if warm else "<p>Tento týden žádný.</p>")
    if callbacks:
        body += "<h3>Domluvené zpětné hovory na příští týden</h3>" + _table(
            ["Kdy", "Firma", "Kontakt"],
            [
                [escape(_local(ctx, c["next_call_at"])), escape(c["company"]), escape(c.get("contact_name") or "")]
                for c in callbacks
            ],
        )
    body += "<h3>Výsledky všech hovorů</h3>" + _table(
        ["Výsledek", "Počet"],
        [[escape(label(o)), str(n)] for o, n in outcomes.most_common()] or [["–", "0"]],
    )
    body += "<p>Kompletní seznam hovorů je v příloze (CSV, otevře se v Excelu).</p>"

    subject = f"{client}: týdenní report AI obchodníka {period} – {len(hot)} horkých, {len(warm)} vlažných"
    text = (
        f"{subject}\n\nNově nalezené firmy: {new_leads}\nHovory: {len(calls)}\nDovoláno: {conversations}\n"
        + "".join(f"- {c['company']} ({format_phone(c['phone'])}): {c.get('needs') or c.get('summary') or ''}\n" for c in hot + warm)
    )
    attachment = Attachment(
        filename=f"hovory-{ctx.calendar.local(start):%Y-%m-%d}.csv",
        data=calls_csv(ctx, calls),
        mimetype="text/csv",
    )
    return Email(subject=subject, html=_page(f"Týdenní report – {client}", body), text=text, attachments=[attachment])


def send_weekly_report(ctx: Context, now: datetime | None = None, days: int = 7) -> Email:
    end = now or utcnow()
    email = weekly_report_email(ctx, end - timedelta(days=days), end)
    ctx.mailer.send(email)
    return email
