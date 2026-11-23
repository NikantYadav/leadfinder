"""Apify trade-show sources (providers/events.py + showdata.py), against real output saved 2026-11-23."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"


def _setup():
    from leadfinder import config, db
    tmp = Path(tempfile.mkdtemp())
    config.DB_PATH, config.OUTPUT = tmp / "t.db", tmp
    con = db.connect()
    con.execute("INSERT INTO industries(slug,name,geo,persona_text,icp_json,icp_approved) VALUES ('p','P','IN','x',?,1)",
                ((FIX / "icp_example.json").read_text(),))
    con.execute("INSERT INTO shows(industry_id,name,year,start_date,exhibitor_list_url) VALUES "
                "(1,'interpack 2026',2026,'2026-05-07','https://www.interpack.com/vis/v1/en/directory/a')")
    con.commit()
    return con, db.get_industry(con, "p")


def test_route():
    from leadfinder.providers import events as ev

    def r(show, capped=True):
        return [(spec["actor"].split("~")[-1], v) for spec, v in ev.route(show, allow_capped=capped)]
    md = "messe-duesseldorf-exhibitors-scraper"
    # fair name wins even when the only list URL is a PDF
    assert r({"name": "interpack 2026", "exhibitor_list_url": "https://x.com/Aussteller.pdf"}) == [(md, "interpack")]
    assert r({"name": "EuroBLECH 2026"}) == [("rx-exhibitors-scraper", "euroblech")]
    assert r({"name": "Hannover Messe 2027"}) == [("deutsche-messe-exhibitors-scraper", "hannover-messe")]
    assert r({"name": "A+A 2025"}) == [(md, "aplusa")]
    assert r({"name": "K 2025"}) == [(md, "k")]
    assert r({"name": "Kitchen Expo"}) == [] and r({"name": "IMTEX 2025", "url": "https://www.imtex.in"}) == []
    # directory URLs: the platform reader first, the free-failing probe after it
    mys = "https://ift27.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm"
    assert r({"name": "IFT 2027", "exhibitor_list_url": mys}) == [
        ("mapyourshow-exhibitor-scraper", mys), ("trade-show-exhibitor-list-scraper", mys)]
    # unknown host: probe only; a Messe-looking host adds skython unless the plan is free
    assert r({"name": "PackPlus", "exhibitor_list_url": "https://www.packplus.in/x"}) == [
        ("trade-show-exhibitor-list-scraper", "https://www.packplus.in/x")]
    assert ("exhibitor-list-scraper", "https://www.messe-x.de/aussteller") in r(
        {"name": "X", "exhibitor_list_url": "https://www.messe-x.de/aussteller"})
    assert ("exhibitor-list-scraper", "https://www.messe-x.de/aussteller") not in r(
        {"name": "X", "exhibitor_list_url": "https://www.messe-x.de/aussteller"}, capped=False)


def test_parse_new_catalog_rows():
    """Real rows: rel8ble Messe Duesseldorf (interpack), rel8ble RX (EuroBLECH), scrapersdelight Map Your Show."""
    from leadfinder.providers import events as ev
    md = [ev.parse_exhibitor(i) for i in json.loads((FIX / "apify_rel8ble_md.json").read_text())]
    assert all(md) and all(x["profile_url"] and "interpack.com" in x["profile_url"] for x in md)
    assert any(x["website"] and x["country"] for x in md)
    rx = [ev.parse_exhibitor(i) for i in json.loads((FIX / "apify_rel8ble_rx.json").read_text())]
    assert rx[0]["name"] == "247TailorSteel Deutschland GmbH" and rx[0]["booth"] == "17-E22"
    assert rx[0]["country"] == "Germany" and "euroblech.com" in rx[0]["profile_url"]
    sd = [ev.parse_exhibitor(i) for i in json.loads((FIX / "apify_sd_mys.json").read_text())]
    assert sd[0]["website"] == "https://tres-a.com" and sd[0]["country"] == "Spain"


def test_parse_skython_rows():
    from leadfinder.providers import events as ev
    rows = [ev.parse_exhibitor(i) for i in json.loads((FIX / "apify_skython_interpack.json").read_text())]
    r = rows[0]
    assert r["name"] == "1AB Wick-Machinery GmbH" and r["website"] == "https://www.wick-machinery.com"
    assert r["country"] == "Austria" and r["booth"] == "Hall 16 / A57"
    assert r["profile_url"].startswith("https://www.interpack.com/vis/v1/en/exhprofiles/")
    # failure rows (tidytools) and rows without a name are dropped
    assert ev.parse_exhibitor({"success": False, "error": "no exhibitors found"}) is None
    assert ev.parse_exhibitor({"website": "x.com"}) is None


def test_ingest_merges_by_domain_and_records_show():
    from leadfinder import db, showdata
    con, ind = _setup()
    # the same firm already known from another show under a different spelling, with its domain
    db.upsert_company(con, "Wick Machinery", "wick-machinery.com", industry_id=ind["id"])
    n = showdata.ingest_exhibitors(con, ind, 1, json.loads((FIX / "apify_skython_interpack.json").read_text()), "u")
    assert n == 8
    cid = con.execute("SELECT id FROM companies WHERE domain='wick-machinery.com'").fetchone()["id"]
    assert con.execute("SELECT COUNT(*) FROM companies WHERE domain='wick-machinery.com'").fetchone()[0] == 1
    e = con.execute("SELECT * FROM evidence WHERE entity='company' AND entity_id=?", (cid,)).fetchone()
    assert (e["kind"], e["confidence"], e["show_id"]) == ("exhibitor", "confirmed", 1)
    assert "Hall 16" in e["snippet"] and "exhprofiles" in e["url"]
    new = con.execute("SELECT domain, domain_source FROM companies WHERE name='A. Hatzopoulos S.A.'").fetchone()
    assert (new["domain"], new["domain_source"]) == ("hatzopoulos.gr", "catalog")


def test_parse_tidytools_mys_rows():
    from leadfinder.providers import events as ev
    rows = [ev.parse_exhibitor(i) for i in json.loads((FIX / "apify_tidytools_mys.json").read_text())]
    assert all(rows) and rows[0]["name"] == "3A BIOTECH SL"
    assert "mapyourshow.com/8_0/exhibitor/exhibitor-details.cfm" in rows[0]["profile_url"]


def test_pipeline_skips_free_capped_reader():
    """On the FREE Apify plan skython reads only 20 rows, so the pipeline (allow_capped=False) never starts it;
    a known fair name goes to its uncapped reader instead."""
    from leadfinder import showdata
    from leadfinder.providers import events as ev
    con, ind = _setup()
    show = dict(con.execute("SELECT * FROM shows WHERE id=1").fetchone())
    calls = []
    orig_run, orig_paid = ev.run_actor, ev.paid_plan
    ev.run_actor = lambda actor, *a, **k: calls.append(actor) or ([], {"status": "SUCCEEDED"}, None)
    ev.paid_plan = lambda: False
    try:
        unknown = {**show, "name": "Some Messe", "exhibitor_list_url": "https://www.messe-x.de/aussteller"}
        showdata.harvest_exhibitors(con, ind, unknown, 2.0, log=lambda m: None, allow_capped=False)
        assert ev.SKYTHON not in calls and calls == [ev.TIDY]
        calls.clear()
        showdata.harvest_exhibitors(con, ind, show, 2.0, log=lambda m: None, allow_capped=False)  # interpack 2026
        assert calls[0] == "rel8ble~messe-duesseldorf-exhibitors-scraper" and ev.SKYTHON not in calls
    finally:
        ev.run_actor, ev.paid_plan = orig_run, orig_paid


def test_parse_10times():
    from leadfinder.providers import events as ev
    out = [ev.parse_event(i) for i in json.loads((FIX / "apify_10times.json").read_text())]
    e = next(x for x in out if x and x["name"].startswith("Engiexpo"))
    assert e["start_date"] == "2026-11-21" and e["country"] == "India" and e["year"] == 2026
    assert "Packaging" in e["categories"] and e["source_url"].startswith("https://10times.com/")


def test_harvest_budget_and_fallback(monkeypatch=None):
    """Unknown host: tidytools (free failure) first, then skython; the skython cap never exceeds the budget."""
    from leadfinder import showdata
    from leadfinder.providers import events as ev
    con, ind = _setup()
    show = dict(con.execute("SELECT * FROM shows WHERE id=1").fetchone())
    show["name"], show["exhibitor_list_url"] = "Some Fair", "https://messe-example.com/exhibitors"
    calls = []
    sky = json.loads((FIX / "apify_skython_interpack.json").read_text())

    def fake_run(actor, body, cap, deadline=None, log=None, ref=None, on_start=None):
        calls.append((actor, cap, body))
        if on_start:
            on_start({"actor": actor, "run_id": "r", "key": "k"})
        if actor == ev.TIDY:
            return [{"success": False, "error": "no_items"}], {"status": "SUCCEEDED", "usageTotalUsd": 0}, None
        return sky, {"status": "SUCCEEDED", "usageTotalUsd": 0.033}, {"actor": actor}
    orig = ev.run_actor
    ev.run_actor = fake_run
    try:
        res = showdata.harvest_exhibitors(con, ind, show, 0.5, log=lambda m: None)
    finally:
        ev.run_actor = orig
    assert [c[0] for c in calls] == [ev.TIDY, ev.SKYTHON]  # free probe first, then the capped reader (CLI allows it)
    assert all(c[1] <= 0.5 + 1e-9 for c in calls)
    assert res["added"] == 8 and res["actor"] == ev.SKYTHON and res["cost"] == 0.033
    # too small a budget for skython's 20-row minimum: not started at all
    calls.clear()
    ev.run_actor = fake_run
    try:
        res = showdata.harvest_exhibitors(con, ind, show, 0.05, log=lambda m: None)
    finally:
        ev.run_actor = orig
    assert ev.SKYTHON not in [c[0] for c in calls] and res["added"] == 0


def test_recent_shows_and_attendee_stage_skip():
    from datetime import date
    from leadfinder import pipeline
    con, ind = _setup()  # interpack 2026-05-07
    con.execute("INSERT INTO shows(industry_id,name,year,start_date,end_date) VALUES "
                "(1,'EuroBLECH 2026',2026,'2026-10-20','2026-10-23'), (1,'Next Fair',2027,'2026-12-20','2026-12-22'),"
                "(1,'Far Fair',2027,'2027-06-01','2027-06-03')")
    con.commit()
    got = [s["name"] for s in pipeline.recent_shows(con, ind, today=date(2026, 11, 24))]
    assert got == ["Next Fair", "EuroBLECH 2026"]  # closest first; May (too old) and June 2027 (too far) left out
    # no recent show: skipped before any network call
    assert "skipped" in pipeline.stage_attendees(con, {**dict(ind), "id": 999}, {})


def test_parse_people_real_euroblech():
    from leadfinder.providers import events as ev
    comps, people = ev.parse_people(json.loads((FIX / "apify_sherwood_euroblech.json").read_text()))
    assert comps == [] and len(people) == 4 and not any(p["staff"] for p in people)
    marek = next(p for p in people if p["name"] == "Marek Czarnecki")
    assert marek["url"].startswith("https://www.linkedin.com/posts/") and 0.6 < marek["score"] < 0.7
    assert ev._person({"personName": "A B", "headline": "Head of Purchasing at Bharat Forge Ltd | Pune",
                       "evidence": [{"url": "u"}]})["company"] == "Bharat Forge Ltd"


def test_ingest_people_visitor_evidence():
    from leadfinder import showdata
    con, ind = _setup()
    items = json.loads((FIX / "apify_sherwood_euroblech.json").read_text())
    out = showdata.ingest_people(con, ind, 1, items)
    assert out["people"] == 4 and out["visitors"] == 4
    kinds = {r["kind"] for r in con.execute("SELECT kind FROM evidence WHERE entity='person'")}
    assert kinds == {"visitor_post"}
    assert con.execute("SELECT COUNT(*) FROM evidence WHERE entity='person' AND confidence='confirmed'").fetchone()[0] == 0


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
            print("ok", k)
