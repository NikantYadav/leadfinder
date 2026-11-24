"""Show posts (showposts.py) and exhibitors' customers (customers.py), against real output saved 2026-11-24."""
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"
IMTEX = {"id": 2, "name": "IMTEX 2025", "year": 2025, "series": "IMTEX", "start_date": "2025-01-23",
         "end_date": "2025-01-29"}
FORMING = {"id": 5, "name": "IMTEX FORMING 2026", "year": 2026, "series": "IMTEX FORMING", "start_date": "2026-01-21",
           "end_date": "2026-01-25"}


def _icps():
    return json.loads((FIX / "icp_example.json").read_text())["icps"]


def _rows():
    return json.loads((FIX / "apify_posts_imtex2025.json").read_text())


def _setup():
    from leadfinder import config, db
    tmp = Path(tempfile.mkdtemp())
    config.DB_PATH, config.OUTPUT = tmp / "t.db", tmp
    con = db.connect()
    con.execute("INSERT INTO industries(slug,name,geo,persona_text,icp_json,icp_approved) VALUES ('p','P','IN','x',?,1)",
                ((FIX / "icp_example.json").read_text(),))
    for s in (IMTEX, FORMING):
        con.execute("INSERT INTO shows(id,industry_id,name,year,series,start_date,end_date) VALUES (?,1,?,?,?,?,?)",
                    (s["id"], s["name"], s["year"], s["series"], s["start_date"], s["end_date"]))
    con.commit()
    return con, db.get_industry(con, "p")


def test_split_headline():
    from leadfinder.showposts import split_headline as sh
    assert sh("Deputy Plant Manager at Taural India Private Limited") == ("Deputy Plant Manager",
                                                                          "Taural India Private Limited")
    assert sh("SUPPLY CHAIN @ ALLCON METALS | STAINLESS STEEL") == ("SUPPLY CHAIN", "ALLCON METALS")
    assert sh("Sales Director - Deep Pneumatics Pvt. Ltd") == ("Sales Director", "Deep Pneumatics Pvt. Ltd")
    assert sh("Manager - Domestic Sales") == ("Manager - Domestic Sales", None)   # no legal suffix: no company
    assert sh("8,113 followers") == (None, None) and sh("") == (None, None)
    title, _ = sh("Sandvik Coromant - Key Account Manager, Manufacturing , Operations Management, B2B Sales , Key "
                  "Account Management , Channel Partner Development")
    assert len(title) <= 90


def test_queries_and_multi_edition_assignment():
    from datetime import date
    from leadfinder import showposts as sp
    assert sp.queries(IMTEX) == ['"IMTEX 2025"', "#IMTEX2025", "#IMTEX"]
    # a Jan 2026 #IMTEX post belongs to IMTEX FORMING 2026, not to the IMTEX 2025 search that found it
    assert sp.show_for(date(2026, 1, 22), "Day 2 at #IMTEX", [IMTEX, FORMING])["id"] == 5
    assert sp.show_for(date(2025, 1, 25), "Bichamp shines at IMTEX 2025", [IMTEX, FORMING])["id"] == 2
    assert sp.show_for(date(2025, 8, 1), "IMTEX 2025 recap", [IMTEX, FORMING]) is None      # outside both windows
    assert sp.show_for(date(2025, 1, 25), "a post about something else", [IMTEX, FORMING]) is None
    oil = {"id": 9, "name": "Oil & Gas Connect Expo", "start_date": "2026-03-12"}
    assert not sp.mentions(oil, "oil prices rose") and sp.mentions(oil, "#OilGasConnectExpo was great")


def test_persona_fit_is_whole_words():
    from leadfinder.showposts import _fits
    icps = _icps()
    assert _fits("Head of Sourcing", icps) and _fits("SUPPLY CHAIN", icps)
    assert not _fits("Varad Engineering makes outsourcing a smarter choice", icps)
    assert not _fits("Sales Intern", icps)   # excluded keyword


