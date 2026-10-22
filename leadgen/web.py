"""HTTP server: Vapi webhook, a read-only dashboard and CSV exports. Runs the scheduler too."""

from __future__ import annotations

import csv
import io
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from leadgen.calls import process_call
from leadgen.context import Context, build_context
from leadgen.phones import format_phone
from leadgen.reports import OUTCOME_LABELS, calls_csv, label
from leadgen.scheduler import build_scheduler
from leadgen.timeutil import parse_iso, utcnow

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "new": "Čeká na posouzení",
    "queued": "Ve frontě",
    "calling": "Právě voláme",
    "done": "Hotovo",
    "rejected": "Nevhodné",
    "do_not_call": "Nevolat",
}
LEAD_EXPORT_COLUMNS = [
    "id", "company", "ico", "phone", "email", "contact_name", "contact_role", "website", "address",
    "city", "region", "segment", "source", "fit_score", "fit_reason", "pitch_hook", "status",
    "outcome", "attempts", "next_call_at", "last_called_at", "created_at",
]

basic_auth = HTTPBasic(auto_error=False)


def create_app(ctx: Context | None = None, start_scheduler: bool | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        context = ctx or build_context()
        app.state.ctx = context
        context.db.release_interrupted_calls()
        if not context.settings.webhook_secret:
            log.warning("WEBHOOK_SECRET není nastaven – webhook přijme požadavek od kohokoli")
        run_scheduler = context.settings.scheduler_enabled if start_scheduler is None else start_scheduler
        scheduler = build_scheduler(context) if run_scheduler else None
        if scheduler:
            scheduler.start()
            log.info("plánovač spuštěn: %s", ", ".join(j.id for j in scheduler.get_jobs()))
        yield
        if scheduler:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="AI obchodník – leady", lifespan=lifespan)

    def get_ctx(request: Request) -> Context:
        return request.app.state.ctx

    def require_admin(
        request: Request, credentials: HTTPBasicCredentials | None = Depends(basic_auth)
    ) -> None:
        s = get_ctx(request).settings
        if not s.admin_password:
            raise HTTPException(503, "Přehled je vypnutý: nastavte ADMIN_PASSWORD.")
        ok = credentials is not None and secrets.compare_digest(
            credentials.username.encode(), s.admin_user.encode()
        ) & secrets.compare_digest(credentials.password.encode(), s.admin_password.encode())
        if not ok:
            raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/vapi/webhook")
    async def vapi_webhook(request: Request, background: BackgroundTasks, token: str | None = None) -> dict:
        context = get_ctx(request)
        secret = context.settings.webhook_secret
        if secret:
            provided = token or request.headers.get("x-vapi-secret") or request.headers.get("x-webhook-secret") or ""
            if not secrets.compare_digest(provided.encode(), secret.encode()):
                raise HTTPException(401)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "neplatný JSON")
        message = (body.get("message") if isinstance(body, dict) else None) or {}
        if message.get("type") == "end-of-call-report":
            background.add_task(process_call, context, message)
        return {}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
    def dashboard(request: Request, status: str | None = None) -> str:
        return render_dashboard(get_ctx(request), status)

    @app.get("/export/leads.csv", dependencies=[Depends(require_admin)])
    def export_leads(request: Request) -> Response:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, LEAD_EXPORT_COLUMNS, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(get_ctx(request).db.all_leads())
        return _csv_response(buf.getvalue().encode("utf-8-sig"), "leady.csv")

    @app.get("/export/calls.csv", dependencies=[Depends(require_admin)])
    def export_calls(request: Request) -> Response:
        context = get_ctx(request)
        calls = context.db.calls_between(datetime(2000, 1, 1, tzinfo=timezone.utc), utcnow())
        return _csv_response(calls_csv(context, calls), "hovory.csv")

    return app


