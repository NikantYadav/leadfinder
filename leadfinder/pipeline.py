"""One-click pipeline: persona text -> evidence-backed leads with emails.

Stages (each recorded in pipeline_stages so the web UI can show a checklist; finished stages are skipped on re-run):
  persona   Claude turns the free-text persona into ICPs (titles, keywords, company rule)      [Claude]
  research  Claude finds shows, exhibitor lists and visitor/buyer signals                      [Claude]
  linkedin  direct LinkedIn people search per ICP (Apify; free plan: 25 profiles/run, 10 runs)
  web       search-engine LinkedIn lookups per company x ICP (TinySearch first)
  verify    LinkedIn profile check of the top leads (country + current company)
  emails    company websites + email waterfall for the top leads
  export    Excel file
Run: ./lf pipeline --industry <slug> [--emails 25] [--model sonnet] [--from-stage research]
"""
import json
import signal
import subprocess
import sys
import time

from . import config, db, targeting

STAGES = [
    ("persona", "Understand the persona"),
    ("research", "Find trade shows, exhibitors and buyers"),
    ("linkedin", "Search LinkedIn"),
    ("web", "Search the web for more people"),
    ("verify", "Check LinkedIn profiles"),
    ("emails", "Find work emails"),
    ("export", "Prepare results"),
]
CLAUDE_TOOLS = ["Bash(./lf:*)", "Read", "Glob", "Grep", "Write(data/work/**)", "Edit(data/work/**)", "WebSearch", "Skill"]


class Stopped(BaseException):
    """The user pressed Stop (SIGTERM). BaseException so stage code's `except Exception` can't swallow it."""

HEADLESS = ("You are running headless from the Lead Finder web UI as one stage of an automated pipeline: there is no "
            "user to ask. Never run `./lf capture` (needs a human). Never run `./lf approve-icp` unless told to. Keep "
            "your final message to a 3-6 line summary with counts.")


def log(msg):
    print(f"▶ {msg}", flush=True)


def _stage_set(con, ind_id, stage, status, detail=None):
    con.execute("""INSERT INTO pipeline_stages(industry_id, stage, status, detail, started_at, ended_at)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(industry_id, stage) DO UPDATE SET status=excluded.status,
                   detail=COALESCE(excluded.detail, detail),
                   started_at=CASE WHEN excluded.status='running' THEN excluded.started_at ELSE started_at END,
                   ended_at=excluded.ended_at""",
                (ind_id, stage, status, json.dumps(detail) if detail is not None else None, db.now(),
                 db.now() if status in ("done", "failed", "skipped") else None))
    con.commit()


DEFAULT_MODEL, DEFAULT_EFFORT = "sonnet", "low"   # routine runs: Sonnet 5 at low effort (user's choice)


def _claude(prompt, model=None, effort=None):
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--allowedTools", *CLAUDE_TOOLS,
           "--model", model or DEFAULT_MODEL, "--effort", effort or DEFAULT_EFFORT]
    sys.stdout.flush()
    r = subprocess.run(cmd, cwd=config.ROOT, stdout=sys.stdout, stderr=subprocess.STDOUT)
    sys.stdout.flush()
    return r.returncode


def stage_persona(con, ind, opts):
    slug = ind["slug"]
    prompt = f"""{HEADLESS}

Task: build the personas for search slug '{slug}' (region: {ind['geo']}) ONLY from the user's description below.
Do not reuse personas, titles or keywords from any other search or file - everything must come from this text.

USER'S DESCRIPTION:
<<<
{ind['persona_text']}
>>>

{targeting.prompt_block(ind)}

1. Read templates/icp_schema.json - it defines the JSON shape and allowed values only (it has no content to copy).
2. Write data/work/{slug}/icp.json in that shape. One ICP per distinct kind of person in the description (often just
   one), codes P1, P2, ... Decide company_rule and find_via from what the user said (e.g. "visit trade shows" ->
   visitor + company_list; "exhibit at 3+ shows" -> exhibitor_3plus + company_list; no mention of shows ->
   any + industry_search). Always fill linkedin_filters (functions + seniority matching the roles and levels the user
   describes; default seniority manager, director, vp, cxo).
3. Run `./lf set-icp --industry {slug} --file data/work/{slug}/icp.json` then `./lf approve-icp --industry {slug}`
   (the user wrote this description themselves, so it is pre-approved)."""
    code = _claude(prompt, opts.get("model"), opts.get("effort"))
    ind2 = db.get_industry(con, slug)
    if code != 0 or not ind2["icp_json"]:
        raise RuntimeError("Claude did not produce personas")
    # enforce the form's targeting whatever Claude wrote
    doc = targeting.apply(json.loads(ind2["icp_json"]), ind2)
    con.execute("UPDATE industries SET icp_json=?, icp_approved=1 WHERE id=?", (json.dumps(doc), ind["id"]))
    con.commit()
    icps = doc["icps"]
    return {"icps": [f'{i["code"]}: {i["name"]}' for i in icps if i.get("enabled", True)]}


