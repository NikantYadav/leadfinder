"""Firecrawl: exhibitor-list link ranking (edition year), budget guard (reserve, run cap), PDF decoding, fetch skips
error pages, map cache, key pool from repeated .env lines. No network: requests and balances are faked."""
import base64
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TODAY = dt.date(2026, 11, 24)
# real /map answers (2026-11-24), trimmed
ADIPEC = [
    {"url": "https://www.adipec.com/exhibition/exhibitor-list", "title": "ADIPEC 2027 | Exhibitor list"},
    {"url": "https://www.adipec.com/exhibitormanual", "title": "Exhibitor List | ADIPEC 2026"},
    {"url": "https://www.adipec.com/exhibition/exhibitor-products", "title": "ADIPEC 2027 | Exhibitor products and services"},
    {"url": "https://www.adipec.com/2025/exhibition/exhibitor-list", "title": "Exhibitor list - ADIPEC 2025"},
    {"url": "https://www.adipec.com/exhibition/exhibitordetails", "title": "ADIPEC 2026 Exhibitor | Synectics"},
    {"url": "https://exhibitor.adipec.com", "title": "Swapcard"},
    {"url": "https://www.adipec.com/exhibition/book-a-stand", "title": "ADIPEC 2027 | Exhibition | Book a stand"},
    {"url": "https://www.adipec.com/visit", "title": "ADIPEC 2027 | Visit | 1 - 4 November 2027"},
]
PROPAK = [
    {"url": "https://propakindia.com/exhibitor-list", "title": "Exhibitor List 2026"},
    {"url": "https://propakindia.com/media/propakindia.com/Exhibitor-List-29fe3b5cc5d5396268486e4c8e1bc4b6.pdf",
     "title": "Company Name"},
    {"url": "https://propakindia.com/form/bookyourstand", "title": "Book a Stand"},
]


def _setup():
    from leadfinder import config, db
    tmp = Path(tempfile.mkdtemp())
    config.DB_PATH = tmp / "t.db"
    config.ENV_REPEATS = {}
    os.environ["FIRECRAWL_API_KEY"] = "fc-test-key-0001"
    for v in ("LF_FIRECRAWL_AUTO", "LF_FIRECRAWL_CAP", "LF_FIRECRAWL_BUCKET"):
        os.environ.pop(v, None)
    return db.connect()


def test_rank_keeps_this_edition_and_sets_other_years_aside():
    from leadfinder.providers import firecrawl as fc
    ranked, other = fc.rank_list_links(ADIPEC, {"name": "ADIPEC", "year": 2026, "start_date": "2026-11-02"}, TODAY)
    urls = [r["url"] for r in ranked]
    assert urls[0] == "https://exhibitor.adipec.com"        # undated Swapcard directory...
    assert ranked[0]["confirmed"] is False                  # ...but the site already shows 2027: the user decides
    same_year = [l for l in ADIPEC if "2027" not in l["title"] and "2025" not in l["title"]]
    r2, _ = fc.rank_list_links(same_year, {"name": "ADIPEC", "year": 2026, "start_date": "2026-11-02"}, TODAY)
    assert r2[0]["url"] == "https://exhibitor.adipec.com" and r2[0]["confirmed"] is True   # no rollover seen
    assert not any("exhibitordetails" in u for u in urls)  # one exhibitor's page ("... Exhibitor | Synectics")
    assert not any("book-a-stand" in u or "manual" in u or u.endswith("/visit") for u in urls)
    assert {o["url"] for o in other} >= {"https://www.adipec.com/exhibition/exhibitor-list",
                                         "https://www.adipec.com/2025/exhibition/exhibitor-list"}
    assert not set(urls) & {o["url"] for o in other}


def test_rank_old_edition_never_takes_undated_or_other_year_lists():
    from leadfinder.providers import firecrawl as fc
    show = {"name": "ProPak India 2025", "year": 2025, "start_date": "2025-09-03"}   # 15 months ago
    ranked, other = fc.rank_list_links(PROPAK, show, TODAY)
    assert ranked == []                         # the undated PDF is the newer edition's list - not proof for 2025
    assert len(other) == 2 and not any("bookyourstand" in o["url"] for o in other)
    show26 = {"name": "ProPak India 2026", "year": 2026, "start_date": "2026-12-10"}     # upcoming
    ranked, _ = fc.rank_list_links(PROPAK, show26, TODAY)
    assert [r["url"] for r in ranked][0] == "https://propakindia.com/exhibitor-list"
    assert any(r["pdf"] for r in ranked) and all(r["confirmed"] for r in ranked)
    past = {"name": "ProPak India 2026", "year": 2026, "start_date": "2026-08-20"}       # 96 days ago
    ranked, other = fc.rank_list_links(PROPAK, past, TODAY)
    assert [r["url"] for r in ranked] == ["https://propakindia.com/exhibitor-list"]       # dated 2026: still fine
    assert any(o["url"].endswith(".pdf") for o in other)     # undated, months after the show: a later edition's


