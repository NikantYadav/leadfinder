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
    # the dated current job beats the cut-off headline in the result title
    assert (p2["name"], p2["title"], p2["company"]) == ("Swapnil Vispute", "Buyer - R&D Defence", "Bharat Forge Ltd")


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
    assert lead["C2"].value >= 70 and lead["D2"].value == "company at a show"
    assert pid

def test_seen_at_show_kept_without_company_and_attendee_show_choice():
    """A person's own show post beats the company rule and needs no company; the attendee step reads core shows only,
    never one running today or one the evidence step added (a company's other fair)."""
    tmp = Path(tempfile.mkdtemp())
    from leadfinder import config
    config.DB_PATH, config.OUTPUT = tmp / "t.db", tmp
    from datetime import date
    from leadfinder import db
    from leadfinder.export import build_leads, at_show_label
    from leadfinder.pipeline import recent_shows
    con = db.connect()
    con.execute("INSERT INTO industries(slug,name,geo,icp_json,icp_approved) VALUES ('t','T','IN',?,1)",
                (Path(__file__).resolve().parent.parent.joinpath("tests/fixtures/icp_example.json").read_text(),))
    ind = db.get_industry(con, "t")
    rows = [("Core past", "2026-10-01", "2026-10-03", None), ("Running", "2026-11-23", "2026-11-26", None),
            ("Other fair", "2026-11-01", "2026-11-03", "evidence"), ("Core soon", "2026-12-10", "2026-12-11", None)]
    for n, a, b, o in rows:
        con.execute("INSERT INTO shows(industry_id,name,year,start_date,end_date,origin) VALUES (?,?,2026,?,?,?)",
                    (ind["id"], n, a, b, o))
    assert [s["name"] for s in recent_shows(con, ind, date(2026, 11, 24))] == ["Core soon", "Core past"]
    # naming a running show (any spelling of its year) lets it in, first
    con.execute("UPDATE industries SET persona_text='sales heads who are at Running-2026 this week' WHERE id=?", (ind["id"],))
    ind = db.get_industry(con, "t")
    assert [s["name"] for s in recent_shows(con, ind, date(2026, 11, 24))] == ["Running", "Core soon", "Core past"]
    con.execute("UPDATE industries SET persona_text='' WHERE id=?", (ind["id"],))
    ind = db.get_industry(con, "t")
    pid = db.upsert_person(con, None, "Ravi Kumar", "National Sales Head", source="apify_show_people")
    db.add_evidence(con, "person", pid, "visitor_post", "inferred", "https://linkedin.com/posts/x", "at the show", 1)
    con.commit()
    leads = build_leads(con, ind)
    assert len(leads) == 1 and leads[0]["company"] is None and leads[0]["seen"] and leads[0]["icp"] == "A"
    assert at_show_label(leads[0]) == "seen at a show"


# ---- real TinySearch / Serper excerpts that the old parser got wrong (cached 2026-11-23/24) ----
def _li(title, snippet, url="https://in.linkedin.com/in/x"):
    return parse_profile({"title": title, "url": url, "snippet": snippet})


def test_markdown_excerpt_current_role_and_clean_location():
    p = _li("Garima Bhatt", "Key Account Manager at HP India Sales Pvt Ltd\n- **Dates:** June 2009 – Present\n\uf0f0 "
            "Managing 40 major accounts across manufacturing, IT &amp; ITES\n\n# Garima Bhatt\nKey Account Manager at HP "
            "India Sales Pvt Ltd\n## Profile\n- **Location:** Chennai, Tamil Nadu, India\n## Experience\n### Key Account "
            "Manager at HP India Sales Pvt Ltd\n- **Dates:** June 2009 – Present\n\uf0f0 Managing 40 major")
    assert (p["title"], p["company"], p["location"]) == \
        ("Key Account Manager", "HP India Sales Pvt Ltd", "Chennai, Tamil Nadu, India")
    assert not p["past_only"]