def stage_research(con, ind, opts):
    slug = ind["slug"]
    icps = json.loads(db.get_industry(con, slug)["icp_json"])["icps"]
    where = ", ".join(targeting.get(ind).get("countries") or [ind["geo"] or "India"])
    cities = targeting.get(ind).get("cities")
    region_note = (f" Region: {where}" + (f" (focus: {', '.join(cities)})" if cities else "") +
                   " - pick shows held in, or drawing buyers from, this region.")
    needs_shows = any(i.get("find_via", "company_list") == "company_list" for i in icps if i.get("enabled", True))
    if not needs_shows:
        prompt = (f"/find-leads (continue industry slug '{slug}'; personas approved)\n\n{HEADLESS} This persona is found "
                  "by title x sector x city, not via shows: there is nothing to research. Reply 'no research needed'.")
    else:
        prompt = (f"/find-leads (continue industry slug '{slug}'; personas approved)\n\n{HEADLESS} Do ONLY: Step 2 (4-6 "
                  "most relevant shows, exact dates)" + region_note + ", Step 3a (exhibitor lists; use `./lf parse-list` for fair-guide "
                  "PDFs; get exhibitor lists for AT LEAST 4 shows - the '3+ shows a year' rule needs overlap - and "
                  "name in your summary any show whose list you could not get), Step 3b (visitor/buyer signals: post-show reports, order news, delegations, buyer-seller "
                  "meets; use `./lf plan-posts` + `./lf run-queue --purpose posts --max 10` and classify post "
                  "candidates). "
                  "Prefer `./lf search` and `./lf fetch` over your own WebSearch. Do NOT run people searches, "
                  "`./lf linkedin-people`, `./lf verify-profiles`, `./lf enrich-emails` or `./lf export` - later "
                  "pipeline stages do those. Budget: about 40 searches.")
    code = _claude(prompt, opts.get("model"), opts.get("effort"))
    stats = con.execute("""SELECT (SELECT COUNT(*) FROM shows WHERE industry_id=:i) shows,
                                  (SELECT COUNT(*) FROM company_industries WHERE industry_id=:i) companies""",
                        {"i": ind["id"]}).fetchone()
    if code != 0:
        raise RuntimeError("Claude research stage failed")
    return dict(stats)


def _decide_min_shows(con, ind):
    """If no company meets the persona's '3+ shows' rule, relax to 2, then 1, and remember it for this search."""
    from .people import companies_for_icp, load_icps
    used = 3
    for icp in load_icps(ind):
        if icp.get("company_rule") == "exhibitor_3plus":
            comps, m = companies_for_icp(con, ind, icp, 3)
            used = min(used, m) if comps else used
    opts = json.loads(ind["options_json"] or "{}")
    if opts.get("min_shows") != used:
        opts["min_shows"] = used
        con.execute("UPDATE industries SET options_json=? WHERE id=?", (json.dumps(opts), ind["id"]))
        con.commit()
    return used


