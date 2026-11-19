"""Local web interface for the Lead Finder: `./lf ui` -> http://127.0.0.1:8787

- Binds to 127.0.0.1 only (your API keys live on this machine); rejects requests whose Host isn't localhost
  (DNS-rebinding guard) and state-changing requests without the X-Requested-With header (CSRF guard).
- Every action runs as a background job: either a whitelisted `./lf` command, or a headless Claude Code run
  (`claude -p`) restricted to `./lf`, reading files, writing under data/work/ and web search.
"""
import json
import os
import signal
import subprocess
import threading
from datetime import datetime, timezone

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from . import config, db
from .export import build_leads, path_for
from .people import company_facts, company_rule_ok, load_icps

ROOT = config.ROOT
STATIC = ROOT / "leadfinder" / "web_static"
JOBS_DIR = config.DATA / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=None)

# ---------------- jobs ----------------
JOB_SCHEMA = """CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, kind TEXT, label TEXT, industry TEXT, cmd TEXT, status TEXT, pid INTEGER,
  started_at TEXT, ended_at TEXT, exit_code INTEGER, log_path TEXT)"""

CLAUDE_TOOLS = ["Bash(./lf:*)", "Bash(docker start openserp)", "Bash(docker ps:*)", "Read", "Glob", "Grep",
                "Write(data/work/**)", "Edit(data/work/**)", "WebSearch", "Skill"]

HEADLESS_NOTE = ("You are running headless from the Lead Finder web UI: there is no chat user to answer questions. "
                 "Never run `./lf capture` (it needs a human at the browser); list such pages in your final summary "
                 "instead. Keep your final message to a short summary of what you did, counts, and what is blocked.")


def _con():
    con = db.connect()
    con.execute(JOB_SCHEMA)
    return con


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lf(*args):
    return [str(ROOT / "lf"), *[str(a) for a in args]]


def _claude(prompt):
    from .pipeline import DEFAULT_EFFORT, DEFAULT_MODEL
    return ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--allowedTools", *CLAUDE_TOOLS,
            "--model", DEFAULT_MODEL, "--effort", DEFAULT_EFFORT]


def build_command(action, p, slug):
    """Whitelisted actions only -> argv list (never a shell string)."""
    i = ["--industry", slug] if slug else []
    num = lambda k, d: int(p.get(k) or d)  # noqa: E731
    if action == "plan_people":
        return _lf("plan-people", *i, *(["--icp", p["icp"]] if p.get("icp") else [])), "Plan people searches"
    if action == "run_queue":
        return _lf("run-queue", "--max", num("max", 40)), "Run search queue"
    if action == "linkedin_people":
        return _lf("linkedin-people", *i, "--icp", p["icp"], "--max", num("max", 150),
                   *(["--via", p["via"]] if p.get("via") in ("apify", "brightdata") else [])), \
            f"LinkedIn people search (ICP {p['icp']})"
    if action == "company_posts":
        return _lf("company-posts", "--show", num("show", 0), "--companies", num("companies", 15)), \
            f"Exhibitor posts for show {p['show']}"
    if action == "posts_api":
        return _lf("posts-api", "--show", num("show", 0), "--query", *p["query"].split("|"), "--max", num("max", 50)), \
            f"LinkedIn post search for show {p['show']}"
    if action == "verify_profiles":
        return _lf("verify-profiles", *i, "--top", num("top", 50)), "Verify LinkedIn profiles"
    if action == "resolve_domains":
        return _lf("resolve-domains", *i), "Find company websites"
    if action == "enrich_emails":
        return _lf("enrich-emails", *i, "--top", num("top", 30), *(["--include-flagged"] if p.get("include_flagged") else [])), \
            "Find emails"
    if action == "export":
        return _lf("export", *i), "Export Excel"
    if action == "pipeline":
        args = ["pipeline", *i]
        if p.get("from_stage"):
            args += ["--from-stage", p["from_stage"]]
        return _lf(*args), "Find people (full pipeline)"
    if action == "claude_personas":
        return _claude(f'/find-leads {p["name"]} --geo "{p.get("geo") or "India + export markets"}"\n\n{HEADLESS_NOTE} '
                       "Do Step 0 and Step 1 up to and including `./lf set-icp`. Do NOT run `./lf approve-icp`: the "
                       "user reviews and approves the personas in the web UI."), f"Claude: draft personas for {p['name']}"
    if action == "claude_research":
        return _claude(f"/find-leads (continue the existing industry with slug '{slug}'; its personas are approved)\n\n"
                       f"{HEADLESS_NOTE} Do Step 2 (shows), Step 3a (exhibitors), Step 3b (visitors and buyers), "
                       "Step 4 (`./lf plan-people` then `./lf run-queue --max 40`), Step 4a (`./lf linkedin-people` "
                       "once per ICP code in the personas with --max 25) and "
                       "Step 6 (`./lf export`). Do NOT run Step 4b email enrichment or `./lf company-posts`: the user "
                       "starts those from the web UI because they spend credits."), f"Claude: research {slug}"
    if action == "claude_posts":
        return _claude(f"Industry slug '{slug}'. {HEADLESS_NOTE} Run `./lf post-candidates --show {num('show', 0)}`. "
                       "For each candidate decide whether the author or a TAGGED person is a show visitor/buyer "
                       "(not the exhibitor's own staff, not a vendor promoting itself). Import real visitors with "
                       "`./lf add-people` (kind visitor_post or thanked_by_exhibitor, confidence confirmed, url = the "
                       "post, show_id set), looking up title/company with `./lf search` when missing. Then mark every "
                       "reviewed candidate with `./lf mark-posts`. Finish with `./lf export`."), \
            f"Claude: classify posts for show {p['show']}"
    if action == "claude_ask":
        text = (p.get("text") or "").strip()
        if not text:
            abort(400, "empty instruction")
        return _claude(f"Industry slug '{slug}'. Use the /find-leads skill conventions and the ./lf CLI. "
                       f"{HEADLESS_NOTE}\n\nTask from the user: {text[:4000]}"), f"Claude: {text[:60]}"
    abort(400, f"unknown action {action}")