def test_classify_real_posts_and_decide_with_keyword_fallback():
    from leadfinder import showposts as sp
    rows, icps = _rows(), _icps()
    exhibitors = {2: [(5, "Bichamp Cutting Technology")], 5: []}
    posts, authors = sp.classify_posts(rows, [IMTEX, FORMING], exhibitors)
    assert {p["show_id"] for p in posts} == {2} and len(posts) == 5    # Dec 2025 / Jul 2026 posts dropped
    bichamp = next(p for p in posts if p["company"] == "Bichamp Cutting Technology")
    assert bichamp["kind"] == "company" and bichamp["exhibitor"] and bichamp["comments"] == 2
    cands = sp.classify_engagers([r for r in rows if r["type"] != "post"], posts)
    assert not any(c["name"].startswith("Span Filtration") for c in cands)   # company page reacting
    eng = sp.decide(cands, {}, exhibitors, icps)          # no Sonnet answers: keyword + wording fallback
    subash = [e for e in eng if e["name"] == "Subash chandra Behera"]
    # "It was a nice interaction with you... Expecting a quick trial": a visit comment; headline 'Manager' says
    # nothing, so the profile must be read before it can count
    assert len(subash) == 1 and subash[0]["kind"] == "visit_comment" and subash[0]["needs_profile"]
    dhanesh = next(e for e in eng if e["name"].startswith("Dhanesh"))
    assert dhanesh["kind"] == "post_engaged" and dhanesh["company"] == "ALLCON METALS" and dhanesh["icp"] == "C"
    assert not any(e["name"] == "Suman Pai" for e in eng)       # social media manager: no persona
    # generic praise is engagement, not a visit
    fake = [{"type": "comment", "commentary": c, "query": {"post": bichamp["url"]},
             "actor": {"name": f"P{i}", "position": "Purchase Manager at Tata Motors", "linkedinUrl": f"u{i}"}}
            for i, c in enumerate(["Great to see this, congrats team!", "Nice connecting with you",
                                   "Great discussion", "Nice meeting you at the stall"])]
    got = sp.decide(sp.classify_engagers(fake, posts), {}, exhibitors, icps)
    assert [e["kind"] for e in got] == ["post_engaged", "post_engaged", "post_engaged", "visit_comment"]
    assert not sp.EXHIBIT_WORDS.search("Explored Hall 3 today") and sp.EXHIBIT_WORDS.search("Hall 5, Booth B152")
    # the profile read gives the real job; it is judged again: a buyer stays a visit, a seller at the exhibitor doesn't
    from leadfinder.providers.linkedin import _slug
    slug = _slug(subash[0]["linkedin_url"]).lower()
    again = sp.profile_candidates(eng, {slug: {"title": "Purchase Manager", "company": "Tata Motors"}})
    s = sp.decide(again, {}, exhibitors, icps)[0]
    assert s["kind"] == "visit_comment" and s["title"] == "Purchase Manager" and s["icp"] == "C"
    again = sp.profile_candidates(eng, {slug: {"title": "Sales Manager", "company": "Bichamp Cutting Technology"}})
    assert sp.decide(again, {}, exhibitors, icps)[0]["kind"] == "post_engaged"
    assert sp.profile_candidates(eng, {}) == []


