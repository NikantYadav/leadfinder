"""Phase 2 tests: provider response parsing (shapes copied from each provider's docs, 2026-11-18),
waterfall behaviour, profile flags. No network. Run: ./env/bin/python tests/test_phase2.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update({"PROSPEO_API_KEY": "k", "HUNTER_API_KEY": "k", "APOLLO_API_KEY": "k", "TOMBA_API_KEY": "k",
                   "TOMBA_API_SECRET": "s", "BETTERCONTACT_API_KEY": "k", "APIFY_TOKEN": "t",
                   "TARGET_COUNTRIES": "India,IN"})

from leadfinder.providers import email as E  # noqa: E402
from leadfinder.providers import linkedin as L  # noqa: E402
from leadfinder.providers.search import ProviderBusy  # noqa: E402


class R:
    def __init__(self, status, body):
        self.status_code, self._b, self.ok, self.text = status, body, 200 <= status < 300, str(body)

    def json(self):
        return self._b


def patch(mod, get=None, post=None):
    mod.requests.get = get or (lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected GET")))
    mod.requests.post = post or (lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected POST")))


P = {"name": "Baburao Solanki", "company": "Bharat Forge Ltd", "domain": "bharatforge.com",
     "linkedin_url": "https://in.linkedin.com/in/baburao-s"}


def test_hunter_found_and_quota():
    body = {"data": {"email": "alexis@reddit.com", "score": 97, "accept_all": False, "position": "Cofounder",
                     "company": "Reddit", "domain": "reddit.com", "verification": {"date": "2021-06-14", "status": "valid"}}}
    seen = {}
    patch(E, get=lambda url, params=None, **k: (seen.update(params), R(200, body))[1])
    res = E.Hunter().find(P)
    assert res["email"] == "alexis@reddit.com" and res["status"] == "verified" and res["score"] == 97
    assert seen["domain"] == "bharatforge.com" and seen["first_name"] == "Baburao" and "linkedin_handle" in seen
    patch(E, get=lambda *a, **k: R(429, {"errors": [{"id": "too_many_requests"}]}))
    try:
        E.Hunter().find(P)
        assert False
    except ProviderBusy:
        pass


def test_prospeo_verified_masked_and_errors():
    ok = {"error": False, "person": {"full_name": "Eoghan Mccabe", "current_job_title": "CEO",
                                     "email": {"status": "VERIFIED", "revealed": True, "email": "eoghan@intercom.com"},
                                     "location": {"country": "United States", "city": "San Francisco"}},
          "company": {"name": "Intercom", "domain": "intercom.io"}}
    sent = {}
    patch(E, post=lambda url, json=None, headers=None, **k: (sent.update(json=json, h=headers), R(200, ok))[1])
    res = E.Prospeo().find(P)
    assert res["status"] == "verified" and res["extra"]["country"] == "United States"
    assert sent["h"]["X-KEY"] == "k" and sent["json"]["only_verified_email"] is True
    assert sent["json"]["data"]["linkedin_url"] == P["linkedin_url"]
    masked = {"error": False, "person": {"email": {"status": "VERIFIED", "email": "eoghan.*****@intercom.com"}}}
    patch(E, post=lambda *a, **k: R(200, masked))
    assert E.Prospeo().find(P) is None
    patch(E, post=lambda *a, **k: R(400, {"error": True, "error_code": "NO_MATCH"}))
    assert E.Prospeo().find(P) is None
    patch(E, post=lambda *a, **k: R(400, {"error": True, "error_code": "INSUFFICIENT_CREDITS"}))
    try:
        E.Prospeo().find(P)
        assert False
    except ProviderBusy:
        pass


def test_apollo_match_and_no_match():
    body = {"person": {"email": "jordan.blake@northstaranalytics.io", "email_status": "verified",
                       "match_confidence": "high", "title": "Founder & CEO", "city": "San Francisco",
                       "country": "United States", "organization": {"name": "Northstar", "primary_domain": "northstaranalytics.io"}}}
    sent = {}
    patch(E, post=lambda url, params=None, headers=None, **k: (sent.update(p=params, h=headers), R(200, body))[1])
    res = E.Apollo().find(P)
    assert res["status"] == "verified" and sent["h"]["x-api-key"] == "k" and sent["p"]["organization_name"]
    patch(E, post=lambda *a, **k: R(200, {"person": {"match_confidence": "none"}}))
    assert E.Apollo().find(P) is None


def test_tomba_finder_and_linkedin_fallback():
    body = {"data": {"email": "jane.doe@stripe.com", "score": 100, "verification": {"status": "accept_all"},
                     "position": "PM", "company": "Stripe", "website_url": "stripe.com", "country": "US"}}
    calls = []
    patch(E, get=lambda url, params=None, headers=None, **k: (calls.append((url, headers)), R(200, body))[1])
    res = E.Tomba().find(P)
    assert res["status"] == "catch_all" and calls[0][0].endswith("/email-finder")
    assert calls[0][1] == {"X-Tomba-Key": "k", "X-Tomba-Secret": "s"}
    E.Tomba().find({"name": "X Y", "linkedin_url": "https://www.linkedin.com/in/x"})
    assert calls[1][0].endswith("/v1/linkedin")
    patch(E, get=lambda *a, **k: R(402, {"errors": {"type": "quota_exceeded"}}))
    try:
        E.Tomba().find(P)
        assert False
    except ProviderBusy:
        pass


def test_bettercontact_async_poll():
    E.time.sleep = lambda s: None
    polls = iter([R(202, {"status": "processing"}),
                  R(200, {"status": "terminated", "data": [{"enriched": True, "contact_email_address": "elon@tesla.com",
                                                             "contact_email_address_status": "deliverable"}]})])
    patch(E, post=lambda *a, **k: R(201, {"success": True, "id": "req1"}), get=lambda *a, **k: next(polls))
    res = E.BetterContact().find(P)
    assert res["email"] == "elon@tesla.com" and res["status"] == "verified"
    assert E.BetterContact().find({"name": "A B", "company": "X"}) is None  # needs a domain


def test_apify_profile_parsing_and_flags():
    items = [{"firstName": "Towhid", "lastName": "Rahman", "headline": "Pharmacist", "linkedinUrl":
              "https://www.linkedin.com/in/towhid-rahman", "publicIdentifier": "towhid-rahman",
              "currentPosition": [{"companyName": "CVS Health"}],
              "location": {"parsed": {"city": "Los Angeles", "country": "United States", "countryCode": "US"}}}]
    sent = {}
    patch(L, post=lambda url, json=None, headers=None, **k: (sent.update(url=url, json=json, h=headers), R(201, items))[1])
    out = L.profiles_apify(["https://www.linkedin.com/in/towhid-rahman"])
    p = out["towhid-rahman"]
    assert p["company"] == "CVS Health" and p["country"] == "United States"
    assert "/v2/actors/harvestapi~linkedin-profile-scraper/run-sync-get-dataset-items" in sent["url"]
    assert sent["h"]["Authorization"] == "Bearer t" and sent["json"]["queries"]
    from leadfinder.enrich import profile_flag
    assert profile_flag(p, "CVS Health") == "outside target geo (United States)"
    assert profile_flag({"country": "India", "company": "Tata Motors"}, "Bharat Forge Ltd") == "now at Tata Motors"
    assert profile_flag({"country": "India", "company": "Bharat Forge Limited"}, "Bharat Forge Ltd") is None
    assert profile_flag({"country_code": "IN", "company": "LMW Machine Tool Division"}, "LMW Ltd") is None


def test_apify_post_search_parsing():
    items = [{"content": "Visited IMTEX 2025 with team", "linkedinUrl": "https://www.linkedin.com/posts/x_activity-1",
              "author": {"name": "Ravi Kumar", "info": "Purchase Head at Wheels India",
                         "linkedinUrl": "https://www.linkedin.com/in/ravi"}, "postedAt": {"date": "2025-01-25T10:00:00Z"}}]
    patch(L, post=lambda *a, **k: R(201, items))
    posts = L.search_posts(['"IMTEX 2025"'], 20)
    assert posts[0]["author"] == "Ravi Kumar" and posts[0]["author_headline"].startswith("Purchase Head")


def test_waterfall_stops_at_verified_and_skips_exhausted():
    import tempfile
    from leadfinder import config, db, enrich
    tmp = Path(tempfile.mkdtemp())
    config.DB_PATH, config.OUTPUT = tmp / "t.db", tmp
    con = db.connect()
    con.execute("INSERT INTO industries(slug,name,geo,icp_json,icp_approved) VALUES ('t','T','IN',?,1)",
                (Path(__file__).resolve().parent.parent.joinpath("tests/fixtures/icp_example.json").read_text(),))
    ind = db.get_industry(con, "t")
    cid = db.upsert_company(con, "Wheels India", "wheelsindia.com", industry_id=ind["id"])
    db.add_evidence(con, "company", cid, "report_named", "likely", "https://x/report.pdf", show_id=None)
    for n in ("Ravi Shankar", "Vijay Nelson"):
        db.upsert_person(con, cid, n, "Sr. GM (Purchase)")
    con.commit()
    calls = []

    class Busy:
        name, free_note = "busy", ""
        def available(self): return True
        def find(self, p): calls.append("busy"); raise ProviderBusy("quota")

    class Catch:
        name, free_note = "catch", ""
        def available(self): return True
        def find(self, p): calls.append("catch"); return {"email": "x@wheelsindia.com", "status": "catch_all", "source": "catch", "extra": {}}

    class Never:
        name, free_note = "never", ""
        def available(self): return True
        def find(self, p): calls.append("never"); return None

    E.PROVIDERS[:] = [Busy(), Catch(), Never()]
    stats = enrich.enrich_emails(con, ind, top=10, log=lambda m: None)
    assert stats["found"] == 2 and stats["statuses"] == {"catch_all": 2}
    assert calls.count("busy") == 1          # exhausted after first failure, not retried for 2nd lead
    assert "never" not in calls              # catch_all is accepted -> stop
    stats2 = enrich.enrich_emails(con, ind, top=10, log=lambda m: None)
    assert stats2["tried"] == 0              # already have emails


def test_domain_guess_rules():
    from leadfinder.enrich import domain_match_score as m
    from leadfinder.normalize import norm_domain
    assert m("Wheels India", "shop.mattel.com.au") == 0
    assert m("Bajaj Auto", "haryanatransport.gov.in") == 0
    assert m("Ather Energy", "google.co.in") == 0
    assert m("Toyota Industries Engine India", "manageengine.com") == 0      # only a generic-ish word matched
    assert m("Bharat Electronics Ltd (BEL)", "bel-india.in") > 0               # bracketed initials
    assert m("Toyota Kirloskar Auto Parts", "tkap.com") > 0                    # initials
    assert m("Rolls-Royce India", "rolls-royce.com") > m("Rolls-Royce India", "rolls-roycemotorcars.com")
    assert norm_domain("https://jobs.boeing.com/x") == "boeing.com"
    assert norm_domain("https://www.tata.co.in") == "tata.co.in"


def test_guessed_domain_not_used_over_company_name():
    seen = {}
    patch(E, get=lambda url, params=None, **k: (seen.update(params), R(200, {"data": {}}))[1])
    E.Hunter().find({**P, "domain_trusted": False})
    assert "domain" not in seen and seen["company"] == "Bharat Forge Ltd"


def test_tinysearch_and_tinyfetch():
    os.environ["TINYFISH_API_KEY"] = "tf"
    from leadfinder.providers import fetch as F
    from leadfinder.providers import search as S
    body = {"query": "q", "page": 0, "total_results": 2, "results": [
        {"position": 1, "site_name": "in.linkedin.com", "title": "Baburao Solanki - Purchase Manager at Bharat Forge Ltd",
         "snippet": "Purchase Manager at Bharat Forge Ltd", "url": "https://in.linkedin.com/in/baburao-s"},
        {"position": 2, "site_name": "in.linkedin.com", "title": "X - Y", "snippet": "", "url": "https://in.linkedin.com/in/x"}]}
    seen = {}
    S.requests.get = lambda url, params=None, headers=None, **k: (seen.update(url=url, p=params, h=headers), R(200, body))[1]
    res = S.TinySearch().search('"Bharat Forge" purchase', site="linkedin.com/in", num=20, region="in")
    assert len(res) == 2 and seen["h"]["X-API-Key"] == "tf"
    assert seen["p"]["include_domains"] == "linkedin.com" and "site:" not in seen["p"]["query"]
    assert seen["p"]["location"] == "IN"
    S.requests.get = lambda *a, **k: R(429, {"error": {"code": "RATE_LIMIT_EXCEEDED"}})
    try:
        S.TinySearch().search("q")
        assert False
    except ProviderBusy:
        pass
    ok = {"results": [{"url": "u", "final_url": "u", "title": "Exhibitors", "text": "Acme Pvt Ltd Hall 4 A101\n" * 60,
                       "links": ["https://acme.in"]}], "errors": []}
    F.requests.post = lambda *a, **k: R(200, ok)
    assert "LINKS:" in F.tinyfetch("https://show.example/exhibitors")
    F.requests.post = lambda *a, **k: R(200, {"results": [], "errors": [{"url": "u", "error": "bot_blocked"}]})
    try:
        F.tinyfetch("https://show.example/exhibitors")
        assert False
    except RuntimeError as e:
        assert "bot_blocked" in str(e)


def test_parse_tinysearch_variants():
    from leadfinder.people import parse_profile
    # real TinySearch results, 2026-11-18
    a = parse_profile({"title": "Pranav Kanhegaonkar", "url": "https://linkedin.com/in/pranavkanhegaonkar",
                       "snippet": "### Purchase Specialist - Electrical and Electronics at Bharat Forge Ltd\n- **Location:** Pune, Maharashtra, India"})
    assert (a["title"], a["company"]) == ("Purchase Specialist - Electrical and Electronics", "Bharat Forge Ltd")
    b = parse_profile({"title": "PRAVIN CHAUDHARI", "url": "https://linkedin.com/in/pravin-chaudhari-711098a8",
                       "snippet": "Deputy Manager Purchase at Bharat Forge Ltd"})
    assert (b["title"], b["company"]) == ("Deputy Manager Purchase", "Bharat Forge Ltd")
    c = parse_profile({"title": "Sandip Ghugare", "url": "https://linkedin.com/in/sandipghugare",
                       "snippet": "District, Maharashtra, India\nManaging end-to-end procurement of machining, fabrication"})
    assert c["location"].startswith("District") and "procurement" in c["title"]
    d = parse_profile({"title": "Dnyaneshwar Shinde - MANAGER, DEFENCE BHARAT FORGE LTD",
                       "url": "https://in.linkedin.com/in/dnyaneshwar-shinde-4490",
                       "snippet": "Bharat Forge Ltd · 1. Purchasing Related Procurement of the materials required by the Defence R&D department. · 2. Control activities Responsible for"})
    assert d["title"] == "MANAGER, DEFENCE BHARAT FORGE LTD" and d["location"] is None


def test_company_page_matching_real_candidates():
    from leadfinder.enrich import company_page_score as sc
    # real TinySearch candidates, 2026-11-18
    assert sc("Wheels India", "https://linkedin.com/company/wheelsindialimited", "Wheels India Limited | LinkedIn") > \
        sc("Wheels India", "https://linkedin.com/company/wheels", "Wheels | LinkedIn")
    assert sc("Wheels India", "https://linkedin.com/company/wheels-inc-", "Wheels, LLC | LinkedIn") == 0
    assert sc("TVS Motor Company", "https://linkedin.com/company/tvs-motor-africa", "TVS Motor Africa") == 0
    assert sc("TVS Motor Company", "https://in.linkedin.com/company/tvs-motor-company", "TVS Motor Company") >= 10
    assert sc("Titan Company", "https://linkedin.com/company/titan-industries", "Titan Company | LinkedIn") > \
        sc("Titan Company", "https://linkedin.com/company/titanmsp", "Titan | LinkedIn")
    assert sc("Godrej & Boyce", "https://linkedin.com/company/godrej", "Godrej & Boyce Mfg. Co. Ltd. | LinkedIn") > 0
    assert sc("Mahindra & Mahindra", "https://linkedin.com/company/ernest-lobo-associates", "Mahindra & Mahindra | LinkedIn") == 0
    assert sc("Musashi Auto Parts India", "https://linkedin.com/company/musashi-auto-parts-indonesia", "Musashi Auto Parts Indonesia") == 0
    assert sc("Hindustan Aeronautics Ltd (HAL)", "https://linkedin.com/company/indianaeronauticltd18", "Hindustan Aeronautics") == 0
    assert sc("Hindustan Aeronautics Ltd (HAL)", "https://linkedin.com/company/hindustan-aeronautics-limited", "Hindustan Aeronautics Limited") > 0


def test_linkedin_people_search_ingest():
    import tempfile
    from leadfinder import config, db, enrich
    from leadfinder.providers import linkedin as LI
    tmp = Path(tempfile.mkdtemp())
    config.DB_PATH, config.OUTPUT = tmp / "t.db", tmp
    con = db.connect()
    con.execute("INSERT INTO industries(slug,name,geo,icp_json,icp_approved) VALUES ('t','T','IN',?,1)",
                (Path(__file__).resolve().parent.parent.joinpath("tests/fixtures/icp_example.json").read_text(),))
    ind = db.get_industry(con, "t")
    cid = db.upsert_company(con, "Wheels India", industry_id=ind["id"])
    db.add_evidence(con, "company", cid, "report_named", "likely", "https://x/r.pdf")
    con.execute("UPDATE companies SET linkedin_url='https://www.linkedin.com/company/wheelsindialimited' WHERE id=?", (cid,))
    con.commit()
    sent = {}
    items = [{"firstName": "Ravi", "lastName": "Shankar", "headline": "Sr.GM (Purchase), Wheels India Ltd",
              "linkedinUrl": "https://www.linkedin.com/in/ravi-s", "location": {"linkedinText": "Chennai, Tamil Nadu, India"},
              "currentPosition": [{"companyName": "Wheels India Limited", "position": "Senior General Manager - Purchase"}]},
             {"firstName": "Joe", "lastName": "Bloggs", "headline": "Buyer at Wheels Inc", "linkedinUrl": "https://www.linkedin.com/in/joe",
              "currentPosition": [{"companyName": "Wheels Inc"}]}]
    LI.requests.post = lambda url, json=None, headers=None, **k: (sent.update(url=url, body=json), R(201, items))[1]
    res = enrich.linkedin_people(con, ind, "C", max_items=50, log=lambda m: None)
    assert res["people_added_or_matched"] == 1 and res["unmatched_profiles"] == 1
    b = sent["body"]
    assert b["currentCompanies"] == ["https://www.linkedin.com/company/wheelsindialimited"]
    assert "21" in b["functionIds"] and b["locations"] == ["India"] and b["profileScraperMode"] == "Short"
    assert "harvestapi~linkedin-profile-search" in sent["url"]
    p = con.execute("SELECT * FROM people").fetchone()
    assert p["icp"] == "C" and p["icp_locked"] == 1 and p["title"].startswith("Senior General Manager")


def _db_with_wheels():
    import tempfile
    from leadfinder import config, db
    tmp = Path(tempfile.mkdtemp())
    config.DB_PATH, config.OUTPUT = tmp / "t.db", tmp
    con = db.connect()
    con.execute("INSERT INTO industries(slug,name,geo,icp_json,icp_approved) VALUES ('t','T','IN',?,1)",
                (Path(__file__).resolve().parent.parent.joinpath("tests/fixtures/icp_example.json").read_text(),))
    ind = db.get_industry(con, "t")
    cid = db.upsert_company(con, "Wheels India", industry_id=ind["id"])
    from datetime import date, timedelta
    st = date.today() - timedelta(days=20)
    con.execute("INSERT INTO shows(industry_id,name,year,start_date,end_date) VALUES (?,?,?,?,?)",
                (ind["id"], "Recent Show", st.year, st.isoformat(), (st + timedelta(days=4)).isoformat()))
    con.execute("INSERT INTO shows(industry_id,name,year,start_date,end_date) VALUES (?,?,?,?,?)",
                (ind["id"], "IMTEX 2025", 2025, "2025-01-23", "2025-01-29"))
    db.add_evidence(con, "company", cid, "report_named", "likely", "https://x/r.pdf")
    db.add_evidence(con, "company", cid, "exhibitor", "confirmed", "https://x/list.pdf", show_id=1)
    con.execute("UPDATE companies SET linkedin_url='https://www.linkedin.com/company/wheelsindialimited' WHERE id=?", (cid,))
    con.commit()
    return con, db, ind


def test_brightdata_business_search_people():
    os.environ["BRIGHTDATA_API_KEY"] = "bd"
    os.environ.pop("APIFY_TOKEN", None)
    from leadfinder import enrich
    from leadfinder.providers import linkedin as LI
    con, db, ind = _db_with_wheels()
    sent = {}
    docs = {"req_id": "r1", "meta": {}, "documents": [
        {"name": "Vijay Nelson", "current_title": "Sourcing & Procurement Manager", "current_company_name": "Wheels India Limited",
         "location": "Chennai, Tamil Nadu, India", "country_code": "IN", "linkedin_person_id": "vijay-nelson"},
        {"name": "Joe Bloggs", "current_title": "Buyer", "current_company_name": "Wheels Inc", "country_code": "US",
         "linkedin_person_id": "joe"}]}
    LI.requests.post = lambda url, headers=None, json=None, **k: (sent.update(url=url, body=json, h=headers), R(200, docs))[1]
    res = enrich.linkedin_people(con, ind, "C", max_items=50, log=lambda m: None, via="auto")
    assert res["via"] == "brightdata" and res["people_added_or_matched"] == 1 and res["unmatched_profiles"] == 1
    assert sent["url"].endswith("/search/people") and sent["h"]["Authorization"] == "Bearer bd"
    b = sent["body"]
    assert b["mode"] == "ludicrous" and b["source"] == "linkedin_people" and b["view"] == "summary"
    assert {"equals": {"country_code": "IN"}} in b["query"]["and"]
    p = con.execute("SELECT * FROM people").fetchone()
    assert p["linkedin_url"] == "https://www.linkedin.com/in/vijay-nelson" and p["icp"] == "C"
    LI.requests.post = lambda *a, **k: R(403, {"error": "not allowlisted"})
    assert "allowlist" in enrich.linkedin_people(con, ind, "C", log=lambda m: None, via="brightdata")["error"]
    os.environ["APIFY_TOKEN"] = "t"


def test_brightdata_company_posts_tagged_visitors():
    os.environ["BRIGHTDATA_API_KEY"] = "bd"
    from leadfinder import enrich
    from leadfinder.providers import linkedin as LI
    LI_time = __import__("time"); LI_time.sleep = lambda s: None
    con, db, ind = _db_with_wheels()
    calls = []
    def post(url, headers=None, params=None, json=None, **k):
        calls.append((url, params, json)); return R(200, {"snapshot_id": "sd_1"})
    progress = iter([R(200, {"status": "running"}), R(200, {"status": "ready"})])
    snapshot = [{"url": "https://www.linkedin.com/posts/wheels_imtex-1", "user_name": "Wheels India Limited",
                 "account_type": "Company", "post_text": "Thank you for visiting our booth at IMTEX 2025!",
                 "date_posted": "2025-01-30T10:00:00Z",
                 "tagged_people": [{"name": "Ravi Kumar", "link": "https://www.linkedin.com/in/ravi"}]},
                {"url": "https://www.linkedin.com/posts/wheels_x-2", "user_name": "Wheels India Limited",
                 "post_text": "Republic day greetings", "tagged_people": []}]
    def get(url, headers=None, params=None, **k):
        return next(progress) if "/progress/" in url else R(200, snapshot)
    LI.requests.post, LI.requests.get = post, get
    show = con.execute("SELECT * FROM shows WHERE id=1").fetchone()
    res = enrich.company_posts(con, show, log=lambda m: None)
    assert res["posts"] == 2 and res["posts_tagging_people"] == 1 and res["new_candidates"] == 2
    url, params, body = calls[0]
    assert url.endswith("/datasets/v3/trigger") and params["discover_by"] == "company_url"
    assert params["dataset_id"] == "gd_lyy3tktm25m4avu764" and params["type"] == "discover_new"
    from datetime import date, timedelta
    st = date.today() - timedelta(days=20)
    assert body["input"][0] == {"url": "https://www.linkedin.com/company/wheelsindialimited",
                                "start_date": (st - timedelta(days=10)).isoformat(),
                                "end_date": (st + timedelta(days=34)).isoformat()}
    old = con.execute("SELECT * FROM shows WHERE id=2").fetchone()
    assert "only reaches recent" in enrich.company_posts(con, old, log=lambda m: None)["error"]
    c = con.execute("SELECT * FROM post_candidates WHERE title LIKE '%tags people%'").fetchone()
    assert "Ravi Kumar" in c["snippet"]


def test_strict_company_match():
    from leadfinder.normalize import strict_company_match as m
    assert m("Wheels India Limited", "Wheels India") and not m("Wheels, LLC", "Wheels India") and not m("Wheels Inc", "Wheels India")
    assert m("LMW Limited", "LMW Ltd") and m("LMW Machine Tool Division", "LMW Ltd") and not m("Lmwire Corp", "LMW Ltd")
    assert m("Bharat Forge Limited", "Bharat Forge Ltd") and m("BHEL", "BHEL") and m("TVS Motor Company Ltd", "TVS Motor Company")
    assert not m("KSB India", "Bharat Forge Ltd") and m("Titan Company Limited", "Titan Company") and not m("Titan MSP", "Titan Company")
    assert m("Procurement Manager at Airbus", "Airbus India") and m("The Boeing Company", "Boeing India")
    assert not m("Airbusters Ltd", "Airbus India")


def test_linkedin_filters_come_from_persona_not_codes():
    from leadfinder.enrich import linkedin_filters_for
    assert linkedin_filters_for({"code": "P1", "linkedin_filters": {"functions": ["sales"], "seniority": ["vp"]}}) == \
        {"functions": ["sales"], "seniority": ["vp"]}
    f = linkedin_filters_for({"code": "P7", "titles": ["Head of Procurement", "Plant Head"], "title_keywords": ["sourcing"]})
    assert f["functions"] == ["purchasing", "operations"]
    assert linkedin_filters_for({"code": "A", "titles": ["Export Manager"]})["functions"] == ["sales"]


def test_schema_has_no_persona_content():
    import json
    sch = json.loads((Path(__file__).resolve().parent.parent / "templates/icp_schema.json").read_text())
    text = json.dumps(sch).lower()
    for leaked in ("purchase head", "export manager", "national sales head", "key account manager", "procurement manager"):
        assert leaked not in text, leaked


if __name__ == "__main__":
    for k, v in dict(globals()).items():
        if k.startswith("test_"):
            v()
            print("ok", k)
