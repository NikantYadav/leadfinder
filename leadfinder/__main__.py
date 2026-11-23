"""lf - Enfolo Lead Finder CLI. JSON in, JSON out, so Claude (the /find-leads skill) can drive it.
Run `./lf <command> -h` for options."""
import argparse
import json
import sys

from . import db, people
from .providers import fetch as fetcher
from .providers import search as router


def out(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def read_json(path):
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("rows", data)


# ---------------- commands ----------------
def cmd_init_industry(a, con):
    slug = db.slugify(a.name)
    con.execute("INSERT OR IGNORE INTO industries(slug, name, geo, created_at) VALUES (?,?,?,?)",
                (slug, a.name, a.geo, db.now()))
    con.commit()
    out(dict(db.get_industry(con, slug)))


def cmd_set_icp(a, con):
    ind = db.get_industry(con, a.industry)
    data = json.load(open(a.file))
    for icp in data["icps"]:
        for k in ("code", "name", "title_keywords"):
            if k not in icp:
                raise SystemExit(f"ICP missing '{k}': {icp}")
    con.execute("UPDATE industries SET icp_json=?, icp_approved=0 WHERE id=?", (json.dumps(data), ind["id"]))
    con.commit()
    out({"industry": ind["slug"], "icps": [i["code"] for i in data["icps"]], "approved": False,
         "next": "show the personas to the user; after they approve run: lf approve-icp --industry " + ind["slug"]})


def cmd_approve_icp(a, con):
    ind = db.get_industry(con, a.industry)
    con.execute("UPDATE industries SET icp_approved=1 WHERE id=?", (ind["id"],))
    con.commit()
    out({"industry": ind["slug"], "approved": True})


def _require_approved(ind):
    if not ind["icp_approved"]:
        raise SystemExit("ICPs for this industry are not approved yet. Show them to the user, then `lf approve-icp`.")


def cmd_add_shows(a, con):
    ind = db.get_industry(con, a.industry)
    n = 0
    for s in read_json(a.file):
        con.execute("""INSERT INTO shows(industry_id, name, series, year, start_date, end_date, city, country, url,
                       exhibitor_list_url, source_url, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(industry_id, name, year) DO UPDATE SET
                       start_date=COALESCE(excluded.start_date, start_date), end_date=COALESCE(excluded.end_date, end_date),
                       city=COALESCE(excluded.city, city), country=COALESCE(excluded.country, country),
                       url=COALESCE(excluded.url, url), exhibitor_list_url=COALESCE(excluded.exhibitor_list_url, exhibitor_list_url),
                       source_url=COALESCE(excluded.source_url, source_url), notes=COALESCE(excluded.notes, notes)""",
                    (ind["id"], s["name"], s.get("series"), s.get("year"), s.get("start_date"), s.get("end_date"),
                     s.get("city"), s.get("country"), s.get("url"), s.get("exhibitor_list_url"),
                     s.get("source_url"), s.get("notes")))
        n += 1
    con.commit()
    cmd_list_shows(a, con, extra={"upserted": n})


def cmd_list_shows(a, con, extra=None):
    ind = db.get_industry(con, a.industry)
    rows = con.execute("""SELECT s.id, s.name, s.year, s.start_date, s.city, s.country, s.exhibitor_list_url,
                          (SELECT COUNT(*) FROM evidence e WHERE e.show_id=s.id AND e.kind='exhibitor') AS exhibitors,
                          (SELECT COUNT(*) FROM evidence e WHERE e.show_id=s.id AND e.kind!='exhibitor') AS visitor_signals
                          FROM shows s WHERE industry_id=? ORDER BY start_date""", (ind["id"],)).fetchall()
    out({**(extra or {}), "shows": [dict(r) for r in rows]})


def cmd_add_companies(a, con):
    """rows: {name, domain?, country?, city?, sector?, url (evidence, required), snippet?, kind?, confidence?, show_id?}"""
    ind = db.get_industry(con, a.industry)
    added, errors = 0, []
    for r in read_json(a.file):
        try:
            cid = db.upsert_company(con, r["name"], r.get("domain"), r.get("country"), r.get("city"),
                                    r.get("sector"), ind["id"])
            db.add_evidence(con, "company", cid, r.get("kind", a.kind), r.get("confidence", a.confidence),
                            r.get("url") or a.url, r.get("snippet"), r.get("show_id", a.show))
            added += 1
        except (KeyError, ValueError) as e:
            errors.append({"row": r, "error": str(e)})
    con.commit()
    out({"added_or_matched": added, "errors": errors[:10]})


def cmd_add_people(a, con):
    """rows: {name, title?, company, company_domain?, location?, linkedin_url?, email?, icp?,
              kind, confidence, url (evidence), snippet?, show_id?}"""
    ind = db.get_industry(con, a.industry)
    added, errors = 0, []
    for r in read_json(a.file):
        try:
            cid = db.upsert_company(con, r["company"], r.get("company_domain"), industry_id=ind["id"]) \
                if r.get("company") else None
            pid = db.upsert_person(con, cid, r["name"], r.get("title"), r.get("location"), r.get("linkedin_url"),
                                   r.get("email"), r.get("phone"), r.get("icp"), r.get("source", "manual"))
            db.add_evidence(con, "person", pid, r.get("kind", a.kind), r.get("confidence", a.confidence),
                            r.get("url") or a.url, r.get("snippet"), r.get("show_id", a.show))
            if r.get("company_evidence", True) and cid and r.get("kind", a.kind) not in ("linkedin_profile",):
                # a person seen at a show also puts their company there
                db.add_evidence(con, "company", cid, r.get("kind", a.kind), "likely" if r.get(
                    "confidence", a.confidence) == "confirmed" else "inferred", r.get("url") or a.url,
                    r.get("snippet"), r.get("show_id", a.show))
            added += 1
        except (KeyError, ValueError) as e:
            errors.append({"row": r, "error": str(e)})
    con.commit()
    out({"added_or_matched": added, "errors": errors[:10]})


def cmd_frequent(a, con):
    ind = db.get_industry(con, a.industry)
    comps = con.execute("""SELECT c.id, c.name, c.domain FROM companies c JOIN company_industries ci
                           ON ci.company_id=c.id WHERE ci.industry_id=?""", (ind["id"],)).fetchall()
    res = []
    for c in comps:
        f = people.company_facts(con, c["id"], a.window_days)
        if f["exhibitor_window_count"] >= a.min:
            res.append({**dict(c), "shows_in_window": f["exhibitor_window_count"],
                        "shows": [s["name"] for s in f["exhibitor_shows"]]})
    res.sort(key=lambda r: -r["shows_in_window"])
    out({"min_shows": a.min, "window_days": a.window_days, "count": len(res), "companies": res[:a.limit]})


def cmd_plan_people(a, con):
    ind = db.get_industry(con, a.industry)
    _require_approved(ind)
    codes = a.icp.split(",") if a.icp else None
    out(people.plan_people_searches(con, ind, codes, a.limit, a.min_shows))


def cmd_plan_titles(a, con):
    ind = db.get_industry(con, a.industry)
    _require_approved(ind)
    out(people.plan_title_searches(con, ind, a.icp, a.sectors.split("|"), a.locations.split("|"), a.limit))


def cmd_plan_posts(a, con):
    show = con.execute("SELECT * FROM shows WHERE id=?", (a.show,)).fetchone()
    if not show:
        raise SystemExit("unknown show id")
    people.plan_post_searches(con, show, a.hashtag)
    out({"show": show["name"], "queued": con.execute(
        "SELECT COUNT(*) FROM search_queue WHERE purpose='posts' AND status='pending'").fetchone()[0]})


def cmd_run_queue(a, con):
    out(people.run_queue(con, a.max, a.purpose, log=lambda m: print(m, file=sys.stderr), retry_empty=a.retry_empty))


def cmd_queue(a, con):
    rows = con.execute("""SELECT purpose, status, COUNT(*) n FROM search_queue GROUP BY purpose, status""").fetchall()
    out({"queue": [dict(r) for r in rows], "providers": router.status(con)})


def cmd_queue_links(a, con):
    """Pending searches as clickable Google links - the zero-key fallback."""
    rows = con.execute("SELECT query, params, context FROM search_queue WHERE status='pending' LIMIT ?",
                       (a.limit,)).fetchall()
    out([{"query": r["query"], "context": json.loads(r["context"]),
          "link": router.google_link(r["query"], json.loads(r["params"]).get("site"))} for r in rows])


def cmd_post_candidates(a, con):
    rows = con.execute("SELECT * FROM post_candidates WHERE show_id=? AND status=?", (a.show, a.status)).fetchall()
    out([dict(r) for r in rows])


def cmd_mark_posts(a, con):
    con.execute(f"UPDATE post_candidates SET status=? WHERE id IN ({','.join('?' * len(a.ids))})", (a.status, *a.ids))
    con.commit()
    out({"updated": len(a.ids)})


def cmd_search(a, con):
    try:
        results, provider = router.search(con, a.query, site=a.site, num=a.num, region=a.region)
        out({"provider": provider, "results": results})
    except router.ProviderBusy as e:
        out({"error": str(e), "fallback_link": router.google_link(a.query, a.site)})


def cmd_fetch(a, con):
    out(fetcher.fetch(con, a.url, a.mode, a.refresh))


def cmd_capture(a, con):
    from .capture import capture
    out(capture(a.url, a.minutes, label=a.label))


def cmd_parse_list(a, con):
    """Parse a fetched exhibitor list (fair guide / catalogue) into add-companies rows."""
    from .parsers import parse_hall_stall
    text = open(a.file).read()
    src = a.url or (text.splitlines()[0].replace("SOURCE: ", "") if text.startswith("SOURCE:") else None)
    rows = parse_hall_stall(text, a.section)
    out_rows = [{"name": r["name"], "url": src, "snippet": f"Hall {r['hall']} Stall {r['stall']}"} for r in rows]
    with open(a.out, "w") as f:
        json.dump(out_rows, f, indent=1, ensure_ascii=False)
    out({"rows": len(out_rows), "out": a.out, "sample": out_rows[:5],
         "next": "spot-check names, then: lf add-companies --industry <slug> --show <id> --file " + a.out})


def cmd_verify_profiles(a, con):
    """Phase 2: check location + current company of the top leads via LinkedIn API (Apify / Bright Data)."""
    from . import enrich
    ind = db.get_industry(con, a.industry)
    out(enrich.verify_profiles(con, ind, a.top, a.icp.split(",") if a.icp else None, a.recheck,
                               log=lambda m: print(m, file=sys.stderr)))


def cmd_resolve_domains(a, con):
    """Phase 2: find website domains for companies that have leads (Hunter free domain finder / search guess)."""
    from . import enrich
    out(enrich.resolve_domains(con, db.get_industry(con, a.industry), a.limit, log=lambda m: print(m, file=sys.stderr),
                                redo_guesses=a.redo_guesses))


def cmd_enrich_emails(a, con):
    """Phase 2: email waterfall (Prospeo -> Hunter -> Apollo -> Tomba -> BetterContact) for the top leads."""
    from . import enrich
    ind = db.get_industry(con, a.industry)
    out(enrich.enrich_emails(con, ind, a.top, a.icp.split(",") if a.icp else None, a.include_flagged,
                             only=a.only.split(",") if a.only else None, log=lambda m: print(m, file=sys.stderr)))


def cmd_posts_api(a, con):
    """Phase 2: LinkedIn post search via Apify -> post_candidates (then classify + add-people)."""
    from . import enrich
    show = con.execute("SELECT * FROM shows WHERE id=?", (a.show,)).fetchone()
    if not show:
        raise SystemExit("unknown show id")
    out(enrich.posts_via_api(con, show, a.query, a.max, a.after))


def cmd_linkedin_people(a, con):
    """Direct LinkedIn people search (Apify, no login): one batched run per ICP over all its companies."""
    from . import enrich
    ind = db.get_industry(con, a.industry)
    _require_approved(ind)
    out(enrich.linkedin_people(con, ind, a.icp, a.max, a.companies, log=lambda m: print(m, file=sys.stderr), via=a.via,
                               dataset_id=a.dataset, seniority=a.seniority.split(",") if a.seniority else None))


def cmd_company_posts(a, con):
    """Bright Data: exhibitor company-page posts around a show (tagged visitors) -> post_candidates."""
    from . import enrich
    show = con.execute("SELECT * FROM shows WHERE id=?", (a.show,)).fetchone()
    if not show:
        raise SystemExit("unknown show id")
    out(enrich.company_posts(con, show, None, a.companies, a.per_company, log=lambda m: print(m, file=sys.stderr)))


def _show(con, ind, show_id):
    show = con.execute("SELECT * FROM shows WHERE id=? AND industry_id=?", (show_id, ind["id"])).fetchone()
    if not show:
        raise SystemExit("unknown show id for this industry (see lf list-shows)")
    return show


def cmd_exhibitors_apify(a, con):
    """Read a show's exhibitor catalog with Apify (35+ fair platforms; budget in USD)."""
    import time
    from . import showdata
    ind = db.get_industry(con, a.industry)
    show = _show(con, ind, a.show)
    show = dict(show)
    if a.list_url:
        show["exhibitor_list_url"] = a.list_url
    res = showdata.harvest_exhibitors(con, ind, show, a.budget, a.max, time.time() + a.minutes * 60,
                                      log=lambda m: print(m, file=sys.stderr))
    if a.list_url and res.get("added"):  # keep a working URL only; never overwrite a good one with a failed guess
        con.execute("UPDATE shows SET exhibitor_list_url=? WHERE id=?", (a.list_url, show["id"]))
        con.commit()
    out(res)


def cmd_discover_shows(a, con):
    """Trade shows by topic/country/date from 10times via Apify (candidates only; add the relevant ones with add-shows)."""
    from . import showdata
    ind = db.get_industry(con, a.industry)
    res = showdata.discover_shows(con, ind, a.query, a.countries.split(",") if a.countries else None,
                                  a.start, a.end, a.max, log=lambda m: print(m, file=sys.stderr))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res["events"], f, indent=1)
    out(res)