def test_sonnet_verdicts_decide_group_and_visit():
    from leadfinder import judge, showposts as sp
    con, ind = _setup()
    rows, icps = _rows(), _icps()
    exhibitors = {2: [(5, "Bichamp Cutting Technology")], 5: []}
    posts, _ = sp.classify_posts(rows, [IMTEX, FORMING], exhibitors)
    cands = sp.classify_engagers([r for r in rows if r["type"] != "post"], posts)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        n = prompt.count("\n") and len([l for l in prompt.split("PEOPLE:")[1].splitlines() if l.strip()])
        out = []
        for i in range(1, n + 1):   # everyone C, the Taural plant manager included; nobody 'visited'
            out.append({"n": i, "group": "C", "role": "Deputy Plant Manager", "company": None, "at_show": "visited"})
        return "Here you go: " + json.dumps(out)
    v, how = judge.judge(con, ind, icps, cands, ask=ask)
    assert how == "sonnet" and len(asked) == 1 and "Procurement teams at events" in asked[0]
    # reactions have no words: 'visited' from the model is overruled
    assert all(v[judge.key(c)]["at_show"] == "none" for c in cands if c["src"] == "reaction")
    people = sp.decide(cands, v, exhibitors, icps)
    shankar = next(p for p in people if p["name"] == "Shankar Thombare")
    assert shankar["icp"] == "C" and shankar["kind"] == "post_engaged"   # keywords alone would have dropped him
    v2, how2 = judge.judge(con, ind, icps, cands, ask=lambda p: (_ for _ in ()).throw(AssertionError("asked")))
    assert how2 == "cache" and v2 == v
    # an answer that is cut off or not JSON -> the caller's keyword fallback, reported
    con2, ind2 = _setup()
    v3, how3 = judge.judge(con2, ind2, icps, cands, ask=lambda p: "sorry")
    assert v3 == {} and how3 == f"fallback:{len(cands)}"


def test_upsert_person_url_match_without_company_does_not_clash():
    from leadfinder import db
    con, _ = _setup()
    cid = db.upsert_company(con, "Allcon Metals")
    loose = db.upsert_person(con, None, "Dhanesh K", "Supply Chain", linkedin_url="https://www.linkedin.com/in/dk")
    at_co = db.upsert_person(con, cid, "Dhanesh K", "Supply chain head")
    # found by URL (no company) while the same name already exists at the company: the company record is used
    assert db.upsert_person(con, cid, "Dhanesh K", "Supply Chain", linkedin_url="https://www.linkedin.com/in/dk") \
        == at_co != loose


def test_pick_posts_budget_and_order():
    from leadfinder.showposts import pick_posts
    posts = [{"url": "a", "exhibitor": False, "text": "", "likes": 100, "comments": 0},
             {"url": "b", "exhibitor": True, "text": "thank you for visiting", "likes": 5, "comments": 2},
             {"url": "c", "exhibitor": False, "text": "", "likes": 0, "comments": 0}]
    c, r, est = pick_posts(posts, 0.2)
    assert c == ["b"] and r == ["b", "a"] and est == round((7 + 25) * 0.002, 3)   # 'c' has nothing to read
    c, r, _ = pick_posts(posts, 0.02)
    assert c == ["b"] and r == ["b"]