def test_markdown_excerpt_with_only_past_jobs_is_past_only():
    # the old parser stored "Sales Engineer at Goma" (a 2016-2017 job) as the current role
    p = _li("Raj Shekar Gella", "Control Valves, Heat Exchangers &amp; Aluminum piping etc.\n### Sales Engineer at Goma "
            "Engineering Private Limited\n- **Dates:** June 2016 – July 2017\n- **Location:**")
    assert p["past_only"] and p["title"] is None and p["company"] is None
    assert p["past_companies"] == ["Goma Engineering Private Limited"]
    p = _li("Om prakash", "### Business Development Manager - India (Steel and Aluminium Segment) at Kanthal\n- **Dates:** "
            "March 2023 – April 2026\n- **Location:** Pan india\n• Led pan-India\n\n# Om prakash\nIIM Calcutta Co’27 | "
            "1 Year Full Time MBA. B2B Growth & Business Development | Industrial\n## Profile\n- **Location:** Greater "
            "Kolkata Area")
    assert p["past_only"] and p["location"] == "Greater Kolkata Area"


def test_markdown_excerpt_present_job_beats_older_ones():
    p = _li("Prashant Patil", "### Key Account Manager Supply & Sourcing at Zilingo Global Private Limited\n- **Dates:** "
            "June 2021 – December 2022\n\nDevelopment • Expert in Textile Fabric Inspection\n## Experience\n### General "
            "Manager Operations at P K Textiles\n- **Dates:** April 2024 – Present\n- **Location:** Bengaluru, Karnataka, "
            "India\n### Sourcing")
    assert (p["title"], p["company"]) == ("General Manager Operations", "P K Textiles")
    assert p["past_companies"] == ["Zilingo Global Private Limited"]
    # an undated older entry is not used when a current job is visible (its title was cut off)
    p = _li("Gopikrishnan Palanisamy", "at Arjun engineering equipments\n- **Dates:** June 2015 – Present\nManufacturer's "
            "of all kind of Air Compressor\n### Sales Engineer at Atlas Copco\n- **Dates:** November 2014")
    assert p["title"] is None and p["roles"] == [(None, "Arjun engineering equipments")]


def test_markdown_excerpt_cut_off_current_job_is_not_past_only():
    p = _li("Vaibhav Jain", "Currently working as a Key Account Manager at Trident Group in their Bed Linen division , "
            "responsible for forging strategic partnerships\n\n– Present\n- **Location:** Gurugram, Haryana, India\n"
            "### Sales Merchandiser at Trident Group India\n- **Dates:** December 2018 – July 2021")
    assert not p["past_only"] and p["title"] is None   # prose is never a title
    p = _li("Nandan Kumar Pandey", "**Dates:** May 2025 – Present\n- **Location:** Surat, Gujarat, India\n### Business "
            "Development Manager at Ganpati shree textile\n- **Dates:** April 2024 – April 2025")
    assert not p["past_only"]


def test_headline_roles():
    p = _li("Mohammed Tahir", "Founder KAPADA Sustainable Clothing | Managing Director at ANECON GLOBAL EXPORT\n## Profile\n"
            "- **Location:** Greater Coimbatore Area\n\nyou do that long enough, the body absorbs everything")
    assert (p["title"], p["company"], p["location"]) == ("Managing Director", "ANECON GLOBAL EXPORT", "Greater Coimbatore Area")
    # the headline names a current job too, next to a dated one
    p = _li("Sunit Basak", "Business Development Manager at Chandrani Pearls\n## Profile\n- **Location:** Kolkata\n\n"
            "## Experience\n### Area Sales Manager at Huechem Textiles Pvt. Ltd.\n- **Dates:** June 2019 – Present")
    assert ("Business Development Manager", "Chandrani Pearls") in p["roles"]


def test_serper_labels_and_cut_titles():
    p = _li("✈️Manjeet Rana✈️ - Procurement - Airbus Group India", "Procurement - Airbus Group India · Experience: Airbus "
            "Group · Education: WLC College · Location: Bengaluru · 500+ connections on LinkedIn.")
    assert (p["company"], p["location"]) == ("Airbus Group", "Bengaluru")
    p = _li("Parthiban S. - Business Development Executive at Sales and ...", "Jyoti CNC Automation Ltd. ... Responsible "
            "for sales and business development of CNC machine tools across Tamil Nadu.")
    assert p["company"] is None and "Jyoti CNC" in p["context"]       # "Sales and ..." is a cut-off company
    p = _li("Vijayalakshmi Banerjee - Senior Global Strategic Sourcing and ...", "Experience ; Global Sr Sourcing "
            "Specialist. Rolls-Royce. Jun 2019 - Present 7 years 6 months ; Sr. Purchase excecutive. Wipro Technologies.")
    assert (p["title"], p["company"]) == ("Global Sr Sourcing Specialist", "Rolls-Royce")


