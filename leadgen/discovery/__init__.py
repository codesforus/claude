"""Discovery orchestration: decide what to search next, run sources, store new leads."""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

from leadgen.context import Context
from leadgen.discovery import ai_search, ares, google_places
from leadgen.discovery.models import Candidate, Task, TaskStats
from leadgen.phones import domain_of, normalize_email, normalize_ico, normalize_phone
from leadgen.qualify import qualify_pending
from leadgen.timeutil import parse_iso, utcnow

log = logging.getLogger(__name__)

COUNTRY = "Česká republika"
INTENT_REFRESH = timedelta(days=7)


def add_candidate(ctx: Context, c: Candidate, verify_ico: bool = True) -> tuple[bool, str]:
    """Deduplicate, check the do-not-call list and store. Returns (saved, message for the AI)."""
    if not c.company:
        return False, "NEULOŽENO: chybí název firmy."
    phone = normalize_phone(c.phone)
    if not phone:
        return False, "NEULOŽENO: chybí platné české telefonní číslo. Dohledej firemní telefon, nebo pokračuj jinou firmou."
    domain = domain_of(c.website)
    ico = normalize_ico(c.ico)
    if ctx.db.is_blocked(phone, domain, ico):
        return False, "NEULOŽENO: firma je na seznamu nevolat (stávající zákazník nebo odmítla kontakt). Pokračuj jinou."
    if ctx.db.find_duplicate(phone, domain, ico):
        return False, "DUPLICITA: tuto firmu už v databázi máme. Pokračuj jinou."

    address = c.address
    if ico and verify_ico:
        record = ares.lookup(ico)
        if record is None:
            ico = None  # unknown in the register: keep the lead, drop the unverified IČO
        elif not record.active:
            return False, "NEULOŽENO: firma je podle ARES zaniklá. Pokračuj jinou."
        else:
            address = address or record.address

    min_score = ctx.config.discovery.min_fit_score
    if c.fit_score is None:
        status = "new"
    else:
        status = "queued" if c.fit_score >= min_score else "rejected"

    try:
        lead_id = ctx.db.add_lead(
            {
                "company": c.company,
                "ico": ico,
                "website": c.website,
                "domain": domain,
                "phone": phone,
                "email": normalize_email(c.email),
                "contact_name": c.contact_name,
                "contact_role": c.contact_role,
                "address": address,
                "city": c.city,
                "region": c.region,
                "segment": c.segment,
                "source": c.source,
                "source_ref": c.source_ref,
                "fit_score": c.fit_score,
                "fit_reason": c.fit_reason,
                "pitch_hook": c.pitch_hook,
                "status": status,
                "note": f"fit_score pod {min_score}" if status == "rejected" else None,
            }
        )
    except sqlite3.IntegrityError:
        return False, "DUPLICITA: tento telefon už v databázi máme. Pokračuj jinou firmou."

    if status == "rejected":
        return True, f"Uloženo (id {lead_id}), ale s fit_score {c.fit_score} se volat nebude. Hledej vhodnější firmy."
    return True, f"Uloženo (id {lead_id})."


def plan_tasks(ctx: Context) -> list[Task]:
    """Rotate through every source × segment × region, least recently searched first.
    Intent searches (tenders, current demand) are refreshed weekly and go first."""
    cfg = ctx.config.discovery
    last = ctx.db.last_run_times()
    now = utcnow()

    intents = [
        Task("ai_search", "intent", i.id, COUNTRY)
        for i in cfg.intent_searches
        if i.enabled and "ai_search" in cfg.sources
    ]
    stale_intents = [
        t for t in intents
        if (ran := parse_iso(last.get(t.key))) is None or now - ran >= INTENT_REFRESH
    ]
    stale_intents.sort(key=lambda t: last.get(t.key, ""))

    rotation: list[Task] = []
    for region in cfg.regions:
        for seg in ctx.config.enabled_segments:
            if "ai_search" in cfg.sources:
                rotation.append(Task("ai_search", "segment", seg.id, region))
            if "google_places" in cfg.sources and ctx.settings.google_maps_api_key and seg.places_queries:
                rotation.append(Task("google_places", "segment", seg.id, region))
    # Stable sort keeps region-major order among tasks that never ran.
    rotation.sort(key=lambda t: last.get(t.key, ""))

    return stale_intents[:1] + rotation


def run_task(ctx: Context, task: Task) -> TaskStats:
    if task.source == "ai_search":
        return ai_search.run(ctx, task, lambda c: add_candidate(ctx, c))

    seg = ctx.config.segment(task.ref_id)
    stats = TaskStats()
    for query in seg.places_queries if seg else []:
        for candidate in google_places.search(
            ctx.settings.google_maps_api_key, query, task.region, task.ref_id
        ):
            stats.found += 1
            saved, _ = add_candidate(ctx, candidate, verify_ico=False)
            stats.added += int(saved)
    return stats


def run_discovery(ctx: Context, max_tasks: int | None = None, force: bool = False) -> list[tuple[Task, TaskStats]]:
    cfg = ctx.config.discovery
    queued = ctx.db.status_counts().get("queued", 0)
    if not force and queued >= cfg.target_queue_size:
        log.info("ve frontě je %d firem (cíl %d), hledání přeskočeno", queued, cfg.target_queue_size)
        return []

    done = []
    for task in plan_tasks(ctx)[: max_tasks or cfg.tasks_per_run]:
        run_id = ctx.db.start_run(task.key)
        try:
            stats = run_task(ctx, task)
        except Exception as e:
            log.exception("hledání %s selhalo", task.key)
            ctx.db.finish_run(run_id, 0, 0, error=str(e)[:500])
            continue
        ctx.db.finish_run(run_id, stats.found, stats.added)
        done.append((task, stats))

    qualify_pending(ctx)
    return done