def test_read_show_ingests_caches_and_builds_leads():
    from leadfinder import db, showposts as sp
    from leadfinder.export import at_show_label, build_leads
    con, ind = _setup()
    cid = db.upsert_company(con, "Bichamp Cutting Technology", industry_id=1)
    db.add_evidence(con, "company", cid, "exhibitor", "confirmed", "https://list", None, 2)
    con.commit()
    rows = _rows()
    posts = [r for r in rows if r["type"] == "post"]
    inline = [r for r in rows if r["type"] != "post"]
    calls = []

    def fake_run(actor, body, cap, deadline, log, ref=None, on_start=None):
        calls.append((actor, body))
        assert cap >= 0.0
        if actor == sp.SEARCH:
            out = posts
        else:
            want = "comment" if actor == sp.COMMENTS else "reaction"
            out = [dict(r, type=None) for r in inline if r["type"] == want and r["query"]["post"] in body["posts"]]
        return out, {"usageTotalUsd": 0.05, "status": "SUCCEEDED"}, {"actor": actor, "run_id": "r", "key": "k"}
    profile = {"subash-chandra-behera-1153306b": {"title": "Head of Procurement", "company": "Tata Motors"}}
    with mock.patch.object(sp.ev, "run_actor", fake_run), \
            mock.patch("leadfinder.pipeline._claude_text", lambda *a, **k: None), \
            mock.patch("leadfinder.providers.linkedin.check_profiles", lambda urls, con=None, log=None: profile):
        res = sp.read_show(con, ind, IMTEX, 0.8, log=lambda m: None)
        assert [c[0] for c in calls] == [sp.SEARCH, sp.COMMENTS, sp.REACTIONS]
        assert calls[0][1]["sortBy"] == "relevance" and calls[0][1]["postedLimitDate"] == "2024-11-24"
        assert res["seen"] >= 1 and res["engaged"] >= 1 and res["exhibitor_posts"] >= 1
        assert res["persona_check"].startswith("fallback:")     # Sonnet unreachable here: keywords decided
        again = sp.read_show(con, ind, IMTEX, 0.8, log=lambda m: None)
    assert len(calls) == 3 and again["cost"] == 0            # everything came from the cache
    kinds = {r[0] for r in con.execute("SELECT kind FROM evidence WHERE entity='person'")}
    assert kinds == {"visit_comment", "post_engaged"}
    leads = {l["name"]: l for l in build_leads(con, db.get_industry(con, "p"))}
    sub = leads["Subash chandra Behera"]
    assert sub["icp"] == "C" and sub["seen"] and at_show_label(sub) == "seen at a show"
    dh = next(l for n, l in leads.items() if n.startswith("Dhanesh"))
    # liked / commented only: passes the buyer group's 'visitor' rule, but is not 'seen'
    assert dh["icp"] == "C" and not dh["seen"] and dh["engaged"] and at_show_label(dh) == "engaged with a show post"
    # an engaged seller at a company that exhibits nowhere does not pass group A's 'exhibits at N+ shows' rule
    assert all(l["icp"] != "A" or l["seen"] or l["company"] == "Bichamp Cutting Technology" for l in leads.values())
    # engaged people's companies stay out of the search's company pool; seen people's join it
    pool = {r[0] for r in con.execute("SELECT c.name FROM companies c JOIN company_industries ci ON "
                                      "ci.company_id=c.id WHERE ci.industry_id=1")}
    assert "ALLCON METALS" not in pool and "Tata Motors" in pool


def test_attendee_stage_runs_post_search_not_sherwood_by_default():
    import time as _time
    from leadfinder import pipeline, showposts
    con, ind = _setup()
    con.execute("UPDATE shows SET start_date='2026-09-01', end_date='2026-09-04' WHERE id=5")
    con.commit()
    seen = []
    with mock.patch("leadfinder.providers.events.available", lambda: True), \
            mock.patch.object(showposts, "read_show", lambda con, ind, s, usd, *a, **k: seen.append((s["name"], usd))
                              or {"posts_in_window": 3, "seen": 1, "engaged": 2, "cost": 0.4}), \
            mock.patch.object(pipeline, "_sherwood", lambda *a: (_ for _ in ()).throw(AssertionError("sherwood"))), \
            mock.patch.object(pipeline, "recent_shows", _pinned(pipeline.recent_shows)), \
            mock.patch.object(pipeline.time, "strftime", lambda f, *a: "2026-11-24" if f == "%Y-%m-%d"
                              else _time.strftime(f, *a)):
        out = pipeline.stage_attendees(con, ind, {})
    # ended shows first (closest first); IMTEX 2025 is within the 2-year reach of the relevance-sorted search
    assert [n for n, _ in seen] == ["IMTEX FORMING 2026", "IMTEX 2025"] and seen[0][1] == pipeline.POST_SHOW_USD
    assert "1 people seen at a show" in out["shows"]["IMTEX 2025"] and out["cost_usd"] == 0.8


def _pinned(fn):
    from datetime import date
    return lambda con, ind, today=None, **k: fn(con, ind, today=date(2026, 11, 24), **k)


