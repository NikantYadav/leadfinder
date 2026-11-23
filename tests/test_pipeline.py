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


if __name__ == "__main__":
    for k, v in dict(globals()).items():
        if k.startswith("test_"):
            v()
            print("ok", k)