def stage_linkedin(con, ind, opts):
    from . import enrich
    from .people import load_icps, plan_title_searches, run_queue
    from . import keys
    from .providers import linkedin as lip
    ind = db.get_industry(con, ind["slug"])
    m = _decide_min_shows(con, ind)
    ind = db.get_industry(con, ind["slug"])
    out = {}
    prev = _progress(con, ind["id"], "linkedin")
    prev_done = {k: v for k, v in prev.items()
                 if isinstance(v, dict) and not v.get("error") and not v.get("skipped") and not v.get("in_flight")}
    if m < 3:
        out["note"] = (f"No company exhibited at 3+ of the shows found, so companies at {m}+ show"
                       f"{'s' if m > 1 else ''} were used (best first).")
    for icp in load_icps(ind):
        if icp["code"] in prev_done:  # finished before a Stop: don't spend another Apify run on it
            out[icp["code"]] = prev_done[icp["code"]]
            continue
        if icp.get("find_via") == "industry_search":
            doc = json.loads(ind["icp_json"])
            sectors = (doc.get("sectors") or [ind["name"]])[:3]
            hubs = targeting.hubs(ind, doc)[:4]
            r = plan_title_searches(con, ind, icp["code"], sectors, hubs, limit=24)
            out[icp["code"]] = {"title_searches_planned": r["planned"]}
            continue
        runs_left = sum(lip.apify_runs_left(k) for k in keys.usable("apify")) if lip.apify_available() else 0
        # an Apify run started before a Stop keeps running on Apify's side: reuse its results instead of paying again
        flight = (prev.get(icp["code"]) or {}).get("in_flight") if isinstance(prev.get(icp["code"]), dict) else None
        ds = lip.finished_run_dataset(flight) if flight and lip.apify_available() else None
        if ds:
            log(f"LinkedIn search for ICP {icp['code']}: reusing the Apify run that finished after the Stop")
            out[icp["code"]] = enrich.linkedin_people(con, ind, icp["code"], max_items=25, log=log, via="apify",
                                                     companies_limit=40, dataset_id=ds)
        elif runs_left:
            log(f"LinkedIn search for ICP {icp['code']} ({runs_left} free Apify run(s) left across your keys)")
            _save_progress(con, ind["id"], "linkedin", {**out, icp["code"]: {"in_flight": db.now()}})
            out[icp["code"]] = enrich.linkedin_people(con, ind, icp["code"], max_items=25, log=log, via="apify",
                                                     companies_limit=40)
        elif lip.brightdata_available():
            out[icp["code"]] = enrich.linkedin_people(con, ind, icp["code"], max_items=100, log=log, via="brightdata")
        else:
            out[icp["code"]] = {"skipped": "no LinkedIn search credits left (Apify free runs used up on every key)"}
        _save_progress(con, ind["id"], "linkedin", out)
    return out


def stage_web(con, ind, opts):
    from .people import plan_people_searches, run_queue
    ind = db.get_industry(con, ind["slug"])
    planned = plan_people_searches(con, ind, limit=60)
    log(f"web people searches planned: {planned}")
    total = {"done": 0, "people_added": 0}
    for _ in range(4):  # up to ~80 searches; stops early when the queue is empty or providers are busy
        r = run_queue(con, max_items=20, log=lambda m: print(m, flush=True))
        total["done"] += r["done"]
        total["people_added"] += r["people_added"]
        if r["stopped"] or not r["pending"]:
            total["stopped"] = r.get("stopped")
            break
    return total


def stage_verify(con, ind, opts):
    from . import enrich
    from .providers import linkedin as lip
    if not (lip.apify_available() or lip.brightdata_available()):
        n = targeting.refresh_geo_flags(con, db.get_industry(con, ind["slug"]))
        return {"skipped": "no LinkedIn profile provider configured", "geo_flags_updated": n}
    ind = db.get_industry(con, ind["slug"])
    r = enrich.verify_profiles(con, ind, top=int(opts.get("verify", 40)), log=log)
    r["geo_flags_updated"] = targeting.refresh_geo_flags(con, ind)
    return r


def stage_emails(con, ind, opts):
    from . import enrich
    n = int(opts.get("emails", 25))
    if n <= 0:
        return {"skipped": "email search turned off"}
    ind = db.get_industry(con, ind["slug"])
    d = enrich.resolve_domains(con, ind, limit=40, log=log)
    e = enrich.enrich_emails(con, ind, top=n, log=log)
    return {"websites_found": d.get("hunter", 0) + d.get("search_guess", 0), **e}


def stage_export(con, ind, opts):
    from .export import export
    r = export(con, db.get_industry(con, ind["slug"]))
    return {"leads": r["leads"], "by_icp": r["by_icp"]}