def start_job(action, params, slug):
    cmd, label = build_command(action, params, slug)
    con = _con()
    cur = con.execute("INSERT INTO jobs(kind, label, industry, cmd, status, started_at) VALUES (?,?,?,?,?,?)",
                      ("claude" if action.startswith("claude") or action == "pipeline" else "lf", label, slug,
                       json.dumps(cmd), "running", _now()))
    jid = cur.lastrowid
    log_path = JOBS_DIR / f"job-{jid}.log"
    con.execute("UPDATE jobs SET log_path=? WHERE id=?", (str(log_path), jid))
    con.commit()
    logf = open(log_path, "w")
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True, env=env)
    con.execute("UPDATE jobs SET pid=? WHERE id=?", (proc.pid, jid))
    con.commit()
    con.close()

    def wait():
        code = proc.wait()
        logf.close()
        c = _con()
        st = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"]
        c.execute("UPDATE jobs SET status=?, exit_code=?, ended_at=? WHERE id=?",
                  ("stopped" if st == "stopping" else "done" if code == 0 else "failed", code, _now(), jid))
        c.commit()
        c.close()
    threading.Thread(target=wait, daemon=True).start()
    return jid


def _friendly_claude_log(raw):
    """Turn stream-json lines into readable progress lines."""
    out = []
    for line in raw.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            if line.strip():
                out.append(line)
            continue
        t = e.get("type")
        if t == "assistant":
            for c in e.get("message", {}).get("content", []):
                if c.get("type") == "text" and c.get("text", "").strip():
                    out.append("💬 " + c["text"].strip())
                elif c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    desc = inp.get("command") or inp.get("file_path") or inp.get("query") or inp.get("pattern") or ""
                    out.append(f"🔧 {c.get('name')}: {str(desc)[:300]}")
        elif t == "user":
            for c in e.get("message", {}).get("content", []):
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    content = c.get("content")
                    if isinstance(content, list):
                        content = " ".join(x.get("text", "") for x in content if isinstance(x, dict))
                    out.append("   ↳ " + str(content).replace("\n", " ")[:240])
        elif t == "result":
            out.append(f"✅ finished ({e.get('subtype')}), turns: {e.get('num_turns')}")
            if e.get("permission_denials"):
                out.append(f"⚠️ blocked actions: {len(e['permission_denials'])}")
    return "\n".join(out)