# ---------- exhibitors' customers ----------
HOME = """<a href="/about">About</a><a href="/clientele">Our Clients</a><a href="/customer-support">Support</a>
<a href="https://other.com/clients">x</a><a href="/case-studies/">Case studies</a>"""
CLIENTS = """<h1>Our valued clients</h1><img src="/img/logos/bharat-forge-logo.png" alt="Bharat Forge">
<img src="/img/logos/tata_motors_250x120.png"><img alt="logo 3" src="/x/3.png"><p>Ace Micromatic is proud.</p>"""


def test_client_links_and_page_text():
    from leadfinder import customers
    links = customers.client_links("https://www.ace.com/", HOME)
    assert links == ["https://www.ace.com/clientele", "https://www.ace.com/case-studies"]   # support / other site out
    t = customers.page_text(CLIENTS)
    assert "Bharat Forge" in t and "tata motors" in t and "Our valued clients" in t
    assert customers.clean_names(["Bharat Forge", "Tata Motors", "Ace Micromatic", "logo 3", "Invented Corp"],
                                 "Ace Micromatic Group", t) == ["Bharat Forge", "Tata Motors"]


def test_customers_run_adds_buyer_companies_once():
    from leadfinder import customers, db, pipeline
    from leadfinder.people import classify, company_facts, companies_for_icp, load_icps
    con, ind = _setup()
    ace = db.upsert_company(con, "Ace Micromatic Group", "acemicromatic.net", industry_id=1)
    db.add_evidence(con, "company", ace, "exhibitor", "confirmed", "https://list", None, 2)
    seen_co = db.upsert_company(con, "Visiting Co", industry_id=1)
    db.add_evidence(con, "company", seen_co, "visitor_post", "inferred", "https://post", None, 2)
    con.commit()
    pages = {"https://acemicromatic.net": ("https://www.acemicromatic.net/", HOME),
             "https://www.acemicromatic.net/clientele": ("https://www.acemicromatic.net/clientele", CLIENTS)}
    got = []
    with mock.patch.object(customers, "_get", lambda url, timeout=25: pages[url]), \
            mock.patch.object(customers, "extract", lambda batch, model="sonnet": got.append(batch) or
                              {b["id"]: ["Bharat Forge", "Tata Motors", "Made Up Ltd"] for b in batch}):
        res = customers.run(con, ind, 5, log=lambda m: None, allow_map=False)
        assert res["customers_added"] == 2 and res["client_pages_found"] == 1 and len(got) == 1
        again = customers.run(con, ind, 5, log=lambda m: None, allow_map=False)
    assert len(got) == 1 and again["from_cache"] == 1          # the site is never read twice
    bf = con.execute("SELECT id FROM companies WHERE name='Bharat Forge'").fetchone()["id"]
    ev = con.execute("SELECT kind, confidence, url, show_id FROM evidence WHERE entity_id=? AND entity='company'",
                     (bf,)).fetchone()
    assert tuple(ev) == ("customer_of_exhibitor", "inferred", "https://www.acemicromatic.net/clientele", None)
    icps = load_icps(db.get_industry(con, "p"))
    assert classify(con, icps, "Head of Procurement", bf) == "C"            # buyer group's 'visitor' rule
    assert classify(con, icps, "Business Development Manager", bf, min_shows=1) is None   # sellers need shows
    # a company seen at a show is searched before one only named as a customer
    c_icp = next(i for i in icps if i["code"] == "C")
    order = [c["name"] for c in companies_for_icp(con, db.get_industry(con, "p"), c_icp)[0]]
    assert order.index("Visiting Co") < order.index("Bharat Forge")
    assert company_facts(con, bf)["direct_visits"] == 0
    # personas that only want sellers skip the step
    doc = json.loads(ind["icp_json"])
    doc["icps"] = [i for i in doc["icps"] if i["code"] == "A"]
    con.execute("UPDATE industries SET icp_json=? WHERE id=1", (json.dumps(doc),))
    con.commit()
    assert "skipped" in pipeline.stage_customers(con, db.get_industry(con, "p"), {})


if __name__ == "__main__":
    for k, v in dict(globals()).items():
        if k.startswith("test_"):
            v()
            print("ok", k)
