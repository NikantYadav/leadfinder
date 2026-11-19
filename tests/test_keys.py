"""Key pools, per-search targeting, stop/resume. No network. Run: ./env/bin/python tests/test_keys.py"""
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from leadfinder import config, db  # noqa: E402

tmp = Path(tempfile.mkdtemp())
config.DB_PATH, config.OUTPUT = tmp / "t.db", tmp

from leadfinder import keys, pipeline, targeting  # noqa: E402
from leadfinder.providers import email as E  # noqa: E402
from leadfinder.providers import search as S  # noqa: E402
from leadfinder.providers.search import ProviderBusy  # noqa: E402


class R:
    def __init__(self, status, body):
        self.status_code, self._b, self.ok, self.text = status, body, 200 <= status < 300, str(body)

    def json(self):
        return self._b


def _reset_env():
    for k in list(os.environ):
        if any(k.startswith(v) for v, _ in keys.SPECS.values()) or k.startswith("TOMBA_API_SECRET"):
            del os.environ[k]


def test_parse_comma_and_numbered_keys_without_leaking():
    _reset_env()
    os.environ.update({"SERPER_API_KEY": "aaaa1111, bbbb2222", "SERPER_API_KEY_2": "cccc3333",
                       "SERPER_API_KEY_3": "aaaa1111"})  # duplicate is dropped
    ks = keys.all_keys("serper")
    assert [k.fp for k in ks] == ["…1111", "…2222", "…3333"]
    assert "aaaa" not in repr(ks)


def test_paired_secrets():
    _reset_env()
    os.environ.update({"TOMBA_API_KEY": "key-one-1,key-two-2", "TOMBA_API_SECRET": "sec1,sec2"})
    assert [(k.value, k.secret) for k in keys.all_keys("tomba")] == [("key-one-1", "sec1"), ("key-two-2", "sec2")]
    os.environ["TOMBA_API_SECRET"] = "shared"
    assert {k.secret for k in keys.all_keys("tomba")} == {"shared"}


def test_busy_key_rotates_to_next_and_rests():
    _reset_env()
    os.environ["PROSPEO_API_KEY"] = "first-key-AAAA,second-key-BBBB"
    seen = []

    def post(url, headers=None, **k):
        seen.append(headers["X-KEY"])
        if headers["X-KEY"].endswith("AAAA"):
            return R(200, {"error": True, "error_code": "INSUFFICIENT_CREDITS"})
        return R(200, {"error": False, "person": {"email": {"email": "a@b.com", "status": "VERIFIED"}}, "company": {}})
    E.requests.post = post
    p = E.Prospeo()
    res = keys.run("prospeo", lambda k: p.find({"name": "A B"}, k))
    assert res["email"] == "a@b.com" and seen == ["first-key-AAAA", "second-key-BBBB"]
    # first key now rests for a day (quota), second is still usable
    assert [k.fp for k in keys.usable("prospeo")] == ["…BBBB"]
    seen.clear()
    keys.run("prospeo", lambda k: p.find({"name": "A B"}, k))
    assert seen == ["second-key-BBBB"]


def test_rate_limit_rests_briefly_quota_rests_long():
    _reset_env()
    os.environ["SERPER_API_KEY"] = "kkkk0001,kkkk0002"
    k1, k2 = keys.all_keys("serper")
    keys.cool(k1, ProviderBusy("HTTP 429"))
    keys.cool(k2, ProviderBusy("HTTP 402"))
    assert keys.cooling_until(k1) < keys.cooling_until(k2)
    try:
        keys.run("serper", lambda k: 1)
        raise AssertionError("expected ProviderBusy")
    except ProviderBusy as e:
        assert "resting" in str(e)


def test_router_uses_next_key_and_counts_it():
    _reset_env()
    os.environ["SERPER_API_KEY"] = "srch-key-0001,srch-key-0002"
    calls = []

    def post(url, json=None, headers=None, **k):
        calls.append(headers["X-API-KEY"])
        if headers["X-API-KEY"].endswith("0001"):
            return R(402, {})
        return R(200, {"organic": [{"title": "t", "link": "https://x.com", "snippet": ""}]})
    S.requests.post = post
    con = db.connect()
    orig = S.PROVIDERS
    S.PROVIDERS = [S.Serper()]
    try:
        res, prov = S.search(con, "q unique 1", use_cache=False)
    finally:
        S.PROVIDERS = orig
    assert prov == "serper" and len(res) == 1 and calls == ["srch-key-0001", "srch-key-0002"]
    k2 = keys.all_keys("serper")[1]
    assert keys.used(k2, __import__("datetime").datetime.utcnow().strftime("%Y-%m")) == 1


def _ind(con, slug, target=None, geo="India"):
    con.execute("INSERT INTO industries(slug,name,geo,persona_text,options_json,icp_json,icp_approved) VALUES (?,?,?,?,?,?,1)",
                (slug, slug, geo, "buyers", json.dumps({"target": target or {}}),
                 Path(__file__).resolve().parent.joinpath("fixtures/icp_example.json").read_text()))
    con.commit()
    return db.get_industry(con, slug)