def _csv_response(data: bytes, filename: str) -> Response:
    return Response(
        data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _local(ctx: Context, value: str | None) -> str:
    dt = parse_iso(value)
    return ctx.calendar.local(dt).strftime("%d.%m. %H:%M") if dt else ""


def _recording_link(url: str | None) -> str:
    return f'<a href="{escape(url)}">▶</a>' if url else ""


def render_dashboard(ctx: Context, status: str | None) -> str:
    counts = ctx.db.status_counts()
    outcomes = ctx.db.outcome_counts()
    leads = ctx.db.recent_leads(200, status)
    calls = ctx.db.recent_calls(30)

    status_links = " ".join(
        f'<a class="chip" href="/?status={k}">{escape(v)}: <b>{counts.get(k, 0)}</b></a>'
        for k, v in STATUS_LABELS.items()
    )
    outcome_chips = " ".join(
        f'<span class="chip">{escape(OUTCOME_LABELS.get(k, k))}: <b>{n}</b></span>'
        for k, n in sorted(outcomes.items(), key=lambda kv: -kv[1])
    )
    lead_rows = "".join(
        "<tr>"
        f"<td>{escape(lead['company'])}<div class='muted'>{escape(ctx.config.segment_name(lead['segment']))}"
        f" · {escape(lead['region'] or '')}</div></td>"
        f"<td>{escape(format_phone(lead['phone']))}</td>"
        f"<td>{lead['fit_score'] if lead['fit_score'] is not None else ''}</td>"
        f"<td>{escape(STATUS_LABELS.get(lead['status'], lead['status']))}</td>"
        f"<td>{escape(label(lead['outcome']))}</td>"
        f"<td>{lead['attempts']}</td>"
        f"<td>{escape(_local(ctx, lead['next_call_at']))}</td>"
        f"<td class='muted'>{escape(lead['pitch_hook'] or lead['note'] or '')}</td>"
        "</tr>"
        for lead in leads
    )
    call_rows = "".join(
        "<tr>"
        f"<td>{escape(_local(ctx, c['started_at']))}</td>"
        f"<td>{escape(c['company'])}</td>"
        f"<td>{escape(label(c['outcome']) or c['status'])}</td>"
        f"<td class='muted'>{escape(c['summary'] or '')}</td>"
        f"<td>{_recording_link(c['recording_url'])}</td>"
        "</tr>"
        for c in calls
    )
    calling = "zapnuté" if ctx.config.calling.enabled else "VYPNUTÉ (calling.enabled v config.yaml)"
    return f"""<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>AI obchodník – leady</title>
<style>
body{{font-family:system-ui,Arial,sans-serif;margin:24px;color:#222}}
table{{border-collapse:collapse;width:100%;font-size:14px;margin-bottom:28px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
th{{background:#f5f5f5}} .muted{{color:#777;font-size:12px}}
.chip{{display:inline-block;padding:4px 10px;margin:2px;border-radius:12px;background:#f0f0f0;color:#222;text-decoration:none}}
</style></head><body>
<h1>AI obchodník pro {escape(ctx.config.client.name)}</h1>
<p>Volání: <b>{escape(calling)}</b> · <a href="/export/leads.csv">export leadů</a> · <a href="/export/calls.csv">export hovorů</a></p>
<p><a class="chip" href="/">Vše</a> {status_links}</p>
<p>{outcome_chips}</p>
<h2>Poslední hovory</h2>
<table><tr><th>Kdy</th><th>Firma</th><th>Výsledek</th><th>Shrnutí</th><th></th></tr>{call_rows}</table>
<h2>Leady{f" – {escape(STATUS_LABELS.get(status, status))}" if status else ""}</h2>
<table><tr><th>Firma</th><th>Telefon</th><th>Fit</th><th>Stav</th><th>Výsledek</th><th>Pokusy</th>
<th>Další hovor</th><th>Poznámka</th></tr>{lead_rows}</table>
</body></html>"""


app = create_app()
