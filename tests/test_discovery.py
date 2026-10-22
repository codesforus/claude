from leadgen.discovery import add_candidate, ai_search, ares, plan_tasks
from leadgen.discovery.models import Candidate, Task

from conftest import block, response


def _cand(**fields):
    data = {
        "company": "Správa Vinohrady s.r.o.",
        "source": "ai_search",
        "phone": "602 111 222",
        "website": "https://www.spravavinohrady.cz",
        "fit_score": 75,
        "segment": "spravci",
    }
    return Candidate(**{**data, **fields})


def test_add_candidate_rules(ctx):
    ok, _ = add_candidate(ctx, _cand(), verify_ico=False)
    assert ok
    lead = ctx.db.leads_by_status("queued")[0]
    assert lead["phone"] == "+420602111222" and lead["domain"] == "spravavinohrady.cz"

    assert add_candidate(ctx, _cand(website="jina.cz"), verify_ico=False)[1].startswith("DUPLICITA")
    assert add_candidate(ctx, _cand(phone="603 999 888"), verify_ico=False)[1].startswith("DUPLICITA")
    assert "telefon" in add_candidate(ctx, _cand(phone="", website="x.cz"), verify_ico=False)[1]

    ctx.db.block("+420604000000", "phone", "stávající zákazník")
    assert "nevolat" in add_candidate(ctx, _cand(phone="604 000 000", website="y.cz"), verify_ico=False)[1]

    ok, message = add_candidate(ctx, _cand(phone="605 000 000", website="z.cz", fit_score=30), verify_ico=False)
    assert ok and "nebude" in message
    assert ctx.db.leads_by_status("rejected")[0]["phone"] == "+420605000000"

    add_candidate(ctx, _cand(phone="606 000 000", website="w.cz", fit_score=None), verify_ico=False)
    assert ctx.db.leads_by_status("new")[0]["phone"] == "+420606000000"


def test_add_candidate_drops_dissolved_company(ctx, monkeypatch):
    monkeypatch.setattr(ares, "lookup", lambda ico: ares.AresRecord(ico, "X", "Praha", active=False))
    ok, message = add_candidate(ctx, _cand(ico="12345678"))
    assert not ok and "zaniklá" in message


def _record(tool_id, **fields):
    data = {
        "company": "Teplo Brno a.s.", "ico": "", "website": "https://teplobrno.cz", "phone": "+420 541 000 111",
        "email": "", "contact_name": "", "contact_role": "", "city": "Brno", "region": "Jihomoravský kraj",
        "segment": "teplarny", "fit_score": 85, "fit_reason": "Provozuje výtopny.", "pitch_hook": "Máte 5 výtopen.",
        "evidence_url": "https://teplobrno.cz/kontakt",
    }
    return block("tool_use", id=tool_id, name="record_company", input={**data, **fields})


def test_ai_search_loop(ctx):
    ctx.db.add_lead({"company": "Stará firma", "phone": "+420541999999", "source": "ai_search",
                     "status": "queued", "segment": "teplarny", "region": "Jihomoravský kraj"})
    claude = ctx.claude
    claude.responses = [
        response("pause_turn", block("server_tool_use", name="web_search"), web_searches=3),
        response("tool_use", _record("t1"), _record("t2", company="Stará firma", phone="541 999 999"), web_searches=2),
        response("end_turn", block("text", text="Hotovo.")),
    ]
    stats = ai_search.run(ctx, Task("ai_search", "segment", "teplarny", "Jihomoravský kraj"),
                          lambda c: add_candidate(ctx, c, verify_ico=False))

    assert (stats.found, stats.added) == (2, 1)
    assert len(claude.create_calls) == 3
    first_prompt = claude.create_calls[0]["messages"][0]["content"]
    assert "Stará firma" in first_prompt  # known companies are excluded up front
    # After pause_turn the conversation is resent without a new user message.
    assert claude.create_calls[1]["messages"][-1]["role"] == "assistant"
    results = claude.create_calls[2]["messages"][-1]["content"]
    assert results[0]["content"].startswith("Uloženo")
    assert results[1]["content"].startswith("DUPLICITA")
    saved = ctx.db.find_duplicate("+420541000111", None, None)
    assert saved["segment"] == "teplarny" and saved["status"] == "queued"


def test_ai_search_stops_at_search_budget(ctx):
    ctx.config.discovery.max_web_searches = 5
    ctx.claude.responses = [response("tool_use", _record("t1"), web_searches=6)]
    stats = ai_search.run(ctx, Task("ai_search", "segment", "teplarny", "Jihomoravský kraj"),
                          lambda c: add_candidate(ctx, c, verify_ico=False))
    assert stats.added == 1 and len(ctx.claude.create_calls) == 1


def test_plan_tasks_rotation(ctx):
    tasks = plan_tasks(ctx)
    assert tasks[0].kind == "intent"
    assert sum(t.kind == "intent" for t in tasks) == 1
    first_segment_task = tasks[1]
    run_id = ctx.db.start_run(first_segment_task.key)
    ctx.db.finish_run(run_id, 0, 0)
    for intent in ctx.config.discovery.intent_searches:
        ctx.db.finish_run(ctx.db.start_run(f"ai_search:{intent.id}:Česká republika"), 0, 0)

    replanned = plan_tasks(ctx)
    assert all(t.kind == "segment" for t in replanned)  # intents ran this week
    assert replanned[-1] == first_segment_task
