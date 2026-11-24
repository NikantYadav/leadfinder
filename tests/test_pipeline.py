"""Pipeline orchestration: stage recording, skip-on-rerun, failures don't stop later stages (except persona)."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _setup():
    from leadfinder import config, db
    tmp = Path(tempfile.mkdtemp())
    config.DB_PATH, config.OUTPUT = tmp / "t.db", tmp
    con = db.connect()
    con.execute("INSERT INTO industries(slug,name,geo,persona_text,icp_json,icp_approved) VALUES ('p','P','IN','buyers',?,1)",
                (Path(__file__).resolve().parent.parent.joinpath("tests/fixtures/icp_example.json").read_text(),))
    con.commit()
    return con


def test_stages_recorded_failure_isolated_and_rerun_skips():
    from leadfinder import pipeline
    con = _setup()
    calls = []

    def ok(name):
        def f(con, ind, opts):
            calls.append(name)
            return {"n": 1, "leads": 1, "done": 1, "companies": 1}
        return f

    def boom(con, ind, opts):
        calls.append("web")
        raise RuntimeError("search provider down")
    pipeline.RUNNERS.update({k: ok(k) for k in pipeline.RUNNERS})
    pipeline.RUNNERS["web"] = boom
    assert pipeline.run("p", {}) == 0
    st = {r["stage"]: r for r in con.execute("SELECT * FROM pipeline_stages").fetchall()}
    assert st["web"]["status"] == "failed" and "provider down" in json.loads(st["web"]["detail"])["error"]
    assert st["export"]["status"] == "done"            # later stages still ran
    calls.clear()
    pipeline.RUNNERS["web"] = ok("web")
    pipeline.run("p", {})
    # re-run retries what didn't finish (web), and the cheap steps after it run again for the new people
    assert calls == ["web", "verify", "emails", "export", "summary"]
    calls.clear()
    pipeline.run("p", {})
    assert calls == ["summary"]                         # everything done: only the report is re-written
    calls.clear()
    pipeline.run("p", {}, from_stage="verify")
    assert calls == ["verify", "emails", "export", "summary"]


def test_persona_failure_stops_pipeline():
    from leadfinder import pipeline
    con = _setup()
    con.execute("UPDATE industries SET icp_json=NULL"); con.commit()
    calls = []
    pipeline.RUNNERS.update({k: (lambda k: lambda c, i, o: calls.append(k) or {})(k) for k in pipeline.RUNNERS})
    pipeline.RUNNERS["persona"] = lambda c, i, o: (_ for _ in ()).throw(RuntimeError("no personas"))
    assert pipeline.run("p", {}) == 1 and calls == []


def test_three_show_rule_relaxes_when_nobody_qualifies():
    from leadfinder import db
    from leadfinder.people import companies_for_icp
    from leadfinder.pipeline import _decide_min_shows
    con = _setup()
    ind = db.get_industry(con, "p")
    for i, d in enumerate(["2025-02-01", "2025-07-31", "2025-09-11"]):
        con.execute("INSERT INTO shows(industry_id,name,year,start_date) VALUES (?,?,?,?)", (ind["id"], f"S{i}", 2025, d))
    big = db.upsert_company(con, "Bobst India", industry_id=ind["id"])
    small = db.upsert_company(con, "Tiny Pack", industry_id=ind["id"])
    for sid in (1, 2):
        db.add_evidence(con, "company", big, "exhibitor", "confirmed", f"https://x/{sid}", show_id=sid)
    db.add_evidence(con, "company", small, "exhibitor", "confirmed", "https://x/3", show_id=3)
    con.commit()
    icp = {"code": "P1", "company_rule": "exhibitor_3plus"}
    comps, m = companies_for_icp(con, ind, icp, 3, min_count=1)
    assert m == 2 and [c["name"] for c in comps] == ["Bobst India"]
    comps, m = companies_for_icp(con, ind, icp, 3)          # fewer than 10 -> relax further, best first
    assert m == 1 and [c["name"] for c in comps] == ["Bobst India", "Tiny Pack"]
    assert _decide_min_shows(con, ind) == 1
    assert json.loads(db.get_industry(con, "p")["options_json"])["min_shows"] == 1


def test_empty_results_are_warnings_not_done():
    from leadfinder.pipeline import _empty_warning
    assert "No people matched" in _empty_warning("export", {"leads": 0})
    assert _empty_warning("export", {"leads": 5}) is None
    assert "found nobody" in _empty_warning("linkedin", {"P1": {"error": "no companies"}, "seconds": 0})


def test_people_planner_ranks_best_first_and_limit_does_not_stall():
    """Regression: the web stage planned only the first 60 companies (by id) and re-running never advanced."""
    from leadfinder import db
    from leadfinder.people import plan_people_searches, run_queue
    con = _setup()
    ind = db.get_industry(con, "p")
    con.execute("INSERT INTO shows(industry_id,name,year,start_date) VALUES (?,?,?,?)", (ind["id"], "S0", 2025, "2025-02-01"))
    ids = [db.upsert_company(con, f"Acme{chr(97 + i % 26)}{chr(97 + i // 26)} Works", industry_id=ind["id"]) for i in range(30)]
    for cid in ids:  # every company has buyer evidence, so ICP C applies to all of them
        db.add_evidence(con, "company", cid, "visitor_post", "inferred", f"https://v/{cid}")
    for cid in ids[-3:]:  # the LAST three companies are the only ones seen at a show
        db.add_evidence(con, "company", cid, "exhibitor", "confirmed", f"https://x/{cid}", show_id=1)
    con.commit()
    n = lambda: con.execute("SELECT COUNT(*) FROM search_queue").fetchone()[0]  # noqa: E731
    r1 = plan_people_searches(con, ind, limit=5, min_shows=1)
    first = n()
    assert 5 <= r1["planned"] <= 6 and first == r1["planned"]
    names = [json.loads(x["context"])["company"] for x in con.execute("SELECT context FROM search_queue ORDER BY id")]
    top = {con.execute("SELECT name FROM companies WHERE id=?", (cid,)).fetchone()[0] for cid in ids[-3:]}
    assert names[0] in top  # the three companies seen at a show come first, though they have the highest ids
    r2 = plan_people_searches(con, ind, limit=5, min_shows=1)  # same call again must queue NEW searches
    assert r2["planned"] > 0 and n() > first
    plan_people_searches(con, ind, min_shows=1)
    total = n()
    assert plan_people_searches(con, ind, min_shows=1)["planned"] == 0 and n() == total  # idempotent
    # a deadline in the past stops the queue loop immediately and says why
    res = run_queue(con, max_items=5, log=lambda m: None, industry_id=ind["id"], deadline=1)
    assert res["done"] == 0 and "time budget" in res["stopped"] and res["pending"] == total


def test_tinysearch_is_paced_and_waits_out_short_429():
    from leadfinder import config
    from leadfinder.providers import search
    calls, sleeps = [], []
    seq = iter([429, 200])

    class R:
        def __init__(self, c): self.status_code, self.headers = c, {"Retry-After": "7"}
        ok = True
    orig = (search.requests.get, search.time.sleep)
    search.requests.get = lambda *a, **k: calls.append(1) or R(next(seq))
    search.time.sleep = lambda x: sleeps.append(x)
    try:
        key = type("K", (), {"value": "abcd1234", "fp": "…1234"})()
        r = search.TinySearch()._get({"query": "q"}, key)
        assert r.status_code == 200 and len(calls) == 2 and 7 in sleeps   # waited Retry-After, then succeeded
        assert config.TINYSEARCH_MIN_GAP >= 2
    finally:
        search.requests.get, search.time.sleep = orig


def test_shows_wanted_parsed_from_text_and_relax_ladder_starts_there():
    from leadfinder import db
    from leadfinder.people import companies_for_icp, effective_min_shows, requested_min_shows
    from leadfinder.targeting import rule_from_text, shows_wanted
    assert shows_wanted("exhibits at 5+ shows a year") == 5
    assert shows_wanted("exhibit at four or more trade fairs") == 4
    assert shows_wanted("10 years in sales, exhibits at shows") is None
    assert rule_from_text("exhibits at 2+ shows") == "exhibitor_3plus"
    assert rule_from_text("exhibits at a show") == "exhibitor"
    con = _setup()
    con.execute("UPDATE industries SET persona_text='sales heads at firms that exhibit at 5+ shows a year'")
    con.commit()
    ind = db.get_industry(con, "p")
    assert requested_min_shows(ind) == 5 and effective_min_shows(ind) == 5
    for i in range(4):
        con.execute("INSERT INTO shows(industry_id,name,year,start_date) VALUES (?,?,?,?)",
                    (ind["id"], f"S{i}", 2025, f"2025-0{i + 1}-10"))
    c = db.upsert_company(con, "Four Show Co", industry_id=ind["id"])
    for sid in range(1, 5):
        db.add_evidence(con, "company", c, "exhibitor", "confirmed", f"https://x/{sid}", show_id=sid)
    con.commit()
    comps, m = companies_for_icp(con, ind, {"code": "A", "company_rule": "exhibitor_3plus"}, min_count=1)
    assert m == 4 and len(comps) == 1   # 5 -> 4, not 5 -> 2


def test_summary_falls_back_without_claude_and_reports_bottleneck():
    from leadfinder import db, pipeline
    con = _setup()
    ind = db.get_industry(con, "p")
    con.execute("INSERT INTO shows(industry_id,name,year,start_date) VALUES (?,?,?,?)", (ind["id"], "Empty Expo", 2025, "2025-03-01"))
    con.commit()
    orig = pipeline._claude_text
    pipeline._claude_text = lambda *a, **k: None   # Sonnet unreachable
    try:
        d = pipeline.stage_summary(con, ind, {})
    finally:
        pipeline._claude_text = orig
    assert d["by"] == "fallback"
    assert "Empty Expo" in d["text"] and "asked 2+ shows" in d["text"] and "covered 0 of 0" in d["text"]


def test_evidence_step_skips_when_enough_companies_or_no_show_rule():
    from leadfinder import db, pipeline
    con = _setup()
    ind = db.get_industry(con, "p")
    orig = pipeline._claude
    pipeline._claude = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call Claude"))
    try:
        d = pipeline.stage_evidence(con, ind, {"evidence_target": 0})   # 0 companies needed -> already enough
        assert "already exhibit" in d["skipped"]
        doc = json.loads(ind["icp_json"])
        for i in doc["icps"]:
            i["company_rule"] = "visitor"
        con.execute("UPDATE industries SET icp_json=?", (json.dumps(doc),)); con.commit()
        d = pipeline.stage_evidence(con, db.get_industry(con, "p"), {})
        assert "no 'exhibits at N+ shows' rule" in d["skipped"]
    finally:
        pipeline._claude = orig


def test_no_show_count_asked_means_no_show_rule():
    """Regression: a persona that never mentions a number of shows was reported as 'asking for 3+ shows'."""
    from leadfinder import db, pipeline
    from leadfinder.people import company_rule_ok, effective_min_shows, requested_min_shows
    con = _setup()
    doc = json.loads(db.get_industry(con, "p")["icp_json"])
    for i in doc["icps"]:
        i["company_rule"] = "visitor_or_exhibitor"
    con.execute("UPDATE industries SET icp_json=?, options_json=?", (json.dumps(doc), json.dumps({"min_shows": 3})))
    con.commit()
    ind = db.get_industry(con, "p")
    assert requested_min_shows(ind) == 0 and effective_min_shows(ind) == 0   # stale stored 3 is ignored
    assert "shows_rule" not in pipeline.run_facts(con, ind)
    assert "Shows rule" not in pipeline.fallback_summary(pipeline.run_facts(con, ind))
    # 'exhibits at multiple shows' with no number -> 2+, and the rule never degrades to 'any company'
    for i in doc["icps"]:
        i["company_rule"] = "exhibitor_3plus"
    con.execute("UPDATE industries SET icp_json=?", (json.dumps(doc),)); con.commit()
    assert requested_min_shows(db.get_industry(con, "p")) == 2
    assert not company_rule_ok("exhibitor_3plus", {"exhibitor_window_count": 0}, 0)


def test_summary_facts_respect_user_choices_and_reject_invented_numbers():
    from leadfinder import db, pipeline
    con = _setup()
    con.execute("UPDATE industries SET options_json=?", (json.dumps({"emails": 0}),))
    ind = db.get_industry(con, "p")
    pipeline._stage_set(con, ind["id"], "emails", "done", {"skipped": "email search turned off"})
    pipeline._stage_set(con, ind["id"], "verify", "done", {"checked": 0, "requested": 40})
    f = pipeline.run_facts(con, db.get_industry(con, "p"))
    steps = {s["step"]: s for s in f["steps"]}
    assert "by you" in steps["Find work emails"]["detail"]["skipped"] and f["email_search"].startswith("off")
    assert "got none back" in steps["Check LinkedIn profiles"]["detail"]["warning"]
    assert "with a work email" not in pipeline.fallback_summary(f)
    assert pipeline.unsupported_numbers("40 requested, 0 checked", f) == []
    assert pipeline.unsupported_numbers("672 of 677 companies", f) == [672, 677]
    orig = pipeline._claude_text
    pipeline._claude_text = lambda *a, **k: "People found: 9999."
    try:
        assert pipeline.stage_summary(con, ind, {})["by"] == "fallback"
    finally:
        pipeline._claude_text = orig




def test_persona_questions_pause_then_answers_or_best_guess():
    """The persona step may ask clarifying questions: the run pauses (exit 0, nothing downstream runs) until the user
    answers (persona re-runs with the answers) or accepts the best guess (persona done, the run carries on)."""
    from unittest import mock
    from leadfinder import db, pipeline, web
    con = _setup()
    calls = []
    pipeline.RUNNERS.update({k: (lambda k: lambda c, i, o: calls.append(k) or {"n": 1, "leads": 1, "done": 1,
                                                                             "companies": 1})(k)
                             for k in pipeline.RUNNERS})
    seen_opts = []

    def asks(c, i, o):
        seen_opts.append(o)
        raise pipeline.NeedsInput({"questions": [{"q": "Buyers or sellers?", "why": "", "options": []}],
                                   "icps": ["P1: Buyer"]})
    pipeline.RUNNERS["persona"] = asks
    assert pipeline.run("p", {}) == 0 and calls == []
    row = con.execute("SELECT status, detail FROM pipeline_stages WHERE stage='persona'").fetchone()
    assert row["status"] == "questions" and json.loads(row["detail"])["questions"][0]["q"] == "Buyers or sellers?"
    assert pipeline.run("p", {}) == 0 and calls == [] and len(seen_opts) == 1   # Resume doesn't re-ask

    client = web.app.test_client()
    h = {"X-Requested-With": "leadfinder", "Host": "127.0.0.1:8787"}
    with mock.patch.object(web, "start_job", lambda action, p, slug: (action, p)):
        assert client.post("/api/searches/p/clarify", json={"answers": [{"q": "Buyers or sellers?", "a": " "}]},
                           headers=h).status_code == 400
        r = client.post("/api/searches/p/clarify", json={"answers": [{"q": "Buyers or sellers?", "a": "buyers"}],
                                                         "note": "only plant heads"}, headers=h)
        assert r.get_json()["job"] == ["pipeline", {"from_stage": "persona"}]
        opts = json.loads(db.get_industry(con, "p")["options_json"])
        assert opts["persona_rounds"] == 1 and [c["a"] for c in opts["clarifications"]] == ["buyers", "only plant heads"]
        assert "A: buyers" in pipeline.clarifications_block(opts)
        r = client.post("/api/searches/p/clarify", json={"use_guess": True}, headers=h)
        assert r.get_json()["job"] == ["pipeline", {}]
    assert con.execute("SELECT status FROM pipeline_stages WHERE stage='persona'").fetchone()["status"] == "done"
    assert pipeline.run("p", {}) == 0 and calls[0] == "research" and len(seen_opts) == 1


def test_read_questions_sanitises():
    from leadfinder import pipeline
    f = Path(tempfile.mkdtemp()) / "questions.json"
    f.write_text(json.dumps({"questions": ["Which side?", {"q": ""}, 5, {"q": "Size?", "options": ["SME", "", "Large"]},
                                          {"q": "a"}, {"q": "b"}, {"q": "c"}]}))
    qs = pipeline._read_questions(f)
    assert [q["q"] for q in qs] == ["Which side?", "Size?", "a", "b"] and qs[1]["options"] == ["SME", "Large"]
    assert pipeline._read_questions(f.with_name("missing.json")) == []


def test_stage_persona_real_questions_rounds_and_stale_file():
    from unittest import mock
    from leadfinder import config, db, pipeline, web
    con = _setup()
    config.DATA = Path(tempfile.mkdtemp())
    work = config.DATA / "work" / "p"
    work.mkdir(parents=True)
    icp = Path(__file__).resolve().parent.parent.joinpath("tests/fixtures/icp_example.json").read_text()
    con.execute("UPDATE industries SET icp_json=NULL"); con.commit()
    prompts = []

    def fake(write_q):
        def f(prompt, model=None, effort=None):
            prompts.append(prompt)
            c = db.connect(); c.execute("UPDATE industries SET icp_json=?", (icp,)); c.commit()
            if write_q:
                (work / "questions.json").write_text(json.dumps({"questions": [{"q": "Which side?"}]}))
            return 0
        return f
    with mock.patch.object(pipeline, "_claude", fake(True)):
        try:
            pipeline.stage_persona(con, db.get_industry(con, "p"), {})
            raise AssertionError("expected questions")
        except pipeline.NeedsInput as e:
            assert e.detail["questions"][0]["q"] == "Which side?" and e.detail["icps"]
        assert "questions.json" in prompts[-1]
        out = pipeline.stage_persona(con, db.get_industry(con, "p"), {"persona_rounds": 2})
        assert out["icps"] and "Do not ask questions" in prompts[-1]
    (work / "questions.json").write_text(json.dumps({"questions": ["stale?"]}))
    with mock.patch.object(pipeline, "_claude", fake(False)):
        assert pipeline.stage_persona(con, db.get_industry(con, "p"), {})["icps"]   # stale file removed first

    # editing the description clears earlier clarifications; other re-runs are refused while questions wait
    con.execute("UPDATE industries SET options_json=?", (json.dumps({"clarifications": [{"q": "x", "a": "buyers"}],
                                                                     "persona_rounds": 1}),))
    pipeline._stage_set(con, db.get_industry(con, "p")["id"], "persona", "questions", {"questions": [{"q": "x"}]})
    client = web.app.test_client()
    h = {"X-Requested-With": "leadfinder", "Host": "127.0.0.1:8787"}
    with mock.patch.object(web, "start_job", lambda action, p, slug: (action, p)):
        assert client.post("/api/searches/p/run", json={}, headers=h).status_code == 409
        r = client.post("/api/searches/p/run", json={"persona": "sellers who exhibit at shows"}, headers=h)
        assert r.status_code == 200
    opts = json.loads(db.get_industry(con, "p")["options_json"])
    assert opts["clarifications"] == [] and opts["persona_rounds"] == 0




def test_rename_and_delete_search_keeps_data_shared_with_other_searches():
    from leadfinder import config, db, web
    con = _setup()
    con.execute(web.JOB_SCHEMA)
    config.DATA = web.config.DATA = Path(tempfile.mkdtemp())
    web.JOBS_DIR = config.DATA / "jobs"
    web.JOBS_DIR.mkdir(parents=True)
    con.execute("INSERT INTO industries(slug,name,geo,persona_text) VALUES ('q','Q','IN','other buyers')")
    pid_, qid = [con.execute("SELECT id FROM industries WHERE slug=?", (s,)).fetchone()[0] for s in ("p", "q")]
    only = db.upsert_company(con, "Only P Ltd", industry_id=pid_)
    both = db.upsert_company(con, "Shared Ltd", industry_id=pid_)
    db.upsert_company(con, "Shared Ltd", industry_id=qid)
    people = {}
    for c, nm in ((only, "Asha Only"), (both, "Ravi Shared")):
        con.execute("INSERT INTO people(company_id,name,norm_name,icp) VALUES (?,?,?, 'A')", (c, nm, nm.lower()))
        people[nm] = con.execute("SELECT id FROM people WHERE name=?", (nm,)).fetchone()[0]
        con.execute("INSERT INTO tracker(person_id,status) VALUES (?,'new')", (people[nm],))
        db.add_evidence(con, "person", people[nm], "linkedin_profile", "likely", "http://x/" + nm)
        db.set_group(con, people[nm], pid_, "A")
    db.set_group(con, people["Ravi Shared"], qid, "B")
    con.execute("INSERT INTO shows(industry_id,name,year) VALUES (?, 'ShowP', 2026)", (pid_,))
    show = con.execute("SELECT id FROM shows").fetchone()[0]
    db.add_evidence(con, "company", only, "exhibitor", "confirmed", "http://s", show_id=show)
    con.execute("INSERT INTO search_queue(purpose,query,params,context) VALUES ('people','q','{}',?)",
                (json.dumps({"company_id": only, "industry_id": pid_}),))
    log = web.JOBS_DIR / "job-1.log"
    log.write_text("x")
    con.execute("INSERT INTO jobs(kind,label,industry,status,log_path) VALUES ('claude','Find people','p','running',?)",
                (str(log),))
    (config.DATA / "work" / "p").mkdir(parents=True)
    con.commit()

    client = web.app.test_client()
    h = {"X-Requested-With": "leadfinder", "Host": "127.0.0.1:8787"}
    assert client.post("/api/searches/p/rename", json={"name": "  "}, headers=h).status_code == 400
    r = client.post("/api/searches/p/rename", json={"name": "  New   name "}, headers=h)
    assert r.get_json() == {"slug": "p", "name": "New name"}
    assert db.get_industry(con, "p")["name"] == "New name" and db.get_industry(con, "p")["slug"] == "p"

    assert client.delete("/api/searches/p", headers=h).status_code == 409   # a run is still going
    con.execute("UPDATE jobs SET status='done'")
    con.commit()
    assert client.delete("/api/searches/p").status_code == 403              # CSRF guard applies
    r = client.delete("/api/searches/p", headers=h)
    assert r.status_code == 200 and (r.get_json()["people"], r.get_json()["companies"]) == (1, 1)

    n = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    assert n("SELECT COUNT(*) FROM industries WHERE slug='p'") == 0 and n("SELECT COUNT(*) FROM shows") == 0
    assert n("SELECT COUNT(*) FROM pipeline_stages") == 0 and n("SELECT COUNT(*) FROM jobs") == 0
    assert n("SELECT COUNT(*) FROM search_queue") == 0 and n("SELECT COUNT(*) FROM evidence WHERE entity='company'") == 0
    assert [r[0] for r in con.execute("SELECT name FROM people")] == ["Ravi Shared"]      # shared company's person kept
    assert [r[0] for r in con.execute("SELECT name FROM companies")] == ["Shared Ltd"]
    assert n("SELECT COUNT(*) FROM tracker") == 1 and n("SELECT COUNT(*) FROM evidence") == 1
    assert n("SELECT COUNT(*) FROM company_industries") == 1
    assert [tuple(r) for r in con.execute("SELECT industry_id, icp FROM person_groups")] == [(qid, "B")]
    assert not log.exists() and not (config.DATA / "work" / "p").exists()


def test_exhibitor_help_pause_continue_without_rerunning_evidence():
    """Evidence pauses (status 'help') when core shows' lists couldn't be read. Resume doesn't re-run it; Continue
    marks it done WITH its result (refreshed) and the run carries on at the next step."""
    from unittest import mock
    from leadfinder import pipeline, web
    con = _setup()
    con.execute("INSERT INTO shows(id, industry_id, name, series, year, url) VALUES (7, 1, 'ADIPEC', 'ADIPEC', 2026, "
                "'https://adipec.com')")
    con.commit()
    calls = []
    pipeline.RUNNERS.update({k: (lambda k: lambda c, i, o: calls.append(k) or {"n": 1, "leads": 1, "done": 1,
                                                                             "companies": 1})(k)
                             for k in pipeline.RUNNERS})

    def evidence(c, i, o):
        calls.append("evidence")
        raise pipeline.NeedsInput({"help": [{"id": 7, "name": "ADIPEC"}],
                                   "result": {"min_shows_wanted": 2, "companies_at_n_before": 3,
                                              "catalogs": {"ADIPEC": "not read: search form"},
                                              "thin_shows_before": ["ADIPEC"]}})
    pipeline.RUNNERS["evidence"] = evidence
    assert pipeline.run("p", {}) == 0 and calls == ["persona", "research", "evidence"]
    assert con.execute("SELECT status FROM pipeline_stages WHERE stage='evidence'").fetchone()["status"] == "help"
    calls.clear()
    assert pipeline.run("p", {}) == 0 and calls == []          # Resume waits, never redoes the paid step

    client = web.app.test_client()
    h = {"X-Requested-With": "leadfinder", "Host": "127.0.0.1:8787"}
    with mock.patch.object(web, "start_job", lambda action, p, slug: (action, p)):
        assert client.post("/api/searches/p/run", json={}, headers=h).status_code == 409
        r = client.get("/api/searches/p/help", headers=h).get_json()
        assert r["paused"] and [s["name"] for s in r["shows"]] == ["ADIPEC"]
        assert "search form" in r["shows"][0]["reason"]
        r = client.post("/api/searches/p/help/continue", json={}, headers=h)
        assert r.get_json()["job"] == ["pipeline", {}]
    row = con.execute("SELECT status, detail FROM pipeline_stages WHERE stage='evidence'").fetchone()
    d = json.loads(row["detail"])
    assert row["status"] == "done" and d["companies_at_n_before"] == 3 and "companies_at_n_after" in d
    assert client.get("/api/searches/p/help", headers=h).get_json()["shows"] == []   # skipped: never asked again
    assert pipeline.run("p", {}) == 0 and calls[0] == "attendees" and "evidence" not in calls


def test_showhelp_parse_and_import_need_a_source_url():
    from leadfinder import db, showhelp
    con = _setup()
    ind = db.get_industry(con, "p")
    assert [r["name"] for r in showhelp.parse_rows('```json\n[{"name": "Acme"}, {"name": "Beta", "booth": "A1"}]\n```')] \
        == ["Acme", "Beta"]
    assert [r["name"] for r in showhelp.parse_rows('{"exhibitors": ["Acme", "Beta"]}')] == ["Acme", "Beta"]
    assert [r["name"] for r in showhelp.parse_rows("Company Name,Country\nAcme,India\nBeta,UAE")] == ["Acme", "Beta"]
    assert [r["name"] for r in showhelp.parse_rows("1. Acme Ltd\n- Beta Pvt Ltd\n\n")] == ["Acme Ltd", "Beta Pvt Ltd"]
    con.execute("INSERT INTO shows(id, industry_id, name, year) VALUES (8, 1, 'Bare Show', 2026)")
    con.commit()
    show = dict(con.execute("SELECT * FROM shows WHERE id=8").fetchone())
    rows = showhelp.parse_rows("Acme Ltd\nBeta Pvt Ltd")
    assert showhelp.import_rows(con, ind, show, rows)["added"] == 0         # no URL anywhere: nothing silently "added"
    r = showhelp.import_rows(con, ind, show, rows, "https://example.com/exhibitors")
    assert r["added"] == 2 and r["exhibitors_now"] == 2
    assert showhelp.decisions(con, ind)[showhelp.show_key(show)]["status"] == "done"
    # rows with their own profile URL need no source URL
    con.execute("INSERT INTO shows(id, industry_id, name, year) VALUES (9, 1, 'Other Show', 2026)")
    show9 = dict(con.execute("SELECT * FROM shows WHERE id=9").fetchone())
    assert showhelp.import_rows(con, ind, show9, [{"name": "Gamma", "profile_url": "https://x.io/g"}])["added"] == 0
    show9["url"] = "https://other.example"
    assert showhelp.import_rows(con, ind, show9, [{"name": "Gamma", "profile_url": "https://x.io/g"}])["added"] == 1


def test_showhelp_needing_help_core_only_and_prompt():
    from leadfinder import config, db, showhelp
    con = _setup()
    ind = db.get_industry(con, "p")
    con.execute("INSERT INTO shows(id, industry_id, name, series, year, url, start_date) VALUES "
                "(10, 1, 'PETROTECH 2026', 'PETROTECH', 2026, 'https://petrotech.example', '2026-11-23'), "
                "(11, 1, 'Bauma 2026', 'Bauma', 2026, NULL, '2026-09-15')")
    con.commit()
    assert [s["name"] for s in showhelp.needing_help(con, ind, core_names=["PETROTECH 2026"])] == ["PETROTECH 2026"]
    p = showhelp.agent_prompt(con, ind, dict(con.execute("SELECT * FROM shows WHERE id=10").fetchone()))
    assert "./lf import-exhibitors --industry p --show 10" in p and str(config.ROOT) in p
    assert "Do not try to get around it" in p and "mapyourshow.com" in p and "```json" in p
    showhelp.decide(con, ind, {"id": 10, "series": "PETROTECH", "year": 2026}, "skipped")
    # same event under a new id (research re-ran) is still settled
    con.execute("INSERT INTO shows(id, industry_id, name, series, year) VALUES (12, 1, 'Petrotech', 'PETROTECH 2026', 2026)")
    assert showhelp.needing_help(con, ind, core_names=["PETROTECH 2026", "Petrotech"]) == []

def test_help_routes_paste_url_skip_prompt():
    from unittest import mock
    from leadfinder import web
    con = _setup()
    con.execute("INSERT INTO shows(id, industry_id, name, series, year, start_date) VALUES "
                "(20, 1, 'PETROTECH 2026', 'PETROTECH', 2026, '2026-11-23'), (21, 1, 'ADIPEC', 'ADIPEC', 2026, '2026-11-02')")
    con.commit()
    client = web.app.test_client()
    h = {"X-Requested-With": "leadfinder", "Host": "127.0.0.1:8787"}
    with mock.patch.object(web, "start_job", lambda action, p, slug: (action, p)):
        assert client.post("/api/searches/p/help/20/url", json={"url": "ftp://x"}, headers=h).status_code == 400
        r = client.post("/api/searches/p/help/20/url", json={"url": "https://expofp.com/petrotech"}, headers=h)
        assert r.get_json()["job"] == ["read_list", {"show": 20, "url": "https://expofp.com/petrotech"}]
        assert client.post("/api/searches/p/help/99/url", json={"url": "https://a.b"}, headers=h).status_code == 404
    # no source URL and the show has none: nothing imported (and no fake success)
    r = client.post("/api/searches/p/help/20/paste", json={"text": "Acme Ltd\nBeta Ltd"}, headers=h).get_json()
    assert r["added"] == 0
    assert client.post("/api/searches/p/help/20/paste", json={"text": "  "}, headers=h).status_code == 400
    r = client.post("/api/searches/p/help/20/paste", json={"text": '[{"name": "Acme Ltd"}, {"name": "Beta Ltd"}]',
                                                            "source_url": "https://petrotech.example/list"}, headers=h)
    assert r.get_json()["added"] == 2
    assert "./lf import-exhibitors --industry p --show 21" in \
        client.get("/api/searches/p/help/21/prompt", headers=h).get_json()["prompt"]
    assert [x["name"] for x in client.get("/api/searches/p/help", headers=h).get_json()["shows"]] == ["ADIPEC"]
    assert client.post("/api/searches/p/help/21/skip", json={}, headers=h).status_code == 200
    r = client.get("/api/searches/p/help", headers=h).get_json()
    assert r["shows"] == [] and not r["paused"] and {x["status"] for x in r["settled"]} == {"done", "skipped"}


def test_profile_reader_free_plan_runs_of_10_and_error_rows_reach_summary():
    """Real Apify behaviour (2026-11-24): on a FREE plan the profile reader takes at most 10 profiles per run; asked for
    25 it 'succeeds' with one row {"error": "Free users are limited to 10 items per run..."}. That row must never be
    read as 'no profiles found': the batch is split into runs of 10, and any other error row reaches verify + summary."""
    import os
    from unittest import mock
    from leadfinder import db, enrich, pipeline
    from leadfinder.providers import events as ev
    from leadfinder.providers import linkedin as lip
    con = _setup()
    saved = {k: v for k, v in os.environ.items() if k.startswith(("APIFY", "BRIGHTDATA"))}
    for k in saved:
        del os.environ[k]
    os.environ["APIFY_TOKEN"] = "tok-one-AAAA"
    lip._per_run_cap.clear()
    runs = []
    urls = [f"https://www.linkedin.com/in/p{i}" for i in range(25)]

    def actor(part, key):
        runs.append(len(part))
        if len(part) > 10:
            return [{"error": "Free users are limited to 10 items per run. Please upgrade to a paid plan."}]
        return [{"publicIdentifier": u.rsplit("/", 1)[1], "linkedinUrl": u, "currentPosition": [{"companyName": "Acme"}]}
                for u in part]
    try:
        # plan looks paid: the first run asks for all 25, the error row teaches the cap of 10
        with mock.patch.object(lip, "_profile_rows", actor), mock.patch.object(ev, "paid_plan", lambda: True):
            got = lip.profiles_apify(urls, con=con)          # treated as paid: tries 25, learns 10 from the error row
        assert len(got) == 25 and runs == [25, 10, 10, 5], runs
        runs.clear()
        with mock.patch.object(lip, "_profile_rows", actor):
            assert len(lip.profiles_apify(urls, con=con)) == 25 and runs == [10, 10, 5]   # cap remembered per key
        lip._per_run_cap.clear()
        runs.clear()
        with mock.patch.object(lip, "_profile_rows", actor), mock.patch.object(ev, "paid_plan", lambda: False):
            assert len(lip.profiles_apify(urls, con=con)) == 25 and runs == [10, 10, 5]   # free plan known up front
        # any other error row is raised with its message, and verify / the summary say so
        bad = lambda part, key: [{"error": "Monthly usage hard limit exceeded"}]  # noqa: E731
        ind = db.get_industry(con, "p")
        with mock.patch.object(lip, "_profile_rows", bad), \
                mock.patch.object(enrich, "select_leads", lambda *a, **k: [
                    {"id": 1, "linkedin_url": urls[0], "company": "Acme"}]):
            r = enrich.verify_profiles(con, ind, top=5)
        assert r["checked"] == 0 and r["requested"] == 1 and "Monthly usage hard limit" in r["error"]
        warn = pipeline._empty_warning("verify", r)
        assert "Monthly usage hard limit" in warn and "none came back" not in warn
        pipeline._stage_set(con, ind["id"], "verify", "warn", {**r, "warning": warn})
        f = pipeline.run_facts(con, ind)
        v = next(s for s in f["steps"] if s["step"] == "Check LinkedIn profiles")
        assert "Monthly usage hard limit" in v["detail"]["warning"]
        assert "Monthly usage hard limit" in pipeline.fallback_summary(f)
    finally:
        del os.environ["APIFY_TOKEN"]
        os.environ.update(saved)
        lip._per_run_cap.clear()


def test_upload_csv_excel_pdf_and_summary_lists_waiting_shows():
    import io
    from unittest import mock
    from openpyxl import Workbook
    from leadfinder import config, db, pipeline, showhelp, web
    con = _setup()
    config.DATA = config.DB_PATH.parent
    con.execute("INSERT INTO shows(id, industry_id, name, series, year, url) VALUES "
                "(30, 1, 'PETROTECH 2026', 'PETROTECH', 2026, 'https://petrotech.example'), "
                "(31, 1, 'ADIPEC', 'ADIPEC', 2026, NULL)")
    con.commit()
    ind = db.get_industry(con, "p")
    f = pipeline.run_facts(con, ind)
    assert set(f["exhibitor_lists_waiting_for_the_user"]["shows"]) == {"PETROTECH 2026", "ADIPEC"}
    assert "PETROTECH 2026" in pipeline.fallback_summary(f)
    client = web.app.test_client()
    h = {"X-Requested-With": "leadfinder", "Host": "127.0.0.1:8787"}
    up = lambda sid, name, data, **form: client.post(f"/api/searches/p/help/{sid}/file", headers=h,  # noqa: E731
                                                     data={"file": (io.BytesIO(data), name), **form},
                                                     content_type="multipart/form-data")
    r = up(30, "list.csv", b"Company Name,Country,Stand\nAcme Pumps,India,H1\nBeta Valves,UAE,H2\n")
    assert r.status_code == 200 and r.get_json()["added"] == 2, r.get_data()
    wb = Workbook(); ws = wb.active
    ws.append(["Exhibitors list 2026"]); ws.append(["S.No", "Exhibitor Name", "Country"])
    ws.append([1, "Gamma Seals", "India"]); ws.append([2, "Delta Pipes", "India"])
    buf = io.BytesIO(); wb.save(buf)
    r = up(31, "agent.xlsx", buf.getvalue())
    assert r.status_code == 400 and b"source URL" in r.get_data()   # no URL anywhere: refused, not "added"
    r = up(31, "agent.xlsx", buf.getvalue(), source_url="https://adipec.example/exhibitors")
    assert r.status_code == 200 and r.get_json()["added"] == 2
    names = {x["name"] for x in con.execute("SELECT c.name FROM companies c JOIN evidence e ON e.entity_id=c.id "
                                            "AND e.entity='company' AND e.show_id=31")}
    assert names == {"Gamma Seals", "Delta Pipes"}
    assert up(30, "old.xls", b"x").status_code == 400 and up(30, "x.docx", b"x").status_code == 400
    with mock.patch.object(web, "start_job", lambda action, p, slug: (action, p)):
        r = up(30, "guide.pdf", b"%PDF-1.4")
        action, p = r.get_json()["job"]
        assert action == "read_file" and p["show"] == 30 and p["file"].endswith("guide.pdf")
    assert pipeline.run_facts(con, ind).get("exhibitor_lists_waiting_for_the_user") is None


def test_summary_facts_are_pre_added_and_unambiguous():
    from leadfinder import db, pipeline
    con = _setup()
    ind = db.get_industry(con, "p")
    pipeline._stage_set(con, ind["id"], "linkedin", "done", {"P1": {"people_added_or_matched": 10,
                        "paid_for_rest": {"people_added_or_matched": 8, "aiark_for_unresolved": {"people_added_or_matched": 1}}}})
    pipeline._stage_set(con, ind["id"], "web", "done", {"done": 5, "companies_searched": 30, "companies_total": 677})
    pipeline._stage_set(con, ind["id"], "verify", "done", {"checked": 25, "flagged": 16, "requested": 25})
    f = pipeline.run_facts(con, ind)
    st = {x["step"]: x["detail"] for x in f["steps"]}
    assert st["Search LinkedIn"]["people_found_per_group"] == {"P1": 19}
    assert "searched 30 of the 677 companies" in st["Search the web for more people"]["coverage"]
    assert "nothing to retry" in st["Check LinkedIn profiles"]["result"]
    assert "Find work emails" in f["what_run_again_would_do"]     # not run yet -> Run again would run it
    assert pipeline.unsupported_numbers("P1: 19 people; 25 of 25 checked, 16 flagged; 30 of 677", f) == []
    assert "P1: 19 people" in pipeline.fallback_summary(f) and "16 flagged" in pipeline.fallback_summary(f)



if __name__ == "__main__":
    for k, v in dict(globals()).items():
        if k.startswith("test_"):
            v()
            print("ok", k)
