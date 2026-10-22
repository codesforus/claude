"""AI researcher: Claude searches the web for matching companies and saves each one
through the record_company tool. Each save is checked against the database right away,
so Claude is told about duplicates and keeps looking for new companies."""

from __future__ import annotations

import logging
from typing import Callable

from leadgen.context import Context
from leadgen.discovery.models import Candidate, Task, TaskStats

log = logging.getLogger(__name__)

MAX_TURNS = 40
OTHER_SEGMENT = "jiny"

SYSTEM_TEMPLATE = """Jsi zkušený B2B rešeršér. Hledáš potenciální zákazníky pro firmu {name} ({website}). \
Nalezené firmy pak telefonicky osloví obchodník.

## Co {name} nabízí
{offer}

## Ideální zákazník
{ideal_customer}

## Koho nezařazovat
{exclude}
- firmy v likvidaci, v insolvenci nebo zaniklé
- soukromé osoby a jejich soukromá čísla

## Jak pracovat
1. Hledej na webu (web_search) a údaje ověřuj na oficiálních stránkách firmy (web_fetch), v ARES / obchodním \
rejstříku nebo ve firemním katalogu. Nic si nedomýšlej – co nemáš ověřené, nech prázdné ("").
2. Telefon musí být veřejně uvedený firemní kontakt (ústředna, obchod, technické oddělení, správa budov). \
Firmu bez dohledatelného telefonu neukládej.
3. Najdeš-li kontaktní osobu odpovědnou za provoz budov, energetiku, technickou správu nebo nákup, uveď jméno \
a funkci. Přímé číslo uveď jen tehdy, je-li zveřejněné jako pracovní kontakt.
4. Přednost mají firmy se signálem aktuální potřeby: plánovaná rekonstrukce kotelny nebo výměna zdroje tepla, \
havárie vytápění, stavba nebo rekonstrukce během topné sezóny, vypsaná veřejná zakázka nebo poptávka, \
rozšiřování provozu. Signál popiš ve fit_reason a odkaz na zdroj dej do evidence_url.
5. fit_score (0–100) = jak pravděpodobně firma potřebuje nabídku {name}: 80+ jasný signál potřeby nebo \
ideální profil, 60–79 dobrý profil bez konkrétního signálu, pod 60 spíš ne.
6. pitch_hook = jedna krátká konkrétní věta pro obchodníka, proč voláme právě jim, např. \
"Spravujete přes 40 bytových domů s vlastní plynovou kotelnou v Brně."
7. Každou firmu ulož samostatným voláním record_company, jakmile ji máš ověřenou. Když nástroj odpoví, \
že firmu už máme, nic dalšího k ní nehledej a pokračuj jinou.
8. Až máš požadovaný počet firem nebo už další vhodné nenacházíš, skonči jednou větou shrnutí."""

RecordFn = Callable[[Candidate], tuple[bool, str]]