def test_role_at_target_company():
    from leadfinder.people import _role_at
    past = _li("Raj Shekar Gella", "### Sales Engineer at Goma Engineering Private Limited\n- **Dates:** June 2016 – July 2017")
    assert _role_at(past, "Goma Engineering Pvt. Ltd.") == (None, False)
    moved = _li("Prashant Patil", "### KAM at Zilingo Global Private Limited\n- **Dates:** June 2021 – December 2022\n\n"
                "### General Manager Operations at P K Textiles\n- **Dates:** April 2024 – Present")
    assert _role_at(moved, "Zilingo Global Private Limited") == (None, False)
    assert _role_at(moved, "P K Textiles") == ("General Manager Operations", True)


def test_employer_matching_and_company_name_cleanup():
    from leadfinder.people import _employer_matches as m, clean_company_name as c
    assert m("HAL", "Hindustan Aeronautics Ltd (HAL)") and m("Bharat Heavy Electricals Limited", "BHEL")
    assert m("Larsen & Toubro", "L&T Construction") and m("Raj Seals Engineering", "Raj Seal Engineering")
    assert m("Godrej Appliances", "Godrej & Boyce") and m("TVS", "TVS Motor Company")
    assert not m("P K Textiles", "Zilingo Global Private Limited") and not m("Arvind Limited", "Kk Birla Grpstil Limited")
    assert c("Uma Industries | Engineering Machinery Manufacturer & Exporter | Industrial Gearbox") == "Uma Industries"
    assert c("K.L. ISPAT PVT LTD deals in Ductile and Cast Iron Graded machined components") == "K.L. ISPAT PVT LTD"
    assert c("Mumbai Looking after Key Accounts and OEM business, responsible for Sales") is None
    assert c("Bharat Forge Ltd") is None


def test_reparse_fixes_people_from_saved_results():
    con, db = _fresh_db()
    from leadfinder.people import reparse_cached
    ind = db.get_industry(con, "t")
    goma = db.upsert_company(con, "Goma Engineering Pvt. Ltd.", industry_id=ind["id"])
    hp = db.upsert_company(con, "HP India Sales Pvt Ltd", industry_id=ind["id"])
    a = db.upsert_person(con, goma, "Raj Shekar Gella", "Sales Engineer", "** ", "https://in.linkedin.com/in/raj-g",
                         source="search")
    b = db.upsert_person(con, hp, "Garima Bhatt", "Key Account Manager",
                         "** Chennai, Tamil Nadu, India\n## Experience", "https://in.linkedin.com/in/garima-b",
                         source="search")
    con.execute("INSERT INTO tracker(person_id, status) VALUES (?, 'contacted')", (a,))
    rows = [{"title": "Raj Shekar Gella", "url": "https://in.linkedin.com/in/raj-g",
             "snippet": "### Sales Engineer at Goma Engineering Private Limited\n- **Dates:** June 2016 – July 2017"},
            {"title": "Garima Bhatt", "url": "https://in.linkedin.com/in/garima-b",
             "snippet": "# Garima Bhatt\nKey Account Manager at HP India Sales Pvt Ltd\n## Profile\n- **Location:** "
                        "Chennai, Tamil Nadu, India"}]
    con.execute("INSERT INTO search_cache VALUES ('tinysearch','q','{}',?,'now')", (json.dumps(rows),))
    con.commit()
    dry = reparse_cached(con)
    assert dry["flag_left"] == 1 and dry["location_fixed"] == 2
    assert con.execute("SELECT profile_flag FROM people WHERE id=?", (a,)).fetchone()[0] is None   # dry run
    reparse_cached(con, apply=True)
    ra = con.execute("SELECT * FROM people WHERE id=?", (a,)).fetchone()
    rb = con.execute("SELECT * FROM people WHERE id=?", (b,)).fetchone()
    assert ra["profile_flag"].startswith("left Goma") and ra["location"] is None
    assert rb["profile_flag"] is None and rb["location"] == "Chennai, Tamil Nadu, India"
    assert con.execute("SELECT COUNT(*) FROM tracker").fetchone()[0] == 1          # nothing deleted
    assert reparse_cached(con)["unchanged"] == 2                                   # idempotent