def cmd_show_people(a, con):
    """People who posted/commented about a show on LinkedIn, via Apify (no login). Visitors -> visitor_post evidence."""
    import time
    from . import showdata
    ind = db.get_industry(con, a.industry)
    show = _show(con, ind, a.show)
    ref = showdata.ev.adopt(showdata.ev.PEOPLE, a.run) if a.run else None
    out(showdata.show_people(con, ind, dict(show), a.budget, a.target, time.time() + a.minutes * 60,
                             log=lambda m: print(m, file=sys.stderr), ref=ref, role_based=a.role_based))


def cmd_set_company(a, con):
    """Manually correct a company's LinkedIn page / website (overrides automatic resolution)."""
    row = con.execute("SELECT * FROM companies WHERE id=? OR name=?", (a.id, a.name)).fetchone()
    if not row:
        raise SystemExit("company not found")
    if a.linkedin_url:
        con.execute("UPDATE companies SET linkedin_url=? WHERE id=?", (a.linkedin_url.split("?")[0].rstrip("/"), row["id"]))
    if a.domain:
        from .normalize import norm_domain
        con.execute("UPDATE companies SET domain=?, domain_source='manual' WHERE id=?", (norm_domain(a.domain), row["id"]))
    con.commit()
    out(dict(con.execute("SELECT id, name, domain, domain_source, linkedin_url FROM companies WHERE id=?",
                         (row["id"],)).fetchone()))


