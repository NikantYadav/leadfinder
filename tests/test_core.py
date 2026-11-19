"""Run: ./env/bin/python -m pytest -q tests   (or ./env/bin/python tests/test_core.py)"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leadfinder.normalize import norm_company, norm_domain, same_company  # noqa: E402
from leadfinder.people import parse_post, parse_profile, title_matches  # noqa: E402
from leadfinder.scoring import best_window_count  # noqa: E402


def test_company_names_collapse():
    variants = ["Bharat Forge Ltd", "BHARAT FORGE LIMITED", "Bharat Forge Pvt. Ltd.", "M/s Bharat Forge Ltd.",
                "Bharat Forge (India) Pvt Ltd", "bharat-forge limited"]
    assert {norm_company(v) for v in variants} == {"bharat forge"}
    assert norm_company("Larsen & Toubro Limited") == norm_company("Larsen and Toubro Ltd")
    assert norm_company("DMG MORI India Pvt Ltd") == "dmg mori"
    # suffix words are only stripped at the end
    assert norm_company("Ltd Engineering Works") == "ltd engineering works"
    assert norm_company("Company") == "company"  # never strip to empty


def test_same_company_is_conservative():
    assert same_company("Bharat Forge Ltd", "BHARAT FORGE LIMITED")
    assert same_company("Bharat Forge", "Bharat Forge Kalyani Ltd")
    assert not same_company("Tata", "Tata Motors")          # single-word prefix too loose
    assert not same_company("KSB India", "Bharat Forge")


def test_search_name():
    from leadfinder.normalize import search_name
    assert search_name("Ace Micromatic Group") == "Ace Micromatic"
    assert search_name("Bharat Electronics Ltd (BEL)") == "Bharat Electronics"
    assert search_name("S & T Engineers Pvt. Ltd") == "S & T Engineers"
    assert search_name("LMW Ltd") == "LMW"
    assert search_name("Batliboi Ltd | Promau S.R.L") == "Batliboi"
    assert search_name("Group") == "Group"
    assert search_name("Axles India") == "Axles India"
    assert search_name("Airbus India") == "Airbus"


def test_domains():
    assert norm_domain("https://www.bharatforge.com/about") == "bharatforge.com"
    assert norm_domain("bharatforge.com") == "bharatforge.com"
    assert norm_domain("https://in.linkedin.com/company/x") == ""


def _shows(*dates):
    return [{"start_date": d, "year": int(d[:4])} for d in dates]


def test_window_counts_distinct_shows_in_12_months():
    assert best_window_count(_shows("2025-01-23", "2025-06-10", "2025-09-22")) == 3
    # spread across two years: best 12-month window holds only 2
    assert best_window_count(_shows("2024-01-10", "2024-12-20", "2025-12-30")) == 2
    # exactly 365 days apart -> not in the same window
    assert best_window_count(_shows("2025-01-01", "2026-01-01")) == 1
    assert best_window_count([]) == 0
    # missing dates fall back to mid-year of the edition
    assert best_window_count([{"start_date": None, "year": 2025}, {"start_date": "2025-03-01", "year": 2025}]) == 2


def test_parse_google_style_profile():
    r = {"title": "mohit saxena - Asst Manager procurement at Bharat Forge ...",
         "url": "https://in.linkedin.com/in/mohit-saxena-74692025",
         "snippet": "Noida, Uttar Pradesh, India · Asst Manager procurement · Bharat Forge Ltd"}
    p = parse_profile(r)
    assert p["name"] == "Mohit Saxena"
    assert p["title"] == "Asst Manager procurement"
    assert p["company"] == "Bharat Forge Ltd"
    assert p["location"].startswith("Noida")
    assert p["linkedin_url"].endswith("mohit-saxena-74692025")


def test_parse_profile_title_without_at():
    r = {"title": "Deepak Kajale - Bharat Forge Ltd | LinkedIn",
         "url": "https://www.google.com/goto?x=1",
         "snippet": "Pune, Maharashtra, India · Senior Manager Tool Planning and Procurement · Bharat Forge Ltd"}
    p = parse_profile(r)
    assert p["name"] == "Deepak Kajale"
    assert p["title"] == "Senior Manager Tool Planning and Procurement"
    assert p["company"] == "Bharat Forge Ltd"
    assert p["linkedin_url"] is None


def test_parse_serper_style_profile():
    r = {"title": "Baburao Solanki - Purchase Manager at Bharat Forge Ltd", "url": "https://in.linkedin.com/in/baburao-s",
         "snippet": "Purchase Manager at Bharat Forge Ltd · Experience: Bharat Forge Ltd · Location: Pune · 1 connection"}
    p = parse_profile(r)
    assert (p["name"], p["title"], p["company"], p["location"]) == \
        ("Baburao Solanki", "Purchase Manager", "Bharat Forge Ltd", "Pune")
    r2 = {"title": "Swapnil Vispute - Strategic sourcing for Defence l Purchase l Supply chain ...",
          "url": "https://in.linkedin.com/in/sv",
          "snippet": "Buyer - R&D Defence. Bharat Forge Ltd. Apr 2021 - Present 5 years 8 months. Pune Division,"}
    p2 = parse_profile(r2)
    assert p2["name"] == "Swapnil Vispute" and "sourcing" in (p2["title"] or "").lower()


def test_parse_profile_rejects_non_profiles():
    assert parse_profile({"title": "Bharat Forge Hunts For Acquisitions", "url": "https://ndtvprofit.com/x",
                          "snippet": ""}) is None


def test_parse_post_author():
    assert parse_post({"title": "Ravi Kumar on LinkedIn: Visited IMTEX 2025 with team", "url": "u"})["author"] == "Ravi Kumar"
    assert parse_post({"title": "Anita Rao's Post - LinkedIn", "url": "u"})["author"] == "Anita Rao"


def test_title_keywords_and_excludes():
    icp = {"title_keywords": ["purchase", "procurement"], "exclude_keywords": ["intern"]}
    assert title_matches("Head - Purchase & Supply Chain", icp)
    assert not title_matches("Procurement Intern", icp)
    assert not title_matches("Plant Manager", icp)


REAL_RESULTS = [  # captured from OpenSERP/Google on 2026-11-18 for: site:linkedin.com/in "Purchase Head" "Bharat Forge"
    {"title": "ramesh shrigod - Vice President at Bharat Forge Ltd", "url": "https://in.linkedin.com/in/ramesh-shrigod-201341359",
     "snippet": "Pune Division, Maharashtra, India · Vice President · Bharat Forge Ltd"},
    {"title": "Deepak Kajale - Bharat Forge Ltd", "url": "https://www.google.com/goto?gsessionid=x",
     "snippet": "Pune, Maharashtra, India · Senior Manager Tool Planning and Procurement · Bharat Forge Ltd"},
    {"title": "mohit saxena - Asst Manager procurement at Bharat Forge ...", "url": "https://in.linkedin.com/in/mohit-saxena-74692025",
     "snippet": "Noida, Uttar Pradesh, India · Asst Manager procurement · Bharat Forge Ltd"},
    {"title": "Chetan Sharma - Deputy Manager Procurement", "url": "https://in.linkedin.com/in/chetan-sharma-231a7a38",
     "snippet": "Pune Division, Maharashtra, India · Deputy Manager · KSB India"},
    {"title": "Bharat Forge Ltd Sales Department", "url": "https://www.google.com/goto?gsessionid=y",
     "snippet": "Bharat Forge Ltd's Sales department is led by Anendd Sharrma"},
    {"title": "Subhash Bharadwaj - Senior Manager at Bharat Forge Ltd", "url": "https://www.google.com/goto?gsessionid=z",
     "snippet": "Pune/Pimpri-Chinchwad Area · Senior Manager · Bharat Forge Ltd"},
]


def _fresh_db():
    tmp = Path(tempfile.mkdtemp())
    from leadfinder import config
    config.DB_PATH, config.OUTPUT = tmp / "t.db", tmp
    from leadfinder import db
    con = db.connect()
    con.execute("INSERT INTO industries(slug,name,geo,icp_json,icp_approved) VALUES ('t','T','IN',?,1)",
                (Path(__file__).resolve().parent.parent.joinpath("tests/fixtures/icp_example.json").read_text(),))
    return con, db


def test_ingest_real_linkedin_results_keeps_only_procurement_at_target():
    from leadfinder.people import _ingest_people
    con, db = _fresh_db()
    ind = db.get_industry(con, "t")
    cid = db.upsert_company(con, "Bharat Forge Ltd", industry_id=ind["id"])
    added = _ingest_people(con, REAL_RESULTS, {"company_id": cid, "company": "Bharat Forge Ltd", "icp": "C",
                                               "industry_id": ind["id"]})
    names = sorted(r["name"] for r in con.execute("SELECT name FROM people").fetchall())
    # procurement titles at Bharat Forge kept; VP / generic Senior Manager / other company / dept page dropped
    assert names == ["Deepak Kajale", "Mohit Saxena"], names
    assert added == 2


def test_pdf_url_answered_with_html_is_rejected(monkeypatch=None):
    from leadfinder.providers import fetch as f

    class R:
        ok, status_code, headers = True, 200, {"content-type": "text/html"}
        content, text = b"<html>home page</html>", "<html>" + "home page " * 200 + "</html>"
    orig = f.requests.get
    f.requests.get = lambda *a, **k: R()
    try:
        try:
            f.direct("https://example.org/assets/FairGuide2025.pdf")
            assert False, "soft-404 PDF should raise"
        except RuntimeError as e:
            assert "expected a PDF" in str(e)
    finally:
        f.requests.get = orig


def test_empty_results_not_cached_and_marked_empty():
    from leadfinder import people
    from leadfinder.providers import search as router
    con, db = _fresh_db()
    ind = db.get_industry(con, "t")
    people.enqueue(con, "people", '"Acme" ("purchase")', "linkedin.com/in",
                   {"company_id": 1, "company": "Acme", "icp": "C", "industry_id": ind["id"]})
    con.commit()

    class Empty:
        name, monthly_free = "fake", None
        def available(self): return True
        def search(self, *a, **k): return []
    orig = router.PROVIDERS
    router.PROVIDERS = [Empty()]
    try:
        stats = people.run_queue(con, 5, log=lambda m: None)
    finally:
        router.PROVIDERS = orig
    assert stats["done"] == 1
    assert con.execute("SELECT status FROM search_queue").fetchone()[0] == "empty"
    assert con.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0] == 0


def test_end_to_end_tracker_roundtrip():
    """Add data, export, edit Tracker in the xlsx, export again: the edit must survive."""
    tmp = Path(tempfile.mkdtemp())
    from leadfinder import config
    config.DB_PATH, config.OUTPUT = tmp / "t.db", tmp
    from openpyxl import load_workbook
    from leadfinder import db
    from leadfinder.export import export
    con = db.connect()
    con.execute("INSERT INTO industries(slug,name,geo,icp_json,icp_approved) VALUES ('t','T','IN',?,1)",
                (Path(__file__).resolve().parent.parent.joinpath("tests/fixtures/icp_example.json").read_text(),))
    ind = db.get_industry(con, "t")
    for i, d in enumerate(["2025-01-23", "2025-05-10", "2025-09-22"]):
        con.execute("INSERT INTO shows(industry_id,name,year,start_date) VALUES (?,?,?,?)", (ind["id"], f"S{i}", 2025, d))
    cid = db.upsert_company(con, "Acme Machines Pvt Ltd", "acme.in", industry_id=ind["id"])
    for sid in (1, 2, 3):
        db.add_evidence(con, "company", cid, "exhibitor", "confirmed", f"https://show{sid}/list", show_id=sid)
    pid = db.upsert_person(con, cid, "Asha Patel", "National Sales Head", linkedin_url="https://in.linkedin.com/in/asha")
    con.commit()
    r1 = export(con, ind)
    assert r1["leads"] == 1 and r1["by_icp"] == {"A": 1}
    wb = load_workbook(r1["file"])
    assert {"Leads", "Companies", "Tracker", "Evidence", "Shows"} <= set(wb.sheetnames) and "Outreach" not in wb.sheetnames
    ws = wb["Tracker"]
    ws["E2"], ws["J2"] = "interviewed", "great call - loses notes after shows"
    wb.save(r1["file"])
    r2 = export(con, ind)
    assert r2["tracker_rows_synced_from_previous"] == 1
    ws = load_workbook(r2["file"])["Tracker"]
    assert ws["E2"].value == "interviewed" and "loses notes" in ws["J2"].value
    lead = load_workbook(r2["file"])["Leads"]
    assert lead["C2"].value >= 70 and lead["D2"].value == "likely"
    assert pid


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f()
        print("ok", f.__name__)