# ---------------- security guards ----------------
@app.before_request
def guard():
    host = (request.host or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost"):
        abort(403)
    if request.method not in ("GET", "HEAD") and request.headers.get("X-Requested-With") != "leadfinder":
        abort(403)


# ---------------- pages ----------------
@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC, name)


# ---------------- API ----------------
@app.get("/api/industries")
def industries():
    con = _con()
    rows = con.execute("SELECT id, slug, name, geo, icp_approved, icp_json IS NOT NULL AS has_icp FROM industries "
                       "ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        q = lambda sql: con.execute(sql, (r["id"],)).fetchone()[0]  # noqa: E731
        out.append({**dict(r),
                    "shows": q("SELECT COUNT(*) FROM shows WHERE industry_id=?"),
                    "companies": q("SELECT COUNT(*) FROM company_industries WHERE industry_id=?"),
                    "people": q("""SELECT COUNT(*) FROM people p JOIN company_industries ci ON ci.company_id=p.company_id
                                   WHERE ci.industry_id=? AND p.icp IS NOT NULL"""),
                    "emails": q("""SELECT COUNT(*) FROM people p JOIN company_industries ci ON ci.company_id=p.company_id
                                   WHERE ci.industry_id=? AND p.email IS NOT NULL""")})
    return jsonify(out)


@app.post("/api/industries")
def new_industry():
    p = request.get_json(force=True)
    name = (p.get("name") or "").strip()
    if not name:
        abort(400, "name required")
    slug = db.slugify(name)
    con = _con()
    con.execute("INSERT OR IGNORE INTO industries(slug, name, geo, created_at) VALUES (?,?,?,?)",
                (slug, name, p.get("geo") or "India + export markets", db.now()))
    con.commit()
    jid = start_job("claude_personas", {"name": name, "geo": p.get("geo")}, slug)
    return jsonify({"slug": slug, "job": jid})


@app.get("/api/industries/<slug>/icp")
def get_icp(slug):
    ind = db.get_industry(_con(), slug)
    return jsonify({"approved": bool(ind["icp_approved"]), "icp": json.loads(ind["icp_json"]) if ind["icp_json"] else None})


@app.put("/api/industries/<slug>/icp")
def put_icp(slug):
    data = request.get_json(force=True)
    for icp in data.get("icps", []):
        for k in ("code", "name", "title_keywords"):
            if k not in icp:
                abort(400, f"ICP missing {k}")
    con = _con()
    ind = db.get_industry(con, slug)
    con.execute("UPDATE industries SET icp_json=?, icp_approved=0 WHERE id=?", (json.dumps(data), ind["id"]))
    con.commit()
    return jsonify({"saved": True, "approved": False})


@app.post("/api/industries/<slug>/approve")
def approve(slug):
    con = _con()
    ind = db.get_industry(con, slug)
    if not ind["icp_json"]:
        abort(400, "no personas yet")
    con.execute("UPDATE industries SET icp_approved=1 WHERE id=?", (ind["id"],))
    con.commit()
    return jsonify({"approved": True})


@app.get("/api/industries/<slug>/leads")
def leads(slug):
    con = _con()
    ind = db.get_industry(con, slug)
    if not ind["icp_json"]:
        return jsonify([])
    rows = build_leads(con, ind)
    tr = {r["person_id"]: dict(r) for r in con.execute("SELECT * FROM tracker").fetchall()}
    keep = ("id", "icp", "score", "confidence", "name", "title", "company", "location", "country", "profile_flag",
            "linkedin_url", "email", "email_status", "email_source", "shows", "evidence_kind", "evidence_url", "why",
            "company_id", "show_ids")
    return jsonify([{**{k: l.get(k) for k in keep}, "tracker": tr.get(l["id"], {})} for l in rows])


@app.get("/api/people/<int:pid>")
def person(pid):
    """Everything known about one person: profile fields, company facts and every piece of evidence with its source."""
    con = _con()
    p = con.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not p:
        abort(404)
    c = con.execute("SELECT * FROM companies WHERE id=?", (p["company_id"],)).fetchone()
    ev = con.execute("""SELECT e.entity, e.kind, e.confidence, e.url, e.snippet, s.name AS show FROM evidence e
                        LEFT JOIN shows s ON s.id=e.show_id
                        WHERE (e.entity='person' AND e.entity_id=?) OR (e.entity='company' AND e.entity_id=?)
                        ORDER BY CASE e.confidence WHEN 'confirmed' THEN 0 WHEN 'likely' THEN 1 ELSE 2 END""",
                     (pid, p["company_id"])).fetchall()
    f = company_facts(con, p["company_id"]) if c else {}
    t = con.execute("SELECT * FROM tracker WHERE person_id=?", (pid,)).fetchone()
    keep = ("id", "name", "title", "location", "country", "linkedin_url", "email", "email_status", "email_source", "icp",
            "profile_flag", "li_company", "source")
    return jsonify({**{k: p[k] for k in keep},
                    "company": {"name": c["name"], "domain": c["domain"], "linkedin_url": c["linkedin_url"],
                                "shows_12mo": f.get("exhibitor_window_count"),
                                "exhibitor_at": [s["name"] for s in f.get("exhibitor_shows", [])]} if c else None,
                    "evidence": [dict(e) for e in ev], "tracker": dict(t) if t else {}})


@app.patch("/api/tracker/<int:pid>")
def tracker(pid):
    p = request.get_json(force=True)
    fields = ("status", "channel", "last_contacted", "interview_date", "pain_confirmed", "notes")
    con = _con()
    cur = dict(con.execute("SELECT * FROM tracker WHERE person_id=?", (pid,)).fetchone() or {})
    vals = {k: p.get(k, cur.get(k)) for k in fields}
    con.execute("""INSERT INTO tracker(person_id, status, channel, last_contacted, interview_date, pain_confirmed, notes,
                   updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(person_id) DO UPDATE SET status=excluded.status,
                   channel=excluded.channel, last_contacted=excluded.last_contacted,
                   interview_date=excluded.interview_date, pain_confirmed=excluded.pain_confirmed,
                   notes=excluded.notes, updated_at=excluded.updated_at""",
                (pid, *[vals[k] for k in fields], _now()))
    con.commit()
    return jsonify({"saved": True})


@app.get("/api/industries/<slug>/companies")
def companies(slug):
    con = _con()
    ind = db.get_industry(con, slug)
    icps = load_icps(ind) if ind["icp_json"] else []
    rows = con.execute("""SELECT c.* FROM companies c JOIN company_industries ci ON ci.company_id=c.id
                          WHERE ci.industry_id=?""", (ind["id"],)).fetchall()
    out = []
    for c in rows:
        f = company_facts(con, c["id"])
        fit = [i["code"] for i in icps if i.get("find_via", "company_list") == "company_list"
               and company_rule_ok(i.get("company_rule"), f)]
        n = con.execute("SELECT COUNT(*) FROM people WHERE company_id=? AND icp IS NOT NULL", (c["id"],)).fetchone()[0]
        out.append({"id": c["id"], "name": c["name"], "fit": fit, "domain": c["domain"], "domain_source": c["domain_source"],
                    "linkedin_url": c["linkedin_url"], "shows_12mo": f["exhibitor_window_count"],
                    "exhibitor_at": [s["name"] for s in f["exhibitor_shows"]], "visitor_signals": f["visitor_evidence"],
                    "people": n})
    out.sort(key=lambda r: (not r["fit"], -r["shows_12mo"], -r["visitor_signals"], r["name"]))
    return jsonify(out)


@app.patch("/api/companies/<int:cid>")
def edit_company(cid):
    p = request.get_json(force=True)
    con = _con()
    if "linkedin_url" in p:
        con.execute("UPDATE companies SET linkedin_url=? WHERE id=?",
                    ((p["linkedin_url"] or "").split("?")[0].rstrip("/") or None, cid))
    if "domain" in p:
        from .normalize import norm_domain
        con.execute("UPDATE companies SET domain=?, domain_source='manual' WHERE id=?", (norm_domain(p["domain"]) or None, cid))
    con.commit()
    return jsonify({"saved": True})


@app.get("/api/industries/<slug>/shows")
def shows(slug):
    con = _con()
    ind = db.get_industry(con, slug)
    rows = con.execute("""SELECT s.*, (SELECT COUNT(*) FROM evidence e WHERE e.show_id=s.id AND e.kind='exhibitor') exhibitors,
                          (SELECT COUNT(*) FROM evidence e WHERE e.show_id=s.id AND e.kind!='exhibitor') visitor_signals,
                          (SELECT COUNT(*) FROM post_candidates pc WHERE pc.show_id=s.id AND pc.status='new') posts_to_review
                          FROM shows s WHERE industry_id=? ORDER BY start_date""", (ind["id"],)).fetchall()
    return jsonify([dict(r) for r in rows])


# ---------------- persona searches (the simple flow) ----------------
def _running_pipeline(con, slug):
    return con.execute("SELECT id, status FROM jobs WHERE industry=? AND label LIKE 'Find people%' "
                       "AND status IN ('running','stopping') ORDER BY id DESC LIMIT 1", (slug,)).fetchone()


def _search_summary(con, ind):
    from .pipeline import STAGES
    st = {r["stage"]: dict(r) for r in con.execute("SELECT * FROM pipeline_stages WHERE industry_id=?",
                                                    (ind["id"],)).fetchall()}
    stages = []
    live = _running_pipeline(con, ind["slug"])
    for key, label in STAGES:
        r = st.get(key, {})
        status = r.get("status") or "pending"
        if status == "running" and not live:  # the process died (reboot, kill): show it as stopped, resumable
            status = "stopped"
        if status == "skipping" and not live:
            status = "skipped"
        stages.append({"key": key, "label": label, "status": status,
                       "detail": json.loads(r["detail"]) if r.get("detail") else None,
                       "started_at": r.get("started_at"), "ended_at": r.get("ended_at")})
    q = lambda sql: con.execute(sql, (ind["id"],)).fetchone()[0]  # noqa: E731
    job = _running_pipeline(con, ind["slug"])
    last = con.execute("SELECT id, status FROM jobs WHERE industry=? AND label LIKE 'Find people%' ORDER BY id DESC "
                       "LIMIT 1", (ind["slug"],)).fetchone()
    icps = [{"code": i["code"], "name": i["name"], "titles": i.get("titles", [])[:8], "rule": i.get("company_rule")}
            for i in (json.loads(ind["icp_json"])["icps"] if ind["icp_json"] else []) if i.get("enabled", True)]
    from . import targeting
    return {"slug": ind["slug"], "name": ind["name"], "persona": ind["persona_text"], "region": ind["geo"],
            "target": targeting.get(ind),
            "options": json.loads(ind["options_json"] or "{}"), "icps": icps, "stages": stages,
            "running_job": job["id"] if job else None, "stopping": bool(job and job["status"] == "stopping"), "last_job": dict(last) if last else None,
            "people": q("""SELECT COUNT(*) FROM people p JOIN company_industries ci ON ci.company_id=p.company_id
                           WHERE ci.industry_id=? AND p.icp IS NOT NULL"""),
            "emails": q("""SELECT COUNT(*) FROM people p JOIN company_industries ci ON ci.company_id=p.company_id
                           WHERE ci.industry_id=? AND p.icp IS NOT NULL AND p.email IS NOT NULL"""),
            "shows": q("SELECT COUNT(*) FROM shows WHERE industry_id=?"),
            "companies": q("SELECT COUNT(*) FROM company_industries WHERE industry_id=?")}


@app.get("/api/searches")
def searches():
    con = _con()
    rows = con.execute("SELECT * FROM industries ORDER BY id DESC").fetchall()
    return jsonify([_search_summary(con, r) for r in rows])


@app.get("/api/searches/<slug>")
def search_detail(slug):
    con = _con()
    return jsonify(_search_summary(con, db.get_industry(con, slug)))


def _opts_from(p, old=None):
    from . import targeting
    o = dict(old or {})
    if "target" in p:
        o["target"] = targeting.clean(p.get("target"))
    if "emails" in p:
        o["emails"] = max(0, min(int(p.get("emails") or 0), 200))
    if "model" in p:   # "routine" = Sonnet 5 low effort (default); "deep" = Opus high effort
        o["model"], o["effort"] = ("opus", "high") if p.get("model") == "deep" else ("sonnet", "low")
    return o


@app.post("/api/searches")
def new_search():
    p = request.get_json(force=True)
    persona = (p.get("persona") or "").strip()
    if len(persona) < 10:
        abort(400, "Describe the people you want to reach (a sentence or two).")
    title = (p.get("title") or "").strip() or " ".join(persona.split()[:7])
    con = _con()
    slug, base, n = db.slugify(title)[:60], db.slugify(title)[:60], 2
    while con.execute("SELECT 1 FROM industries WHERE slug=?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    opts = _opts_from(p)
    region = ", ".join((opts.get("target") or {}).get("countries") or []) or p.get("region") or "India"
    con.execute("INSERT INTO industries(slug, name, geo, persona_text, options_json, created_at) VALUES (?,?,?,?,?,?)",
                (slug, title, region, persona, json.dumps(opts), db.now()))
    con.commit()
    return jsonify({"slug": slug, "job": start_job("pipeline", {}, slug)})


@app.post("/api/searches/<slug>/run")
def rerun_search(slug):
    p = request.get_json(force=True)
    con = _con()
    ind = db.get_industry(con, slug)
    if _running_pipeline(con, slug):
        abort(409, "This search is already running.")
    from_stage = p.get("from_stage")
    if (p.get("persona") or "").strip() and p["persona"].strip() != (ind["persona_text"] or ""):
        con.execute("UPDATE industries SET persona_text=?, icp_approved=0 WHERE id=?", (p["persona"].strip(), ind["id"]))
        from_stage = "persona"
    from . import targeting
    old = json.loads(ind["options_json"] or "{}")
    new = _opts_from(p, old)
    if "target" in p:  # a targeting change must re-run the steps it affects, not be skipped as "already done"
        # compare with what the form showed (older searches derive countries from Region), not the raw stored value
        changed = targeting.rerun_from(targeting.get(ind), new.get("target") or {})
        order = [k for k, _ in __import__("leadfinder.pipeline", fromlist=["STAGES"]).STAGES]
        if changed and (not from_stage or order.index(changed) < order.index(from_stage)):
            from_stage = changed
        if new["target"].get("countries"):
            con.execute("UPDATE industries SET geo=? WHERE id=?", (", ".join(new["target"]["countries"]), ind["id"]))
    elif p.get("region") and p["region"] != ind["geo"]:
        con.execute("UPDATE industries SET geo=? WHERE id=?", (p["region"], ind["id"]))
        from_stage = from_stage or "persona"
    if int(new.get("emails", 25)) > int(old.get("emails", 25)) and not from_stage:
        from_stage = "emails"  # more emails wanted: the emails step must run again (it only tries people without one)
    con.execute("UPDATE industries SET options_json=? WHERE id=?", (json.dumps(new), ind["id"]))
    con.commit()
    return jsonify({"job": start_job("pipeline", {"from_stage": from_stage} if from_stage else {}, slug)})


@app.get("/api/credits")
def credits():
    from . import credits as cr
    return jsonify(cr.report(refresh=request.args.get("refresh") == "1"))


@app.post("/api/searches/<slug>/skip")
def skip_stage(slug):
    """Skip a step (or Include it again with skip=false). Skipping the running step interrupts just that step;
    the run continues with the next one."""
    from .pipeline import STAGES
    p = request.get_json(force=True)
    stage, skip = p.get("stage"), p.get("skip", True)
    if stage not in [k for k, _ in STAGES]:
        abort(400, "unknown step")
    con = _con()
    ind = db.get_industry(con, slug)
    cur = con.execute("SELECT status FROM pipeline_stages WHERE industry_id=? AND stage=?",
                      (ind["id"], stage)).fetchone()
    cur = cur["status"] if cur else None
    if not skip:
        if cur == "skipped":
            con.execute("DELETE FROM pipeline_stages WHERE industry_id=? AND stage=?", (ind["id"], stage))
            con.commit()
        return jsonify({"ok": True})
    if stage == "persona" and not ind["icp_json"]:
        abort(400, "This step can't be skipped yet: every later step needs the personas it makes.")
    job = _running_pipeline(con, slug)
    if cur == "running" and job:
        con.execute("UPDATE pipeline_stages SET status='skipping' WHERE industry_id=? AND stage=?", (ind["id"], stage))
        con.commit()
        pid = con.execute("SELECT pid FROM jobs WHERE id=?", (job["id"],)).fetchone()["pid"]
        try:
            os.killpg(pid, signal.SIGTERM)  # the pipeline sees 'skipping' and moves on instead of stopping
        except ProcessLookupError:
            pass
        return jsonify({"ok": True, "interrupted": True})
    if cur == "done":
        abort(409, "This step has already finished.")
    con.execute("""INSERT INTO pipeline_stages(industry_id, stage, status, detail, started_at, ended_at)
                   VALUES (?,?,'skipped',?,?,?) ON CONFLICT(industry_id, stage) DO UPDATE SET status='skipped',
                   detail=excluded.detail, ended_at=excluded.ended_at""",
                (ind["id"], stage, json.dumps({"note": "Skipped by you."}), db.now(), db.now()))
    con.commit()
    return jsonify({"ok": True})


@app.post("/api/searches/<slug>/stop")
def stop_search(slug):
    """Stop a running search. Finished steps are kept; Resume continues from the step that was running."""
    con = _con()
    job = _running_pipeline(con, slug)
    if not job:
        abort(409, "This search is not running.")
    return stop_job(job["id"])


@app.get("/api/jobs")
def jobs():
    rows = _con().execute("SELECT id, kind, label, industry, status, started_at, ended_at, exit_code FROM jobs "
                          "ORDER BY id DESC LIMIT 50").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/jobs")
def new_job():
    p = request.get_json(force=True)
    return jsonify({"job": start_job(p.get("action", ""), p.get("params") or {}, p.get("industry"))})


@app.get("/api/jobs/<int:jid>")
def job(jid):
    r = _con().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r:
        abort(404)
    raw = open(r["log_path"]).read() if r["log_path"] and os.path.exists(r["log_path"]) else ""
    text = _friendly_claude_log(raw) if r["kind"] == "claude" else raw
    return jsonify({**{k: r[k] for k in ("id", "kind", "label", "industry", "status", "started_at", "ended_at",
                                         "exit_code")}, "log": text[-60000:]})


@app.post("/api/jobs/<int:jid>/stop")
def stop_job(jid):
    con = _con()
    r = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if r and r["status"] == "running" and r["pid"]:
        con.execute("UPDATE jobs SET status='stopping' WHERE id=?", (jid,))
        con.commit()
        try:
            os.killpg(r["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    return jsonify({"stopping": True})


@app.get("/download/<slug>.xlsx")
def download(slug):
    p = path_for(db.get_industry(_con(), slug))
    if not p.exists():
        abort(404, "export first")
    return send_file(p, as_attachment=True, download_name=p.name)


def _watch_orphan(jid, pid):
    """A job started by a previous server process: poll until it exits, then mark it finished."""
    import time
    while True:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(5)
    c = _con()
    log_path = c.execute("SELECT log_path FROM jobs WHERE id=?", (jid,)).fetchone()["log_path"]
    tail = open(log_path).read()[-3000:] if log_path and os.path.exists(log_path) else ""
    ok = "failed:" not in tail.splitlines()[-1] if tail.strip() else True
    c.execute("UPDATE jobs SET status=?, ended_at=? WHERE id=?", ("done" if ok else "failed", _now(), jid))
    c.commit()


def serve(port=8787):
    c = _con()  # jobs from a previous server run: keep 'running' only if the process is still alive
    for r in c.execute("SELECT id, pid FROM jobs WHERE status IN ('running','stopping')").fetchall():
        alive = False
        try:
            os.kill(r["pid"], 0)
            alive = True
        except (OSError, TypeError):
            pass
        if not alive:
            c.execute("UPDATE jobs SET status='interrupted', ended_at=? WHERE id=?", (_now(), r["id"]))
        else:
            threading.Thread(target=_watch_orphan, args=(r["id"], r["pid"]), daemon=True).start()
    c.commit()
    print(f"Lead Finder UI: http://127.0.0.1:{port}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