def test_budget_reserve_for_automatic_use_and_run_cap():
    con = _setup()
    from leadfinder.providers import firecrawl as fc
    live = {"n": 151}
    read = lambda k: live["n"]  # noqa: E731
    assert fc.check_budget(1, con, auto=True, read_live=read) == 151          # 150 left after it: allowed
    con.execute("DELETE FROM provider_state WHERE provider='firecrawl_balance'")
    live["n"] = 150
    try:
        fc.check_budget(1, con, auto=True, read_live=read)
        raise AssertionError("automatic use must keep the reserve")
    except fc.OutOfBudget as e:
        assert "reserve" in str(e)
    assert fc.check_budget(1, con, auto=False, read_live=read) == 150         # the user's own commands may use it
    con.execute("DELETE FROM provider_state WHERE provider='firecrawl_balance'")
    live["n"] = 0
    try:
        fc.check_budget(1, con, auto=False, read_live=read)
        raise AssertionError("nothing left")
    except fc.OutOfBudget:
        pass
    # run cap, counted across processes in credit_usage
    con.execute("DELETE FROM provider_state WHERE provider='firecrawl_balance'")
    live["n"] = 900
    os.environ.update({"LF_FIRECRAWL_CAP": "2", "LF_FIRECRAWL_BUCKET": "firecrawl_run:t:1"})
    fc._note_spend(con, 2)
    assert fc.run_spent(con) == 2
    try:
        fc.check_budget(1, con, auto=True, read_live=read)
        raise AssertionError("run cap")
    except fc.OutOfBudget as e:
        assert "budget of 2" in str(e)
    assert fc.check_budget(1, con, auto=False, read_live=read)                # manual use ignores the run cap
    # local spends lower the cached balance between live reads
    assert fc.balance(con, read_live=read) == 900
    fc._note_spend(con, 2)
    assert fc.balance(con, read_live=read) == 898 and fc.run_spent(con) == 4
    for v in ("LF_FIRECRAWL_CAP", "LF_FIRECRAWL_BUCKET"):
        os.environ.pop(v)


def test_post_forces_basic_proxy_and_counts_real_credits():
    con = _setup()
    from leadfinder import keys
    from leadfinder.providers import firecrawl as fc
    import requests
    sent = []

    class R:
        status_code, ok = 200, True
        def json(self):
            return {"success": True, "data": {"markdown": "x" * 500, "metadata": {"statusCode": 200, "creditsUsed": 3}}}
    orig_post, orig_bal = requests.post, fc.balance
    requests.post = lambda url, json=None, **kw: sent.append((url, json)) or R()
    fc.balance = lambda *a, **k: 1000
    fc.MIN_GAP = 0
    try:
        page = fc.scrape("https://example.com/list", actions=fc.scroll_actions(2), con=con)
    finally:
        requests.post, fc.balance = orig_post, orig_bal
    assert sent[0][1]["proxy"] == "basic" and sent[0][1]["actions"][0]["type"] == "wait"
    assert page["status"] == 200 and len(page["markdown"]) == 500
    k = keys.all_keys("firecrawl")[0]
    assert keys.used(k, dt.datetime.now(dt.timezone.utc).strftime("%Y-%m"), con) == 3


def test_map_is_cached():
    con = _setup()
    from leadfinder.providers import firecrawl as fc
    calls = []
    orig = fc._post
    fc._post = lambda path, body, cost, con=None, auto=None, timeout=0: calls.append(body) or {"links": ADIPEC}
    try:
        a = fc.map_site("https://adipec.com/some/page", "exhibitor", con=con)
        b = fc.map_site("adipec.com", "exhibitor", con=con)
    finally:
        fc._post = orig
    assert len(calls) == 1 and calls[0]["url"] == "https://adipec.com" and a == b == ADIPEC