def cmd_pipeline(a, con):
    """Run every stage for one persona/industry (what the web UI's 'Find people' button does)."""
    from . import pipeline
    ind = db.get_industry(con, a.industry)
    opts = json.loads(ind["options_json"] or "{}")
    if a.emails is not None:
        opts["emails"] = a.emails
    if a.model:
        opts["model"] = a.model
    con.close()
    raise SystemExit(pipeline.run(a.industry, opts, a.from_stage))


def cmd_ui(a, con):
    """Local web interface on http://127.0.0.1:<port> (this machine only)."""
    con.close()
    from .web import serve
    serve(a.port)


def cmd_credits(a, con):
    """Remaining free credits per provider (live where the provider has a balance endpoint)."""
    from . import enrich
    month = con.execute("SELECT provider, used FROM credit_usage WHERE month=strftime('%Y-%m','now')").fetchall()
    out({"providers": enrich.balances(), "search": router.status(con),
         "used_this_month_by_tool": {r["provider"]: r["used"] for r in month}})


def cmd_export(a, con):
    from .export import export
    ind = db.get_industry(con, a.industry)
    out(export(con, ind, a.min_shows))


def cmd_sync_tracker(a, con):
    from .export import sync_tracker
    out({"synced": sync_tracker(con, db.get_industry(con, a.industry))})