def test_targeting_enforced_and_geo_per_search():
    con = db.connect()
    t = targeting.clean({"countries": "UAE, India", "cities": ["Dubai"], "seniority": "Head, Plant Director, wizard",
                         "functions": "Procurement", "exclude_titles": "intern",
                         "company_rule": "companies that visit shows to buy", "headcount": "50-500"})
    assert t["seniority"] == ["Head", "Plant Director", "wizard"]  # free text kept as typed
    assert targeting.headcount_codes(t["headcount"]) == ["C", "D", "E"]
    assert targeting.headcount_codes(["1000+"]) == ["F", "G", "H", "I"]
    assert targeting.rule_from_text("exhibits at 3+ shows a year") == "exhibitor_3plus"
    assert targeting.rule_from_text("no trade show requirement") == "any"
    assert targeting.rule_from_text("met them at a conference") is None   # left to Claude
    ind = _ind(con, "tgt", t)
    doc = targeting.apply(json.loads(ind["icp_json"]), ind)
    assert doc["hubs"] == ["Dubai"]
    for icp in doc["icps"]:
        assert icp["linkedin_filters"]["seniority"] == ["director"]  # "Head"/"Director" mapped; "wizard" -> Claude
        assert icp["linkedin_filters"]["functions"] == ["purchasing"]
        assert icp["company_rule"] == "visitor" and "intern" in icp["exclude_keywords"]
    tc = targeting.target_countries(ind)
    assert {"uae", "ae", "united arab emirates", "india", "in"} <= tc
    assert targeting.region_code(ind) == "AE" and targeting.linkedin_locations(ind) == ["Dubai"]
    assert "HARD CONSTRAINTS" in targeting.prompt_block(ind) and "wizard" in targeting.prompt_block(ind)
    old = _ind(con, "old", None, geo="India + export markets")  # pre-targeting searches still work
    assert targeting.region_code(old) == "IN"


def test_rerun_from():
    assert targeting.rerun_from({"countries": ["India"]}, {"countries": ["India"]}) is None
    assert targeting.rerun_from({}, {"cities": ["Pune"]}) == "linkedin"
    assert targeting.rerun_from({"cities": ["Pune"]}, {"cities": ["Pune"], "seniority": ["vp"]}) == "persona"


def test_stop_marks_stage_and_resume_continues():
    con = db.connect()
    ind = _ind(con, "stopme")
    calls = []
    orig = dict(pipeline.RUNNERS)

    def research(con, ind, opts):
        calls.append("research")
        return {"shows": 1, "companies": 1}

    def linkedin(con, ind, opts):
        calls.append("linkedin")
        if calls.count("linkedin") == 1:
            pipeline._save_progress(con, ind["id"], "linkedin", {"P1": {"people_added_or_matched": 3}})
            os.kill(os.getpid(), signal.SIGTERM)  # the Stop button
        prev = pipeline._progress(con, ind["id"], "linkedin")
        return {"resumed_with": sorted(prev)}
    try:
        pipeline.RUNNERS.update({"persona": lambda *a: {"icps": ["P1"]}, "research": research, "linkedin": linkedin,
                                 "web": lambda *a: {"done": 1}, "verify": lambda *a: {"checked": 0},
                                 "emails": lambda *a: {"found": 0}, "export": lambda *a: {"leads": 1}})
        code = pipeline.run("stopme", {})
        assert code == 130
        st = {r["stage"]: r["status"] for r in con.execute("SELECT stage, status FROM pipeline_stages WHERE industry_id=?",
                                                           (ind["id"],))}
        assert st["research"] == "done" and st["linkedin"] == "stopped" and "web" not in st
        code = pipeline.run("stopme", {})  # Resume
        assert code == 0
        assert calls == ["research", "linkedin", "linkedin"]  # research not repeated
        d = json.loads(con.execute("SELECT detail FROM pipeline_stages WHERE industry_id=? AND stage='linkedin'",
                                   (ind["id"],)).fetchone()["detail"])
        assert d["resumed_with"] == ["P1"]  # the finished persona's progress was available to skip it
    finally:
        pipeline.RUNNERS.clear()
        pipeline.RUNNERS.update(orig)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)



def test_skip_pending_and_running_steps():
    con = db.connect()
    ind = _ind(con, "skipme")
    calls = []
    orig = dict(pipeline.RUNNERS)

    def research(con, ind, opts):
        calls.append("research")
        # the user presses Skip on this running step, and also skips a later one
        con.execute("UPDATE pipeline_stages SET status='skipping' WHERE industry_id=? AND stage='research'", (ind["id"],))
        con.execute("INSERT INTO pipeline_stages(industry_id, stage, status) VALUES (?, 'verify', 'skipped')", (ind["id"],))
        con.commit()
        os.kill(os.getpid(), signal.SIGTERM)

    def rec(name):
        return lambda *a: calls.append(name) or {"done": 1, "leads": 1, "found": 0}
    try:
        pipeline.RUNNERS.update({"persona": lambda *a: {"icps": ["P1"]}, "research": research,
                                 "linkedin": rec("linkedin"), "web": rec("web"), "verify": rec("verify"),
                                 "emails": rec("emails"), "export": rec("export")})
        assert pipeline.run("skipme", {}) == 0
        assert calls == ["research", "linkedin", "web", "emails", "export"]
        st = {r["stage"]: r["status"] for r in con.execute("SELECT stage, status FROM pipeline_stages WHERE industry_id=?",
                                                           (ind["id"],))}
        assert st["research"] == "skipped" and st["verify"] == "skipped"
        calls.clear()
        pipeline.run("skipme", {}, from_stage="linkedin")  # an edit & re-run keeps the user's skips
        assert "verify" not in calls and "linkedin" in calls
    finally:
        pipeline.RUNNERS.clear()
        pipeline.RUNNERS.update(orig)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
