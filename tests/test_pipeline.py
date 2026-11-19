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
    assert calls == ["web"]                             # re-run only retries what didn't finish
    calls.clear()
    pipeline.run("p", {}, from_stage="verify")
    assert calls == ["verify", "emails", "export"]


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


if __name__ == "__main__":
    for k, v in dict(globals()).items():
        if k.startswith("test_"):
            v()
            print("ok", k)