def test_pdf_decode_and_fetch_skips_firecrawl_after_404():
    con = _setup()
    from leadfinder.providers import fetch, firecrawl as fc
    pdf = b"%PDF-1.4 fake"
    assert fc.decode_pdf(base64.b64encode(pdf).decode()) == pdf
    assert fc.decode_pdf("data:application/pdf;base64," + base64.b64encode(pdf).decode()) == pdf
    try:
        fc.decode_pdf(base64.b64encode(b"<html>").decode())
        raise AssertionError("not a PDF")
    except RuntimeError:
        pass
    called = []

    def gone(url):
        raise RuntimeError("direct: HTTP 404, 120 chars")
    orig = dict(fetch.METHODS)
    fetch.METHODS.update({"direct": gone, "tinyfetch": gone, "openserp": gone,
                          "firecrawl": lambda url: called.append(url) or "x" * 999})
    try:
        r = fetch.fetch(con, "https://example.com/missing")
    finally:
        fetch.METHODS.clear()
        fetch.METHODS.update(orig)
    assert called == [] and "error" in r and any("charges for error pages" in t for t in r["tried"])


def test_repeated_env_lines_join_the_key_pool():
    _setup()
    from leadfinder import config, keys
    config.ENV_REPEATS = {"FIRECRAWL_API_KEY": ["fc-test-key-0001", "fc-second-key-0002"]}
    try:
        assert [k.value for k in keys.all_keys("firecrawl")] == ["fc-test-key-0001", "fc-second-key-0002"]
    finally:
        config.ENV_REPEATS = {}


def test_evidence_maps_thin_shows_reads_this_edition_and_hands_links_on():
    con = _setup()
    from unittest import mock
    from leadfinder import db, pipeline, showhelp
    from leadfinder.providers import firecrawl as fc
    con.execute("INSERT INTO industries(slug,name,geo) VALUES ('t','T','IN')")
    ind = db.get_industry(con, "t")
    con.execute("INSERT INTO shows(industry_id,name,year,start_date,url) VALUES (?,?,?,?,?)",
                (ind["id"], "ADIPEC", 2026, (TODAY - dt.timedelta(days=20)).isoformat(), "https://adipec.com"))
    con.execute("INSERT INTO shows(industry_id,name,year,start_date,url,origin) VALUES (?,?,?,?,?,?)",
                (ind["id"], "Other fair", 2026, TODAY.isoformat(), "https://other.example", "evidence"))
    pipeline._stage_set(con, ind["id"], "evidence", "running")
    con.commit()
    read = []
    with mock.patch.object(fc, "map_site", lambda url, search=None, **k: ADIPEC), \
            mock.patch.object(fc, "current_edition", lambda show, today=None: True), \
            mock.patch.object(showhelp, "read_url", lambda con, ind, show, url, **k: read.append((url, k["apify"])) or {"added": 0}):
        r = pipeline._firecrawl_lists(con, ind, {})
    assert r["firecrawl_maps"] == 1 and list(r["firecrawl_lists"]) == ["ADIPEC"]        # evidence-origin show left out
    assert read == []                      # Swapcard link's edition is unconfirmed (site shows 2027): not imported
    assert r["list_links"] == {"ADIPEC": ["https://exhibitor.adipec.com"]}    # offered to the user instead
    assert "later edition" in r["firecrawl_lists"]["ADIPEC"]
    con.execute("INSERT INTO shows(industry_id,name,year,start_date,url) VALUES (?,?,?,?,?)",
                (ind["id"], "Fair B", 2026, (TODAY + dt.timedelta(days=60)).isoformat(), "https://fairb.example"))
    con.commit()
    fairb = [{"url": "https://fairb.mapyourshow.com/8_0/explore/exhibitor-gallery.cfm", "title": "Exhibitor List"}]
    with mock.patch.object(fc, "map_site", lambda url, search=None, **k: fairb), \
            mock.patch.object(showhelp, "read_url", lambda con, ind, show, url, **k: read.append((url, k["apify"])) or {"added": 40, "via": "apify:tidytools"}):
        r = pipeline._firecrawl_lists(con, ind, {})
    assert read == [(fairb[0]["url"], True)] and "40 exhibitors" in r["firecrawl_lists"]["Fair B"]
    with mock.patch.object(fc, "map_site", lambda *a, **k: (_ for _ in ()).throw(AssertionError("mapped twice"))):
        assert pipeline._firecrawl_lists(con, ind, {}) == {}     # progress kept: a resumed stage does not map again


if __name__ == "__main__":
    for k, v in dict(globals()).items():
        if k.startswith("test_"):
            v()
            print("ok", k)