def test_serper_cut_titles_keep_company_and_past_jobs():
    # a cut-off result title keeps the company minus its (maybe cut) last word
    p = _li("Rajesh S - Senior Executive Purchase at Hindalco Industries Limited EX ...", "Experience ; Senior Executive "
            "Purchase. Hindalco Industries Limited. Nov 2026 ; Purchase Executive(SCM) -TEPS. Tata Electronics. Aug 2024 ;")
    assert p["company"] == "Hindalco Industries Limited" and p["past_companies"] == ["Tata Electronics"]
    p = _li("Umang Kapoor - AGM Purchase at Highway Roop precision ...", "Purchase Manager. Musashi Auto Parts India Pvt "
            "Ltd. Mar 2023 - Nov 2025 2 years 9 months. Bawal, Haryana, India. Purchase Manager. MUVIQ. Nov 2010 - Mar 2023 12")
    assert p["company"] == "Highway Roop" and "Musashi Auto Parts India Pvt Ltd" in p["past_companies"]
    assert "Musashi" not in p["context"]                     # a past job never counts as working there
    p = _li("Madhu Kiran - Purchase Manager at Honda Motorcycle & Scooter India ...", "Purchase Manager. Honda Motorcycle "
            "& Scooter India Pvt. Ltd. May 2013 - Present 13 years 7 months. Kolar, Karnataka, India.")
    assert p["company"] == "Honda Motorcycle & Scooter India Pvt Ltd" and not p["past_companies"]
    # a current job with no company (maybe a promotion) is not "left"
    p = _li("Akash Narvankar - Senior Technical Sales Head", "Senior Technical Sales Head. Jun 2024 - Present. Senior Sales "
            "Representative Supercut Engineers Pvt. Ltd. May 2022 - Feb 2024")
    assert p["orphan_current"]


def test_reparse_flags_need_no_current_mention():
    con, db = _fresh_db()
    from leadfinder.people import reparse_cached
    ind = db.get_industry(con, "t")
    rows, ids = [], {}
    for name, co, title, snippet in [
            ("Umang Kapoor", "Musashi Auto Parts India", "Umang Kapoor - AGM Purchase at Highway Roop precision ...",
             "Purchase Manager. Musashi Auto Parts India Pvt Ltd. Mar 2023 - Nov 2025 2 years 9 months."),
            ("Maneesh Bhardwaj", "Escorts Kubota", "Maneesh Bhardwaj - Head of Supply Chain Management at CNH ...",
             "Head of Sourcing - Procurement. Escorts Kubota Limited. Oct 2020 - Jun 2023 2 years 9 months."),
            ("Sunil Kumar S", "Ather Energy", "Sunil Kumar S - Senior Manager - Strategic Sourcing - Ather Energy",
             "Experience ; Sr. Engineer - Sourcing. Zetwerk. Feb 2020 ; Sr. Engineer - Purchase. Ather Energy. May 2017 ;"),
            ("Abhijit Purkayastha", "Asahi India Glass Limited (AIS)", "Abhijit Purkayastha",
             "### Key Account Manager-East at Asahi India Glass Limited (AIS)\n- **Dates:** January 2023 – March 2026\n\n"
             "# Abhijit Purkayastha\nSr. Key Account Manager-East, Asahi Glass India Ltd\n## Profile\n- **Location:** "
             "Kolkata, West Bengal, India")]:
        url = "https://in.linkedin.com/in/" + name.lower().replace(" ", "-")
        cid = db.upsert_company(con, co, industry_id=ind["id"])
        ids[name] = db.upsert_person(con, cid, name, "Purchase Manager", None, url, source="search")
        rows.append({"title": title, "url": url, "snippet": snippet})
    con.execute("INSERT INTO search_cache VALUES ('serper','q','{}',?,'now')", (json.dumps(rows),))
    con.commit()
    reparse_cached(con, apply=True)
    flag = {n: con.execute("SELECT profile_flag FROM people WHERE id=?", (i,)).fetchone()[0] for n, i in ids.items()}
    assert flag["Umang Kapoor"] == "now at Highway Roop (search excerpt)"
    assert flag["Maneesh Bhardwaj"].startswith("left Escorts Kubota?")
    assert flag["Sunil Kumar S"] is None            # the result title still names Ather Energy
    assert flag["Abhijit Purkayastha"] is None      # the headline names the company, words in another order