def _empty_warning(key, d):
    if key == "research" and not d.get("companies"):
        return "No companies found at the shows. Try wording the description more broadly."
    if key == "linkedin":
        vals = [v for k, v in d.items() if isinstance(v, dict)]
        if vals and not any((v.get("people_added_or_matched") or v.get("title_searches_planned")) for v in vals):
            errs = [v.get("error") or v.get("skipped") for v in vals if v.get("error") or v.get("skipped")]
            return "LinkedIn search found nobody" + (f": {errs[0]}" if errs else ".")
    if key == "web" and not d.get("done"):
        return "No web searches ran (nothing planned, or all search providers are busy)."
    if key == "export" and not d.get("leads"):
        return "No people matched this persona yet. Use 'Edit & re-run' to broaden the description."
    return None


RUNNERS = {"persona": stage_persona, "research": stage_research, "linkedin": stage_linkedin, "web": stage_web,
           "verify": stage_verify, "emails": stage_emails, "export": stage_export}


def _progress(con, ind_id, stage):
    """Partial results a stopped stage saved, so a resumed run continues instead of starting over."""
    row = con.execute("SELECT status, detail FROM pipeline_stages WHERE industry_id=? AND stage=?",
                      (ind_id, stage)).fetchone()
    if not row or row["status"] not in ("running", "stopped") or not row["detail"]:
        return {}
    d = json.loads(row["detail"])
    return d.get("partial") or {}


def _save_progress(con, ind_id, stage, partial):
    con.execute("UPDATE pipeline_stages SET detail=? WHERE industry_id=? AND stage=?",
                (json.dumps({"partial": partial}, default=str), ind_id, stage))
    con.commit()


def _status(con, ind_id, stage):
    row = con.execute("SELECT status FROM pipeline_stages WHERE industry_id=? AND stage=?", (ind_id, stage)).fetchone()
    return row["status"] if row else None


def _on_term(signum, frame):
    raise Stopped()


def run(slug, opts, from_stage=None):
    signal.signal(signal.SIGTERM, _on_term)
    con = db.connect()
    ind = db.get_industry(con, slug)
    (config.DATA / "work" / slug).mkdir(parents=True, exist_ok=True)  # Claude may write here (no mkdir permission)
    keys = [k for k, _ in STAGES]
    if from_stage:
        for k in keys[keys.index(from_stage):]:
            # a step the user chose to skip stays skipped across re-runs (they can Include it again)
            con.execute("DELETE FROM pipeline_stages WHERE industry_id=? AND stage=? AND status!='skipped'",
                        (ind["id"], k))
        con.commit()
    for key, label in STAGES:
        status = _status(con, ind["id"], key)  # read fresh: the user may skip a later step while this runs
        if status == "done":
            log(f"{label}: already done, skipping")
            continue
        if status == "skipped":
            log(f"{label}: skipped by you")
            continue
        if key == "persona" and not ind["persona_text"] and ind["icp_json"]:
            _stage_set(con, ind["id"], key, "done", {"note": "personas defined earlier"})
            continue
        log(f"{label}…")
        _stage_set(con, ind["id"], key, "running")
        t = time.time()
        try:
            detail = RUNNERS[key](con, ind, opts)
        except Stopped:
            if _status(con, ind["id"], key) == "skipping":  # Skip pressed on this running step: move on
                _stage_set(con, ind["id"], key, "skipped", {"note": "Skipped by you while it was running."})
                log(f"{label}: skipped by you, continuing with the next step")
                if key == "persona" and not db.get_industry(con, slug)["icp_json"]:
                    return 1
                continue
            row = con.execute("SELECT detail FROM pipeline_stages WHERE industry_id=? AND stage=?",
                              (ind["id"], key)).fetchone()
            keep = json.loads(row["detail"]) if row and row["detail"] else {}
            _stage_set(con, ind["id"], key, "stopped", {**keep, "note": "Stopped by you. Resume continues here."})
            log(f"{label}: stopped. Resume continues from this step.")
            return 130
        except Exception as e:  # noqa: BLE001 - record and continue where it is safe to
            _stage_set(con, ind["id"], key, "failed", {"error": str(e)[:500]})
            log(f"{label} failed: {e}")
            if key in ("persona",):
                return 1  # nothing downstream can run without personas
            continue
        detail = {**(detail or {}), "seconds": round(time.time() - t)}
        warn = _empty_warning(key, detail)
        if warn:
            detail["warning"] = warn
        _stage_set(con, ind["id"], key, "warn" if warn else "done", detail)
        log(f"{label}: done {json.dumps(detail, default=str)[:400]}")
    return 0