def cmd_stats(a, con):
    ind = db.get_industry(con, a.industry)
    q = lambda sql: con.execute(sql, (ind["id"],)).fetchone()[0]  # noqa: E731
    out({
        "industry": ind["name"], "icp_approved": bool(ind["icp_approved"]),
        "shows": q("SELECT COUNT(*) FROM shows WHERE industry_id=?"),
        "companies": q("SELECT COUNT(*) FROM company_industries WHERE industry_id=?"),
        "people": q("""SELECT COUNT(*) FROM people p JOIN company_industries ci ON ci.company_id=p.company_id
                       WHERE ci.industry_id=?"""),
        "people_with_icp": q("""SELECT COUNT(*) FROM people p JOIN company_industries ci ON ci.company_id=p.company_id
                                WHERE ci.industry_id=? AND p.icp IS NOT NULL"""),
        "companies_searched": q("""SELECT COUNT(DISTINCT json_extract(context, '$.company_id')) FROM search_queue
                                   WHERE purpose='people' AND status IN ('done','empty')
                                   AND json_extract(context, '$.industry_id')=?"""),
        "companies_with_people": q("""SELECT COUNT(DISTINCT p.company_id) FROM people p
                                      JOIN company_industries ci ON ci.company_id=p.company_id WHERE ci.industry_id=?"""),
        "queue": [dict(r) for r in con.execute(
            "SELECT purpose, status, COUNT(*) n FROM search_queue GROUP BY purpose, status").fetchall()],
    })