def record_tool(segment_ids: list[str]) -> dict:
    def text(description: str) -> dict:
        return {"type": "string", "description": description}

    properties = {
        "company": text("Obchodní název firmy nebo organizace"),
        "ico": text("IČO (8 číslic), nebo prázdný řetězec"),
        "website": text("Oficiální web firmy"),
        "phone": text("Veřejný firemní telefon tak, jak je uveden na webu"),
        "email": text("Veřejný firemní e-mail, nebo prázdný řetězec"),
        "contact_name": text("Jméno kontaktní osoby, nebo prázdný řetězec"),
        "contact_role": text("Funkce kontaktní osoby, nebo prázdný řetězec"),
        "city": text("Město sídla nebo provozovny"),
        "region": text("Kraj"),
        "segment": {"type": "string", "enum": segment_ids, "description": "Segment zákazníka"},
        "fit_score": {"type": "integer", "description": "0–100, viz pravidla"},
        "fit_reason": text("Proč se firma hodí, včetně signálu potřeby"),
        "pitch_hook": text("Jedna věta pro obchodníka, proč voláme právě jim"),
        "evidence_url": text("URL, kde jsi údaje nebo signál potřeby ověřil"),
    }
    return {
        "name": "record_company",
        "description": "Uloží jednu ověřenou firmu do databáze potenciálních zákazníků. "
        "Odpověď řekne, zda byla uložena, nebo proč ne (duplicita, neplatný telefon...).",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


def system_prompt(ctx: Context) -> str:
    c = ctx.config.client
    return SYSTEM_TEMPLATE.format(
        name=c.name,
        website=c.website,
        offer=c.offer.strip(),
        ideal_customer=c.ideal_customer.strip(),
        exclude=c.exclude.strip(),
    )


def task_prompt(ctx: Context, task: Task, known: list[str]) -> str:
    n = ctx.config.discovery.companies_per_task
    if task.kind == "intent":
        intent = next(i for i in ctx.config.discovery.intent_searches if i.id == task.ref_id)
        segments = "\n".join(f"- {s.id}: {s.name}" for s in ctx.config.enabled_segments)
        head = (
            f"Zadání (hledání podle aktuální potřeby): {intent.description.strip()}\n"
            f"Oblast: {task.region}\n\n"
            f"Najdi až {n} firem nebo organizací, které tuto potřebu právě mají. "
            f"Každé přiřaď nejbližší segment:\n{segments}\n- {OTHER_SEGMENT}: jiný"
        )
    else:
        seg = ctx.config.segment(task.ref_id)
        assert seg is not None, task
        head = (
            f"Zadání: {seg.name}\n{seg.description.strip()}\n"
            f"Region: {task.region}\n\n"
            f"Najdi až {n} nových firem z tohoto segmentu a regionu (segment = {seg.id})."
        )
    if known:
        listing = "\n".join(f"- {name}" for name in known)
        head += f"\n\nTyto firmy už v databázi máme, znovu je neukládej:\n{listing}"
    return head


def _candidate(data: dict, task: Task) -> Candidate:
    def opt(key: str) -> str | None:
        value = str(data.get(key) or "").strip()
        return value or None

    segment = opt("segment")
    return Candidate(
        company=opt("company") or "",
        source="ai_search",
        source_ref=opt("evidence_url"),
        phone=opt("phone"),
        website=opt("website"),
        email=opt("email"),
        ico=opt("ico"),
        contact_name=opt("contact_name"),
        contact_role=opt("contact_role"),
        city=opt("city"),
        # Segment tasks keep the task's region so rotation and duplicate hints stay consistent.
        region=task.region if task.kind == "segment" else opt("region"),
        segment=None if segment == OTHER_SEGMENT else segment,
        fit_score=max(0, min(100, int(data.get("fit_score") or 0))),
        fit_reason=opt("fit_reason"),
        pitch_hook=opt("pitch_hook"),
    )


def _web_searches(response) -> int:
    server_tools = getattr(response.usage, "server_tool_use", None)
    return int(getattr(server_tools, "web_search_requests", 0) or 0)


def run(ctx: Context, task: Task, record: RecordFn) -> TaskStats:
    cfg = ctx.config.discovery
    segment_ids = [s.id for s in ctx.config.enabled_segments] + [OTHER_SEGMENT]
    tools = [
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": cfg.max_web_searches,
            "user_location": {"type": "approximate", "country": "CZ", "timezone": ctx.config.timezone},
        },
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 15},
        record_tool(segment_ids),
    ]
    known = ctx.db.known_companies(
        task.ref_id if task.kind == "segment" else None,
        task.region if task.kind == "segment" else None,
    )
    messages: list[dict] = [{"role": "user", "content": task_prompt(ctx, task, known)}]
    stats = TaskStats()
    searches = 0

    for _ in range(MAX_TURNS):
        response = ctx.claude.create(
            max_tokens=16000,
            system=system_prompt(ctx),
            tools=tools,
            messages=messages,
            output_config={"effort": cfg.effort},
            cache_control={"type": "ephemeral"},
        )
        messages.append({"role": "assistant", "content": response.content})
        searches += _web_searches(response)

        if response.stop_reason == "pause_turn":
            # Server-side tool loop hit its iteration limit; re-sending resumes it.
            continue
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            if response.stop_reason == "max_tokens":
                log.warning("%s: odpověď useknuta (max_tokens)", task.key)
            break

        results = []
        for block in tool_uses:
            if block.name != "record_company":
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                     "content": f"Neznámý nástroj {block.name}."}
                )
                continue
            stats.found += 1
            saved, message = record(_candidate(block.input, task))
            stats.added += int(saved)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": message})
        messages.append({"role": "user", "content": results})

        if stats.added >= cfg.companies_per_task:
            break
        if searches >= cfg.max_web_searches:
            log.info("%s: vyčerpán rozpočet %d vyhledávání", task.key, cfg.max_web_searches)
            break

    log.info("%s: nalezeno %d, uloženo %d, vyhledávání %d", task.key, stats.found, stats.added, searches)
    return stats
