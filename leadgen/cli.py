"""Command line: `python -m leadgen <příkaz>`. Run `python -m leadgen -h` for the list."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import timedelta
from pathlib import Path

from leadgen.context import Context, build_context
from leadgen.phones import domain_of, format_phone, normalize_ico, normalize_phone
from leadgen.timeutil import utcnow


def _read_csv(path: str) -> list[dict[str, str]]:
    text = Path(path).read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,\t")
    return [
        {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        for row in csv.DictReader(text.splitlines(), dialect=dialect)
    ]


def _pick(row: dict[str, str], *names: str) -> str | None:
    return next((row[n] for n in names if row.get(n)), None)


def cmd_init(ctx: Context, args: argparse.Namespace) -> None:
    print(f"Databáze: {ctx.settings.database_path}")
    print(f"Segmenty: {', '.join(s.id for s in ctx.config.enabled_segments)}")
    print(f"Regiony: {len(ctx.config.discovery.regions)}, zdroje: {', '.join(ctx.config.discovery.sources)}")
    print(f"Volání: {'zapnuto' if ctx.config.calling.enabled else 'vypnuto'}")
    print(f"Webhook: {ctx.settings.webhook_url or '(PUBLIC_BASE_URL není nastavena, použije se jen dotazování Vapi)'}")


def cmd_discover(ctx: Context, args: argparse.Namespace) -> None:
    from leadgen.discovery import run_discovery

    for task, stats in run_discovery(ctx, max_tasks=args.tasks, force=args.force):
        print(f"{task.key}: nalezeno {stats.found}, uloženo {stats.added}")


def cmd_plan(ctx: Context, args: argparse.Namespace) -> None:
    from leadgen.discovery import plan_tasks

    for task in plan_tasks(ctx)[: args.n]:
        print(task.key)


def cmd_qualify(ctx: Context, args: argparse.Namespace) -> None:
    from leadgen.qualify import qualify_pending

    print(f"Posouzeno: {qualify_pending(ctx, limit=args.limit)}")


def cmd_preview(ctx: Context, args: argparse.Namespace) -> None:
    now = utcnow()
    print(f"Teď {'je' if ctx.calendar.in_window(now) else 'není'} okno pro volání.")
    for lead in ctx.db.due_leads(now, args.n):
        print(f"[{lead['fit_score']}] {lead['company']} {format_phone(lead['phone'])} – {lead['pitch_hook'] or ''}")


def cmd_dial(ctx: Context, args: argparse.Namespace) -> None:
    from leadgen.dialer import run_dialer

    started = run_dialer(ctx)
    print(f"Spuštěno hovorů: {len(started)}" if started else "Nic k volání (mimo okno, plná kapacita, prázdná fronta nebo vypnuto).")


def cmd_reconcile(ctx: Context, args: argparse.Namespace) -> None:
    from leadgen.calls import reconcile

    print(f"Zpracováno hovorů: {reconcile(ctx)}")


def cmd_test_call(ctx: Context, args: argparse.Namespace) -> None:
    from leadgen.dialer import call_variables

    phone = normalize_phone(args.phone)
    if not phone:
        sys.exit(f"Neplatné číslo: {args.phone}")
    lead = ctx.db.find_duplicate(phone, None, None)
    if lead is None:
        lead_id = ctx.db.add_lead({
            "company": args.company, "phone": phone, "source": "test", "status": "calling",
            "segment": ctx.config.enabled_segments[0].id, "fit_score": 100,
            "pitch_hook": args.hook, "city": "Praha",
        })
    elif lead["source"] != "test":
        sys.exit(f"Číslo patří skutečnému leadu ({lead['company']}), testovací hovor nespouštím.")
    else:
        lead_id = lead["id"]
        ctx.db.update_lead(lead_id, status="calling", outcome=None, notified_at=None)
    lead = ctx.db.get_lead(lead_id)
    call = ctx.vapi.create_call(phone, lead["company"], call_variables(ctx, lead))
    ctx.db.add_call(lead_id, call["id"], phone, utcnow())
    ctx.db.update_lead(lead_id, attempts=lead["attempts"] + 1)
    print(f"Volám {format_phone(phone)}, Vapi call id {call['id']}. Výsledek zpracuje webhook nebo `reconcile`.")


def cmd_report(ctx: Context, args: argparse.Namespace) -> None:
    from leadgen.reports import send_weekly_report, weekly_report_email

    if args.send:
        email = send_weekly_report(ctx, days=args.days)
        print(f"Odesláno: {email.subject}")
    else:
        end = utcnow()
        email = weekly_report_email(ctx, end - timedelta(days=args.days), end)
        path = ctx.mailer.save_to_outbox(email)
        print(f"{email.subject}\nUloženo do {path} (odeslání: --send)")


def cmd_alerts(ctx: Context, args: argparse.Namespace) -> None:
    from leadgen.reports import send_instant_alerts

    print(f"Odesláno upozornění: {send_instant_alerts(ctx)}")


def cmd_import_leads(ctx: Context, args: argparse.Namespace) -> None:
    from leadgen.discovery import add_candidate
    from leadgen.discovery.models import Candidate

    saved = skipped = 0
    for row in _read_csv(args.file):
        candidate = Candidate(
            company=_pick(row, "company", "firma", "nazev", "název") or "",
            source=args.source,
            phone=_pick(row, "phone", "telefon", "tel"),
            website=_pick(row, "website", "web", "url"),
            email=_pick(row, "email", "e-mail"),
            ico=_pick(row, "ico", "ičo"),
            contact_name=_pick(row, "contact_name", "kontakt", "jmeno", "jméno"),
            contact_role=_pick(row, "contact_role", "funkce", "pozice"),
            city=_pick(row, "city", "mesto", "město"),
            region=_pick(row, "region", "kraj"),
            segment=_pick(row, "segment"),
            fit_score=args.score,
            fit_reason=_pick(row, "note", "poznamka", "poznámka"),
            pitch_hook=_pick(row, "pitch_hook", "hook"),
        )
        ok, message = add_candidate(ctx, candidate)
        saved += int(ok)
        skipped += int(not ok)
        if not ok:
            print(f"- {candidate.company}: {message}")
    print(f"Uloženo {saved}, přeskočeno {skipped}." + ("" if args.score is not None else " Nové leady posoudí AI (`qualify`)."))


def _block_row(ctx: Context, row: dict[str, str], reason: str) -> int:
    blocked = 0
    for value, kind in (
        (normalize_phone(_pick(row, "phone", "telefon", "tel")), "phone"),
        (domain_of(_pick(row, "website", "web", "url", "domain", "domena", "doména")), "domain"),
        (normalize_ico(_pick(row, "ico", "ičo")), "ico"),
    ):
        if value:
            ctx.db.block(value, kind, reason)
            blocked += 1
    return blocked


def cmd_block(ctx: Context, args: argparse.Namespace) -> None:
    normalize = {"phone": normalize_phone, "domain": domain_of, "ico": normalize_ico}[args.kind]
    value = normalize(args.value)
    if not value:
        sys.exit(f"Neplatná hodnota pro {args.kind}: {args.value}")
    ctx.db.block(value, args.kind, args.reason)
    print(f"Blokováno: {value}")


def cmd_import_blocklist(ctx: Context, args: argparse.Namespace) -> None:
    total = sum(_block_row(ctx, row, args.reason) for row in _read_csv(args.file))
    print(f"Blokováno hodnot: {total}")


def cmd_stats(ctx: Context, args: argparse.Namespace) -> None:
    print("Stavy leadů:", ctx.db.status_counts())
    print("Výsledky:", ctx.db.outcome_counts())
    print("Právě probíhající hovory:", len(ctx.db.active_calls()))
    print("Hovory dnes:", ctx.db.count_calls_since(ctx.calendar.day_start(utcnow())))


def cmd_serve(ctx: Context | None, args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("leadgen.web:app", host=args.host, port=args.port, log_level="info")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="leadgen", description="AI vyhledávání firem a volání přes Vapi")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="vytvoří databázi a vypíše nastavení").set_defaults(fn=cmd_init)

    s = sub.add_parser("discover", help="najde nové firmy (AI rešerše / Google Places)")
    s.add_argument("--tasks", type=int, help="kolik úloh spustit (výchozí podle configu)")
    s.add_argument("--force", action="store_true", help="hledat i když je fronta plná")
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("plan", help="vypíše, co se bude hledat příště")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("qualify", help="AI posoudí nové leady bez skóre")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_qualify)

    s = sub.add_parser("preview", help="koho bude systém volat jako další")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(fn=cmd_preview)

    sub.add_parser("dial", help="jedno kolo vytáčení").set_defaults(fn=cmd_dial)
    sub.add_parser("reconcile", help="dotáhne výsledky hovorů z Vapi").set_defaults(fn=cmd_reconcile)

    s = sub.add_parser("test-call", help="zkušební hovor na vlastní číslo")
    s.add_argument("phone")
    s.add_argument("--company", default="Testovací SVJ Vinohrady")
    s.add_argument("--hook", default="Spravujete bytový dům s vlastní plynovou kotelnou.")
    s.set_defaults(fn=cmd_test_call)

    s = sub.add_parser("report", help="týdenní report (bez --send jen uloží do outboxu)")
    s.add_argument("--send", action="store_true")
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(fn=cmd_report)

    sub.add_parser("alerts", help="odešle čekající okamžitá upozornění").set_defaults(fn=cmd_alerts)

    s = sub.add_parser("import-leads", help="nahraje firmy z CSV (firma;telefon;web;...)")
    s.add_argument("file")
    s.add_argument("--source", default="import")
    s.add_argument("--score", type=int, help="rovnou nastavit fit_score (jinak posoudí AI)")
    s.set_defaults(fn=cmd_import_leads)

    s = sub.add_parser("block", help="přidá telefon / doménu / IČO na seznam nevolat")
    s.add_argument("value")
    s.add_argument("--kind", choices=["phone", "domain", "ico"], default="phone")
    s.add_argument("--reason", default="ručně")
    s.set_defaults(fn=cmd_block)

    s = sub.add_parser("import-blocklist", help="seznam nevolat z CSV (např. stávající zákazníci)")
    s.add_argument("file")
    s.add_argument("--reason", default="stávající zákazník")
    s.set_defaults(fn=cmd_import_blocklist)

    sub.add_parser("stats", help="přehled čísel").set_defaults(fn=cmd_stats)

    s = sub.add_parser("serve", help="spustí server (webhook, přehled, plánovač)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve, no_context=True)
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    ctx = None if getattr(args, "no_context", False) else build_context()
    args.fn(ctx, args)