def cmd_providers(a, con):
    from . import config
    out({"search": router.status(con),
         "fetch": {"direct": True, "tinyfetch": bool(config.get("TINYFISH_API_KEY")), "openserp": router.OpenSERP().available(),
                   "firecrawl": bool(config.get("FIRECRAWL_API_KEY")), "capture(browser)": True}})


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lf", description=__doc__)
    sp = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *args):
        p = sp.add_parser(name, help=(fn.__doc__ or "").split("\n")[0])
        p.set_defaults(fn=fn)
        for spec in args:
            flags, kw = spec[0], spec[1] if len(spec) > 1 else {}
            p.add_argument(*flags, **kw)
        return p

    IND = (["--industry"], {"required": True})
    add("init-industry", cmd_init_industry, (["--name"], {"required": True}), (["--geo"], {"default": "India + export markets"}))
    add("set-icp", cmd_set_icp, IND, (["--file"], {"required": True}))
    add("approve-icp", cmd_approve_icp, IND)
    add("add-shows", cmd_add_shows, IND, (["--file"], {"required": True}))
    add("list-shows", cmd_list_shows, IND)
    ev = [(["--kind"], {"default": "exhibitor"}), (["--confidence"], {"default": "confirmed"}),
          (["--show"], {"type": int}), (["--url"], {"help": "default evidence URL for rows without one"})]
    add("add-companies", cmd_add_companies, IND, (["--file"], {"required": True}), *ev)
    add("add-people", cmd_add_people, IND, (["--file"], {"required": True}), *ev)
    add("frequent", cmd_frequent, IND, (["--min"], {"type": int, "default": 3}),
        (["--window-days"], {"type": int, "default": 365}), (["--limit"], {"type": int, "default": 200}))
    add("plan-people", cmd_plan_people, IND, (["--icp"], {}), (["--limit"], {"type": int}),
        (["--min-shows"], {"type": int, "default": None}))
    add("plan-titles", cmd_plan_titles, IND, (["--icp"], {"required": True}), (["--sectors"], {"required": True}),
        (["--locations"], {"required": True}), (["--limit"], {"type": int}))
    add("plan-posts", cmd_plan_posts, (["--show"], {"type": int, "required": True}), (["--hashtag"], {"nargs": "*"}))
    add("run-queue", cmd_run_queue, (["--max"], {"type": int, "default": 20}), (["--purpose"], {}),
        (["--retry-empty"], {"action": "store_true", "help": "also retry searches that came back empty"}))
    add("queue", cmd_queue)
    add("queue-links", cmd_queue_links, (["--limit"], {"type": int, "default": 50}))
    add("post-candidates", cmd_post_candidates, (["--show"], {"type": int, "required": True}),
        (["--status"], {"default": "new"}))
    add("mark-posts", cmd_mark_posts, (["--status"], {"required": True}), (["ids"], {"type": int, "nargs": "+"}))
    add("search", cmd_search, (["query"],), (["--site"], {}), (["--num"], {"type": int, "default": 10}),
        (["--region"], {"default": "IN"}))
    add("fetch", cmd_fetch, (["url"],), (["--mode"], {"default": "auto", "choices": ["auto", "direct", "tinyfetch", "openserp", "firecrawl"]}),
        (["--refresh"], {"action": "store_true"}))
    add("capture", cmd_capture, (["url"],), (["--minutes"], {"type": int, "default": 20}), (["--label"], {"default": "page"}))
    add("parse-list", cmd_parse_list, (["file"],), (["--layout"], {"default": "hall-stall", "choices": ["hall-stall"]}),
        (["--section"], {"help": "only pages containing this heading, e.g. \"Exhibitors’ Main List\""}),
        (["--url"], {"help": "evidence URL (default: SOURCE line of the file)"}), (["--out"], {"required": True}))
    add("verify-profiles", cmd_verify_profiles, IND, (["--top"], {"type": int, "default": 50}), (["--icp"], {}),
        (["--recheck"], {"action": "store_true"}))
    add("resolve-domains", cmd_resolve_domains, IND, (["--limit"], {"type": int, "default": 50}),
        (["--redo-guesses"], {"action": "store_true", "help": "re-resolve domains that were search guesses"}))
    add("enrich-emails", cmd_enrich_emails, IND, (["--top"], {"type": int, "default": 30}), (["--icp"], {}),
        (["--include-flagged"], {"action": "store_true", "help": "also enrich leads flagged outside geo / moved"}),
        (["--only"], {"help": "comma list of providers to use, e.g. hunter,prospeo"}))
    add("posts-api", cmd_posts_api, (["--show"], {"type": int, "required": True}),
        (["--query"], {"nargs": "+", "required": True, "help": 'e.g. "IMTEX 2025" "#imtex2025"'}),
        (["--max"], {"type": int, "default": 50}), (["--after"], {"help": "only posts after YYYY-MM-DD"}))
    add("linkedin-people", cmd_linkedin_people, IND, (["--icp"], {"required": True}),
        (["--max"], {"type": int, "default": 150, "help": "max profiles (25 per $0.10 search page)"}),
        (["--companies"], {"type": int, "default": 40}),
        (["--via"], {"default": "auto", "choices": ["auto", "deepline", "apify", "aiark", "brightdata"]}),
        (["--dataset"], {"help": "re-ingest a finished Apify run's dataset id (free) instead of a new run"}),
        (["--seniority"], {"help": "comma list: senior,strategic,manager,director,vp,cxo,owner"}))
    add("company-posts", cmd_company_posts, (["--show"], {"type": int, "required": True}),
        (["--companies"], {"type": int, "default": 15}), (["--per-company"], {"type": int, "default": 40}))
    add("exhibitors-apify", cmd_exhibitors_apify, IND, (["--show"], {"type": int, "required": True}),
        (["--list-url"], {"help": "exhibitor directory URL (saved on the show)"}),
        (["--budget"], {"type": float, "default": 1.0, "help": "USD cap for this show (~$3 per 1,000 exhibitors)"}),
        (["--max"], {"type": int, "help": "max exhibitors (default: what the budget allows)"}),
        (["--minutes"], {"type": int, "default": 30}))
    add("discover-shows", cmd_discover_shows, IND, (["--query"], {"required": True}),
        (["--countries"], {"help": "comma list of ISO codes, e.g. IN,DE,US"}), (["--start"], {}), (["--end"], {}),
        (["--max"], {"type": int, "default": 40}), (["--out"], {"help": "write the candidates to this JSON file"}))
    add("show-people", cmd_show_people, IND, (["--show"], {"type": int, "required": True}),
        (["--budget"], {"type": float, "default": 2.0, "help": "USD in total incl. the actors it calls (min 1.5)"}),
        (["--target"], {"help": "what kind of exhibitors matter, e.g. 'packaging machinery makers'"}),
        (["--role-based"], {"action": "store_true", "help": "also list booth staff found by job title (no evidence)"}),
        (["--run"], {"help": "adopt a finished/running Apify run id instead of starting one"}),
        (["--minutes"], {"type": int, "default": 40}))
    add("set-company", cmd_set_company, (["--id"], {"type": int}), (["--name"], {}), (["--linkedin-url"], {}),
        (["--domain"], {}))
    add("credits", cmd_credits)
    add("pipeline", cmd_pipeline, IND, (["--emails"], {"type": int}), (["--model"], {}),
        (["--from-stage"], {"choices": ["persona", "research", "evidence", "attendees", "linkedin", "web", "verify", "emails", "export", "summary"]}))
    add("ui", cmd_ui, (["--port"], {"type": int, "default": 8787}))
    add("export", cmd_export, IND, (["--min-shows"], {"type": int, "default": None}))
    add("sync-tracker", cmd_sync_tracker, IND)
    add("stats", cmd_stats, IND)
    add("providers", cmd_providers)

    a = ap.parse_args(argv)
    con = db.connect()
    try:
        a.fn(a, con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