def test_groups_are_per_search():
    """The same person fits a group in one search and none (or another) in a second; exporting one search must not
    change the other's group, and a locked group survives the export."""
    con, db = _fresh_db()
    from leadfinder.export import build_leads
    t = db.get_industry(con, "t")
    con.execute("INSERT INTO industries(slug,name,geo,icp_json,icp_approved) VALUES ('s','S','IN',?,1)",
                (json.dumps({"icps": [{"code": "S1", "name": "Sales", "title_keywords": ["sales"],
                                       "company_rule": "any"}]}),))
    s = db.get_industry(con, "s")
    cid = db.upsert_company(con, "Acme Forge Ltd", industry_id=t["id"])
    db.upsert_company(con, "Acme Forge Ltd", industry_id=s["id"])
    pid = db.upsert_person(con, cid, "Ravi Kumar", "Head of Purchase", linkedin_url="https://in.linkedin.com/in/rk",
                           source="search")
    other = db.upsert_person(con, cid, "Asha Rao", "Sales Manager", icp="S1", industry_id=s["id"], source="manual")
    db.add_evidence(con, "company", cid, "visitor_post", "likely", "https://linkedin.com/posts/x")   # fits C in t
    con.commit()
    groups = lambda ind: db.groups_for(con, ind["id"])  # noqa: E731
    build_leads(con, s)
    assert groups(s)[pid] == (None, 0) and groups(s)[other] == ("S1", 1)
    t_leads = {l["id"]: l["icp"] for l in build_leads(con, t)}
    assert groups(s)[pid] == (None, 0)                    # search t's export left search s alone
    assert t_leads[pid] == "C" and groups(t)[pid] == ("C", 0)        # same person, a different group per search
    con.execute("UPDATE people SET title='Sales Director' WHERE id=?", (other,))
    build_leads(con, s)
    assert groups(s)[other] == ("S1", 1)                  # locked: not re-derived from the title
    try:
        db.upsert_person(con, cid, "No Search", "Buyer", icp="C")
        raise AssertionError("a group without its search must be refused")
    except ValueError:
        pass


def test_sidebar_counts_match_the_results_list():
    """The sidebar/search page count is the results list: grouped, not flagged, company-less show visitors included."""
    con, db = _fresh_db()
    from leadfinder.export import build_leads
    t = db.get_industry(con, "t")
    cid = db.upsert_company(con, "Acme Forge Ltd", industry_id=t["id"])
    db.add_evidence(con, "company", cid, "visitor_post", "likely", "https://linkedin.com/posts/x")
    shown = db.upsert_person(con, cid, "Ravi Kumar", "Head of Purchase", email="r@acme.in", source="search")
    hidden = db.upsert_person(con, cid, "Old Hand", "Purchase Manager", source="search")
    con.execute("UPDATE people SET profile_flag='left Acme Forge Ltd? (search excerpt shows only a past job there)' "
                "WHERE id=?", (hidden,))
    db.upsert_person(con, cid, "Plant Guy", "Plant Engineer", source="search")          # fits no group
    con.execute("INSERT INTO shows(id, industry_id, name, year) VALUES (5, ?, 'Expo', 2026)", (t["id"],))
    visitor = db.upsert_person(con, None, "Seen Person", "National Sales Head", source="apify_show_people")
    db.add_evidence(con, "person", visitor, "visitor_post", "inferred", "https://linkedin.com/posts/y", "", 5)
    con.commit()
    leads = build_leads(con, t)
    listed = [l for l in leads if not l.get("profile_flag")]
    assert {l["id"] for l in listed} == {shown, visitor}
    assert db.listed_counts(con, t["id"]) == {"people": 2, "emails": 1, "people_all": 3}


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f()
        print("ok", f.__name__)
