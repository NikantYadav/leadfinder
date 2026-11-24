"""One-click pipeline: persona text -> evidence-backed leads with emails.

Stages (each recorded in pipeline_stages so the web UI can show a checklist; finished stages are skipped on re-run):
  persona   Claude turns the free-text persona into ICPs (titles, keywords, company rule)      [Claude]
  research  Claude finds shows, exhibitor lists and visitor/buyer signals                      [Claude]
  evidence  when few companies meet the 'N+ shows' rule: retry thin exhibitor lists, then look up the top
            companies' own "visit us at" / exhibitions pages for other shows they exhibit at       [Claude]
  attendees LinkedIn posts about the core shows (relevance-sorted, so older editions too), their commenters and
            reactors who fit a persona (showposts.py); sherwood actor only with opts attendee_sherwood
  customers exhibitors' own clients / case-studies pages -> their customers join the company pool (customers.py)
  linkedin  direct LinkedIn people search per ICP (Apify; free plan: 25 profiles/run, 10 runs)
  web       search-engine LinkedIn lookups per company x ICP (TinySearch first)
  verify    LinkedIn profile check of the top leads (country + current company)
  emails    company websites + email waterfall for the top leads
  export    Excel file
  summary   plain-language report of what ran, what was skipped and the bottleneck (Sonnet words code-built facts)
Run: ./lf pipeline --industry <slug> [--emails 25] [--model sonnet] [--from-stage research]
"""
import json
import re
import signal
import subprocess
import sys
import time

from . import config, db, targeting

STAGES = [
    ("persona", "Understand the persona"),
    ("research", "Find trade shows, exhibitors and buyers"),
    ("evidence", "Find more exhibitor evidence"),
    ("attendees", "Find people who posted about or engaged with show posts"),
    ("customers", "Find exhibitors' customers"),
    ("linkedin", "Search LinkedIn"),
    ("web", "Search the web for more people"),
    ("verify", "Check LinkedIn profiles"),
    ("emails", "Find work emails"),
    ("export", "Prepare results"),
    ("summary", "Summary"),
]
CLAUDE_TOOLS = ["Bash(./lf:*)", "Read", "Glob", "Grep", "Write(data/work/**)", "Edit(data/work/**)", "WebSearch", "Skill"]


class NeedsInput(Exception):
    """A stage needs the user before it can continue: the persona step's clarifying questions (detail.questions ->
    status 'questions'), or exhibitor lists only a human can open (detail.help -> status 'help', see showhelp.py)."""
    def __init__(self, detail):
        super().__init__("waiting for your answers")
        self.detail = detail


MAX_QUESTION_ROUNDS = 2   # after this many rounds of answers, the persona step goes with its best reading


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


def clarifications_block(opts):
    qa = [c for c in (opts.get("clarifications") or []) if (c.get("a") or "").strip()]
    if not qa:
        return ""
    lines = "\n".join(f"- Q: {c.get('q') or 'Extra note'}\n  A: {c['a'].strip()}" for c in qa)
    return ("THE USER'S CLARIFICATIONS (answers to earlier questions about this description; they override any guess, "
            "and win over the description where they conflict):\n" + lines + "\n")


def _read_questions(path):
    """Questions Claude wrote to questions.json: [{q, why?, options?}], at most 4, junk dropped."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    items = raw.get("questions") if isinstance(raw, dict) else raw
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            it = {"q": it}
        if not isinstance(it, dict) or not str(it.get("q") or "").strip():
            continue
        opts = [str(o).strip()[:120] for o in (it.get("options") or []) if str(o).strip()][:5]
        out.append({"q": str(it["q"]).strip()[:400], "why": str(it.get("why") or "").strip()[:300], "options": opts})
    return out[:4]


def stage_persona(con, ind, opts):
    slug = ind["slug"]
    qfile = config.DATA / "work" / slug / "questions.json"
    qfile.unlink(missing_ok=True)  # stale questions from an earlier round must not pause this one
    may_ask = not opts.get("no_questions") and int(opts.get("persona_rounds") or 0) < MAX_QUESTION_ROUNDS
    ask = f"""
4. Questions: if something in the description is unclear AND the answer would change WHO you search for (e.g. which
   roles, buyer vs seller side, which industry or company type, company size, what "show" or "event" means), still do
   steps 2-3 with your best reading, then write data/work/{slug}/questions.json as
   {{"questions": [{{"q": "short question", "why": "what changes depending on the answer", "options": ["a", "b"]}}]}}
   with 1-4 questions (options are optional suggested answers). Do NOT ask about anything the description or the
   user's clarifications already settle, nor about minor details you can decide sensibly yourself. If nothing
   essential is unclear, do not write that file.""" if may_ask else """
4. Do not ask questions: go with your best reading of the description and the clarifications."""
    prompt = f"""{HEADLESS}

Task: build the personas for search slug '{slug}' (region: {ind['geo']}) ONLY from the user's description below.
Do not reuse personas, titles or keywords from any other search or file - everything must come from this text.

USER'S DESCRIPTION:
<<<
{ind['persona_text']}
>>>

{clarifications_block(opts)}
{targeting.prompt_block(ind)}

1. Read templates/icp_schema.json - it defines the JSON shape and allowed values only (it has no content to copy).
2. Write data/work/{slug}/icp.json in that shape. One ICP per distinct kind of person in the description (often just
   one), codes P1, P2, ... Decide company_rule and find_via from what the user said (e.g. "visit trade shows" ->
   visitor + company_list; "exhibit at 3+ shows" -> exhibitor_3plus + company_list; no mention of shows ->
   any + industry_search). Always fill linkedin_filters (functions + seniority matching the roles and levels the user
   describes; default seniority manager, director, vp, cxo).
3. Run `./lf set-icp --industry {slug} --file data/work/{slug}/icp.json` then `./lf approve-icp --industry {slug}`
   (the user wrote this description themselves, so it is pre-approved).{ask}"""
    code = _claude(prompt, opts.get("model"), opts.get("effort"))
    questions = _read_questions(qfile) if may_ask else []
    ind2 = db.get_industry(con, slug)
    icps = []
    if ind2["icp_json"]:
        # enforce the form's targeting whatever Claude wrote
        doc = targeting.apply(json.loads(ind2["icp_json"]), ind2)
        con.execute("UPDATE industries SET icp_json=?, icp_approved=1 WHERE id=?", (json.dumps(doc), ind["id"]))
        con.commit()
        icps = [f'{i["code"]}: {i["name"]}' for i in doc["icps"] if i.get("enabled", True)]
    if questions:
        # pause: the user answers (persona runs again with the answers) or goes with the best guess in icp_json
        raise NeedsInput({"questions": questions, "icps": icps, "round": int(opts.get("persona_rounds") or 0) + 1})
    if code != 0 or not icps:
        raise RuntimeError("Claude did not produce personas")
    return {"icps": icps}


def stage_research(con, ind, opts):
    slug = ind["slug"]
    icps = json.loads(db.get_industry(con, slug)["icp_json"])["icps"]
    where = ", ".join(targeting.get(ind).get("countries") or [ind["geo"] or "India"])
    cities = targeting.get(ind).get("cities")
    region_note = (f" Region: {where}" + (f" (focus: {', '.join(cities)})" if cities else "") +
                   " - pick shows held in, or drawing buyers from, this region.")
    from .people import requested_min_shows
    n_shows = requested_min_shows(db.get_industry(con, slug))
    overlap = (f"get exhibitor lists for AT LEAST {n_shows + 1} shows - the '{n_shows}+ shows a year' rule needs "
               "overlap - and " if n_shows > 1 else "")
    needs_shows = any(i.get("find_via", "company_list") == "company_list" for i in icps if i.get("enabled", True))
    if not needs_shows:
        prompt = (f"/find-leads (continue industry slug '{slug}'; personas approved)\n\n{HEADLESS} This persona is found "
                  "by title x sector x city, not via shows: there is nothing to research. Reply 'no research needed'.")
    else:
        prompt = (f"/find-leads (continue industry slug '{slug}'; personas approved)\n\n{HEADLESS} Do ONLY: Step 2 (4-6 "
                  "most relevant shows, exact dates)" + region_note + " Shows outside the region are fine when the persona's "
                  "companies exhibit or buy there. Record each show's exhibitor directory URL as exhibitor_list_url; "
                  "when choosing between similar shows prefer ones whose directory is on Map Your Show, a2z, ExpoFP or "
                  "Swapcard: a later step reads those automatically, so you need not copy them. Step 3a (exhibitor lists for the other shows; use "
                  "`./lf parse-list` for fair-guide PDFs; " + overlap + "name in your summary any show whose list you could not get), Step 3b (visitor/buyer signals: post-show reports, order news, delegations, buyer-seller "
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
    """If too few companies meet the persona's 'N+ shows' rule (N as the user asked, default 3), relax to N-1, ... 1,
    and remember the value used for this search (options_json.min_shows). The requested N is never overwritten."""
    from .people import companies_for_icp, load_icps, requested_min_shows
    wanted = requested_min_shows(ind)
    used = wanted
    for icp in load_icps(ind):
        if icp.get("company_rule") == "exhibitor_3plus":
            comps, m = companies_for_icp(con, ind, icp, wanted)
            used = min(used, m) if comps else used
    opts = json.loads(ind["options_json"] or "{}")
    if opts.get("min_shows") != used:
        opts["min_shows"] = used
        con.execute("UPDATE industries SET options_json=? WHERE id=?", (json.dumps(opts), ind["id"]))
        con.commit()
    return used


EVIDENCE_TARGET_COMPANIES = 25   # enough companies at N+ shows: no need to dig for more evidence
THIN_LIST = 30                   # a show with fewer exhibitors than this probably had its list missed


def show_coverage(con, ind):
    """Per show: exhibitors recorded. Used by the evidence step and the summary."""
    return [dict(r) for r in con.execute(
        """SELECT s.id, s.name, s.series, s.year, s.start_date, s.exhibitor_list_url,
                  (SELECT COUNT(DISTINCT e.entity_id) FROM evidence e WHERE e.show_id=s.id AND e.kind='exhibitor'
                   AND e.entity='company') exhibitors
           FROM shows s WHERE s.industry_id=? ORDER BY s.start_date""", (ind["id"],)).fetchall()]


def shows_distribution(con, ind):
    """{shows in the best 12-month window: number of companies}, e.g. {0: 32, 1: 635, 2: 3, 3: 3}."""
    from .people import company_facts
    dist = {}
    for (cid,) in con.execute("SELECT company_id FROM company_industries WHERE industry_id=?", (ind["id"],)):
        n = company_facts(con, cid)["exhibitor_window_count"]
        dist[n] = dist.get(n, 0) + 1
    return dict(sorted(dist.items()))


def _at_least(dist, n):
    return sum(v for k, v in dist.items() if k >= n)


APIFY_CATALOG_USD = 2.0  # per search: exhibitor catalogs read through Apify (opts apify_catalog_usd); ~$3 per 1,000


def _apify_catalogs(con, ind, opts):
    """Read the thin shows' exhibitor catalogs with Apify actors before asking Claude: shows matched by fair name
    (interpack, EuroBLECH, Hannover Messe, ...) or by a supported directory URL (Map Your Show, ExpoFP, a2z, Swapcard,
    ...). Other shows (custom sites, PDFs) are left to Claude. Each started run is saved, so a resumed stage adopts it."""
    from . import showdata
    from .providers import events as ev
    if not ev.available():
        return {"skipped": "no Apify token"}
    budget = float(opts.get("apify_catalog_usd", APIFY_CATALOG_USD))
    prog = dict(_progress(con, ind["id"], "evidence").get("catalogs") or {})
    paid = ev.paid_plan()
    shows = {r["id"]: dict(r) for r in con.execute("SELECT * FROM shows WHERE industry_id=?", (ind["id"],))}
    todo = [s for s in show_coverage(con, ind) if s["exhibitors"] < THIN_LIST
            and not (prog.get(str(s["id"])) or {}).get("finished")
            and ev.route(shows[s["id"]], allow_capped=paid)]
    spent = sum((v or {}).get("cost", 0) for v in prog.values())
    out = {}
    for i, s in enumerate(todo):
        left = budget - spent
        if left < 0.1:
            out[s["name"]] = "not read: Apify catalog budget used up"
            continue
        key = str(s["id"])
        saved = prog.get(key) or {}

        def started(ref, key=key):
            prog[key] = {"ref": ref}
            _save_progress(con, ind["id"], "evidence", {"catalogs": prog})
        res = showdata.harvest_exhibitors(con, ind, shows[s["id"]], left / (len(todo) - i), deadline=time.time() + 20 * 60,
                                          log=log, ref=saved.get("ref"), on_start=started, allow_capped=False)
        spent += res.get("cost", 0)
        prog[key] = {"finished": True, "cost": res.get("cost", 0), "added": res.get("added", 0)}
        _save_progress(con, ind["id"], "evidence", {"catalogs": prog})
        out[s["name"]] = f'{res["added"]} exhibitors via {res["actor"].split("~")[0]}' if res.get("added") \
            else f'not read: {res.get("error")}'
    return {"catalogs": out, "apify_usd": round(spent, 2)} if out else {}


FIRECRAWL_MAPS = 8          # thin core shows whose site is mapped per run (opts firecrawl_maps), 1 credit each
FIRECRAWL_RUN_CREDITS = 40  # Firecrawl credits one pipeline run may use (opts firecrawl_credits); free plan: 1,000/month
FIRECRAWL_APIFY_USD = 1.0   # Apify credit for directories the site map turned up (opts firecrawl_apify_usd)
PLATFORM_WORDS = ("swapcard", "mapyourshow", "map your show", "expofp", "a2zinc", "a2z", "personify")


def _firecrawl_lists(con, ind, opts):
    """Thin core shows: map the show's site with Firecrawl (1 credit, every link on the site in one call), rank the
    exhibitor list / directory / catalogue links of THIS edition (other years are only reported), and read the best
    two through showhelp.read_url: Apify catalog readers for directory platforms, PDF parser, Claude reading the page,
    a scrolling browser render. Each show is mapped once per search (progress saved)."""
    from . import showhelp
    from .providers import events as ev
    from .providers import firecrawl as fc
    if not fc.available():
        return {}
    prog = dict(_progress(con, ind["id"], "evidence").get("lists") or {})
    shows = {r["id"]: dict(r) for r in con.execute(
        "SELECT * FROM shows WHERE industry_id=? AND COALESCE(origin, '') != 'evidence'", (ind["id"],))}
    todo = [s for s in show_coverage(con, ind) if s["exhibitors"] < THIN_LIST and s["id"] in shows
            and str(s["id"]) not in prog and (shows[s["id"]]["url"] or shows[s["id"]]["exhibitor_list_url"])]
    apify_left = float(opts.get("firecrawl_apify_usd", FIRECRAWL_APIFY_USD))
    out, maps = {}, 0
    for s in todo[:int(opts.get("firecrawl_maps", FIRECRAWL_MAPS))]:
        show = shows[s["id"]]
        site = show["url"] or show["exhibitor_list_url"]
        try:
            links = fc.map_site(site, "exhibitor")
        except fc.OutOfBudget as e:
            out[s["name"]] = f"not mapped: {e}"
            break
        except Exception as e:  # noqa: BLE001 - one show's site failing must not stop the others
            prog[str(s["id"])] = {"error": str(e)[:200]}
            out[s["name"]] = f"site map failed: {str(e)[:120]}"
            continue
        maps += 1
        ranked, other = fc.rank_list_links(links, show)
        entry = {"candidates": [r["url"] for r in ranked[:5]], "other_editions": [o["url"] for o in other[:5]]}
        unconfirmed = [r["url"] for r in ranked if not r["confirmed"]]
        if unconfirmed:
            entry["unconfirmed"] = unconfirmed[:3]
        for cand in [r for r in ranked if r["confirmed"]][:2]:   # unconfirmed edition: the user decides
            # Apify only for directory platforms (Swapcard, MYS, ExpoFP, a2z - often on the show's own subdomain, the
            # page title names the platform) and known fair sites; plain pages and PDFs are read for free
            platform = cand["directory"] or any(w in f'{cand["url"]} {cand["title"]}'.lower() for w in PLATFORM_WORDS)
            routed = any(sp["actor"] != ev.TIDY for sp, _ in ev.route({"name": "", "exhibitor_list_url": cand["url"]}))
            use_apify = (platform or routed) and not cand["pdf"] and apify_left >= 0.3
            log(f"  {s['name']}: reading {cand['url']} ({cand['why']})")
            res = showhelp.read_url(con, ind, show, cand["url"], budget=min(0.5, apify_left), log=log,
                                    apify=use_apify)
            apify_left -= res.get("cost", 0) or 0
            if res.get("added"):
                entry["read"] = {"url": cand["url"], "added": res["added"], "via": res.get("via")}
                break
        prog[str(s["id"])] = entry
        _save_progress(con, ind["id"], "evidence", {"lists": prog})
        if entry.get("read"):
            out[s["name"]] = f'{entry["read"]["added"]} exhibitors from {entry["read"]["url"]} ({entry["read"]["via"]})'
        elif ranked and len(unconfirmed) == len(ranked):
            out[s["name"]] = "not read: the show site has moved on to a later edition; its undated list link(s) may " \
                             "not be this year's: " + ", ".join(unconfirmed[:2])
        elif ranked:
            out[s["name"]] = f'not read: {len(ranked)} list link(s) on the show site, none readable: ' \
                             + ", ".join(entry["candidates"][:3])
        else:
            out[s["name"]] = "not read: no exhibitor-list link for this edition on the show site" + \
                (f' (other editions: {", ".join(entry["other_editions"][:2])})' if other else "")
    links = {shows[int(k)]["name"]: v["candidates"][:3] for k, v in prog.items()
             if int(k) in shows and v.get("candidates") and not v.get("read")}
    return {"firecrawl_lists": out, "firecrawl_maps": maps, **({"list_links": links} if links else {})} if out else {}


def _list_hints(con, ind):
    """'show name -> candidate links' from this run's site maps, for Claude's prompt and the help card."""
    prog = _progress(con, ind["id"], "evidence").get("lists") or {}
    return {int(k): v for k, v in prog.items() if v.get("candidates") or v.get("other_editions")}


def stage_evidence(con, ind, opts):
    from .people import company_facts, load_icps, requested_min_shows
    ind = db.get_industry(con, ind["slug"])
    if not any(i.get("company_rule") == "exhibitor_3plus" for i in load_icps(ind)):
        return {"skipped": "this persona has no 'exhibits at N+ shows' rule"}
    n = requested_min_shows(ind)
    if n < 2:
        return {"skipped": "this persona asks for no minimum number of shows"}
    target = int(opts.get("evidence_target", EVIDENCE_TARGET_COMPANIES))
    before = shows_distribution(con, ind)
    if _at_least(before, n) >= target:
        return {"skipped": f"{_at_least(before, n)} companies already exhibit at {n}+ shows", "min_shows_wanted": n,
                "companies_at_n_before": _at_least(before, n)}
    lists = _firecrawl_lists(con, ind, opts)
    hints = _list_hints(con, ind)
    apify = {**lists, **_apify_catalogs(con, ind, opts)}
    mid = shows_distribution(con, ind)
    if _at_least(mid, n) >= target:
        return {**apify, "min_shows_wanted": n, "companies_at_n_before": _at_least(before, n),
                "companies_at_n_after": _at_least(mid, n), "distribution_after": {str(k): v for k, v in mid.items()}}
    shows = show_coverage(con, ind)
    thin = [s for s in shows if s["exhibitors"] < THIN_LIST]
    comps = con.execute("""SELECT c.id, c.name, c.domain FROM companies c JOIN company_industries ci
                           ON ci.company_id=c.id WHERE ci.industry_id=?""", (ind["id"],)).fetchall()
    facts = {c["id"]: company_facts(con, c["id"]) for c in comps}
    # closest to the bar first: 2-show companies, then 1-show companies that also have buyer evidence
    top = sorted([c for c in comps if facts[c["id"]]["exhibitor_shows"]],
                 key=lambda c: (-facts[c["id"]]["exhibitor_window_count"], -facts[c["id"]]["visitor_evidence"], c["id"]))
    top = top[:int(opts.get("evidence_companies", 30))]
    slug = ind["slug"]
    show_lines = "\n".join(f'  show_id={s["id"]} | {s["name"]} | series={s["series"]} | {s["start_date"]} | '
                            f'{s["exhibitors"]} exhibitors recorded | list url: {s["exhibitor_list_url"] or "-"}'
                            + (f'\n      links on its site (already tried, could not be read): '
                               f'{", ".join(u for u in hints[s["id"]]["candidates"][:3] if u not in hints[s["id"]].get("unconfirmed", []))}'
                               if set(hints.get(s["id"], {}).get("candidates") or []) - set(hints.get(s["id"], {}).get("unconfirmed") or []) else "")
                            + (f'\n      undated list links, but the site already shows a later edition (import only if the page '
                               f'proves it is this year\'s list): {", ".join(hints[s["id"]]["unconfirmed"])}'
                               if hints.get(s["id"], {}).get("unconfirmed") else "")
                            + (f'\n      list links of OTHER editions (only for a show row of that year): '
                               f'{", ".join(hints[s["id"]]["other_editions"][:3])}'
                               if hints.get(s["id"], {}).get("other_editions") else "")
                            for s in shows)
    comp_lines = "\n".join(f'  {c["name"]}' + (f' ({c["domain"]})' if c["domain"] else "") + " | exhibits at: "
                            + ", ".join(x["name"] for x in facts[c["id"]]["exhibitor_shows"]) for c in top)
    prompt = f"""/find-leads (continue industry slug '{slug}'; personas approved)

{HEADLESS}

Goal: more companies must be PROVEN to exhibit at {n}+ trade shows within one 12-month window (the persona's rule).
Right now only {_at_least(mid, n)} do. Companies per number of shows (best 12 months): {mid}.
Most companies show up at just one show because exhibitor lists for the other shows are missing. Work in this order:

PART 1 - missing exhibitor lists (highest yield: one list adds hundreds of company/show links).
Shows on record (exhibitors recorded so far):
{show_lines}
For every show with fewer than {THIN_LIST} exhibitors, get its exhibitor list (Step 3a of the skill: `./lf fetch`
the list URL or find one; fair-guide / show-daily / catalogue PDFs via `./lf search '"<show>" exhibitor list OR
"fair guide" OR catalogue pdf'`; `./lf parse-list` for Hall/Stall layouts). Import with
`./lf add-companies --industry {slug} --show <show_id> --kind exhibitor --confidence confirmed --file ...`.
Shows to retry: {", ".join(s["name"] for s in thin) or "none"}.
If you find a show's online exhibitor directory on Map Your Show, a2z, ExpoFP or Swapcard, read it with
`./lf exhibitors-apify --industry {slug} --show <show_id> --list-url <directory URL> --budget 0.5` instead of
fetching page by page (paid Apify credit: at most 2 such calls in this step).

PART 2 - other shows the best companies exhibit at (only with search budget left after part 1).
These companies are closest to the bar:
{comp_lines}
For each, look for proof of exhibiting at OTHER shows: the company's own "events / exhibitions / meet us at" page,
"visit us at <show> Hall X Stall Y" news or posts, exhibitor directories. For a company with a domain,
`./lf site-map <domain> --events` lists its events / exhibitions / news pages in one call (1 Firecrawl credit; at most
{int(opts.get("evidence_site_maps", 12))} such calls, and only when a search did not already find the page); then `./lf fetch` the best one. Rules:
- Reuse a show from the list above when it is the same event (same series and year) - use its show_id. Never add a
  second row for an event that is already there under another spelling.
- A new show needs exact start_date/end_date (the rule counts shows within 12 months of each other); add it with
  `./lf add-shows --industry {slug} --file data/work/{slug}/more_shows.json` (fields: name, series, year, start_date,
  end_date, city, country, url, source_url), then `./lf list-shows --industry {slug}` to get its id.
- Write company rows with the company name EXACTLY as listed above, so they attach to the existing company, and the
  proof page as url. Only shows held within 12 months of the company's other shows help.

Budget: about {int(opts.get("evidence_searches", 40))} searches in total. Do NOT run people searches, LinkedIn, email or
export commands. In your summary give: lists retried (got / not got and why), companies checked, new show links added."""
    known = {r[0] for r in con.execute("SELECT id FROM shows WHERE industry_id=?", (ind["id"],))}
    code = _claude(prompt, opts.get("model"), opts.get("effort"))
    # shows added here are the companies' OTHER shows (often another industry's fair): they count toward the
    # N-shows rule but are not core shows of this search, so the attendee step leaves them out
    con.execute(f"""UPDATE shows SET origin='evidence' WHERE industry_id=? AND origin IS NULL
                    AND id NOT IN ({",".join("?" * len(known)) or "NULL"})""", (ind["id"], *known))
    con.commit()
    after = shows_distribution(con, db.get_industry(con, slug))
    out = {**apify, "min_shows_wanted": n, "companies_at_n_before": _at_least(before, n),
           "companies_at_n_after": _at_least(after, n), "distribution_after": {str(k): v for k, v in after.items()},
           "thin_shows_before": [s["name"] for s in thin],
           "thin_shows_after": [s["name"] for s in show_coverage(con, ind) if s["exhibitors"] < THIN_LIST]}
    if code != 0:
        raise RuntimeError("Claude evidence step failed")
    if thin and not opts.get("no_help"):
        # core shows whose lists neither Apify nor Claude could read: ask the human before people search spends
        # credits on an incomplete company pool. The result rides along, so Continue marks this step done with it.
        from . import showhelp
        need = showhelp.needing_help(con, db.get_industry(con, slug), core_names=[s["name"] for s in thin])
        if need:
            raise NeedsInput({"help": need, "result": out})
    return out


ATTENDEE_SHOW_USD = 1.5   # per show (opts attendee_usd); below this the actor's sub-run minimums eat the budget
ATTENDEE_MAX_SHOWS = 2    # opts attendee_shows
ATTENDEE_DAYS_BEFORE, ATTENDEE_DAYS_AFTER = 75, 45  # LinkedIn posts are read newest first: only recent shows yield


def _show_key(text):
    """'PETROTECH 2026' / 'ADIPEC-2026' -> 'petrotech' / 'adipec': lower case, no years, single spaces."""
    t = re.sub(r"\b(19|20)\d\d\b", " ", (text or "").lower())
    return " ".join(re.sub(r"[^a-z0-9&]+", " ", t).split())


def asked_for_shows(ind, shows):
    """Ids of shows the user named in the description, a clarification answer or the trade-show targeting field."""
    try:
        opts = json.loads(ind["options_json"] or "{}")
    except (TypeError, ValueError):
        opts = {}
    said = " ".join([ind["persona_text"] or "", str((opts.get("target") or {}).get("company_rule") or "")]
                    + [c.get("a") or "" for c in opts.get("clarifications") or []])
    said = f" {_show_key(said)} "
    out = set()
    for s in shows:
        for k in {_show_key(s["name"]), _show_key(s["series"])}:
            if len(k) >= 4 and f" {k} " in said:
                out.add(s["id"])
    return out


def recent_shows(con, ind, today=None, before=ATTENDEE_DAYS_BEFORE, after=ATTENDEE_DAYS_AFTER):
    """Shows to read attendees from. Relevance first: only core shows of this search, never the companies' other
    shows the evidence step added (often another industry's fair, e.g. a pharma show an oil & gas supplier also
    attends). Then date: ended within ~75 days or starting within ~45 days, so their LinkedIn posts are still among
    the newest. Shows running today are left out (their posts are still coming in and a read is never repeated)
    unless the user named the show; a show the user named also goes first, even one the evidence step added.
    Otherwise closest first."""
    from datetime import date, timedelta
    t = today or date.today()
    lo, hi = (t - timedelta(days=before)).isoformat(), (t + timedelta(days=after)).isoformat()
    rows = [dict(r) for r in con.execute(
        """SELECT * FROM shows WHERE industry_id=? AND start_date IS NOT NULL
           AND COALESCE(end_date, start_date) >= ? AND start_date <= ?""", (ind["id"], lo, hi))]
    asked = asked_for_shows(ind, rows)
    ts = t.isoformat()
    running = lambda s: s["start_date"][:10] <= ts <= (s["end_date"] or s["start_date"])[:10]
    rows = [s for s in rows if s["id"] in asked or (s["origin"] != "evidence" and not running(s))]
    return sorted(rows, key=lambda s: (s["id"] not in asked, abs((date.fromisoformat(s["start_date"][:10]) - t).days)))


POST_SHOWS = 3            # opts attendee_post_shows: core shows whose LinkedIn posts are read per run
POST_SHOW_USD = 0.80      # opts attendee_post_usd: per show (post search ~$0.25, the rest commenters / reactors)
POST_DAYS_BEFORE = 730    # opts attendee_post_days: relevance-sorted post search reached a 22-month-old show (IMTEX 2025)


def stage_attendees(con, ind, opts):
    """LinkedIn posts about this search's core shows (showposts.py): authors, commenters and reactors who fit a
    persona. Visit comments / visit posts make a person 'seen'; plain likes and comments are post_engaged (weaker).
    The sherwood actor (newest posts only, $1.50 a show, measured 0 and 4 vendor people) runs only when
    opts attendee_sherwood is set. Paid Apify credit; every run is capped and cached per show."""
    from . import showposts
    from .providers import events as ev
    if not ev.available():
        return {"skipped": "no Apify token"}
    before = int(opts.get("attendee_post_days", POST_DAYS_BEFORE))
    today = time.strftime("%Y-%m-%d")
    asked = asked_for_shows(ind, recent_shows(con, ind, before=before))
    # shows the user named first, then shows that already ended (their "thanks for visiting" posts and comments are
    # in), closest first; upcoming shows last (only invitations so far)
    shows = sorted(recent_shows(con, ind, before=before),
                   key=lambda s: (s["id"] not in asked, s["start_date"][:10] > today))
    shows = shows[:int(opts.get("attendee_post_shows", POST_SHOWS))]
    per_show = float(opts.get("attendee_post_usd", POST_SHOW_USD))
    prog = dict(_progress(con, ind["id"], "attendees"))
    out, spent = {}, 0.0
    for s in shows:
        key = f"posts:{s['id']}"
        if (prog.get(key) or {}).get("finished"):
            out[s["name"]] = prog[key].get("summary")
            continue

        def started(refs, key=key):
            prog[key] = {"refs": refs}
            _save_progress(con, ind["id"], "attendees", prog)
        try:
            res = showposts.read_show(con, ind, s, per_show, time.time() + 30 * 60, log=log,
                                      refs=(prog.get(key) or {}).get("refs"), on_start=started)
        except Exception as e:  # noqa: BLE001 - one show failing must not stop the others
            res = {"error": str(e)[:200]}
        spent += res.get("cost") or 0
        other = res.get("posts_for_other_editions") or 0
        summary = (f'{res.get("posts_in_window", 0)} posts' + (f' (+{other} about other shows of this search)' if other
                                                                else "")
                   + f' · {res.get("seen", 0)} people seen at a show · {res.get("engaged", 0)} engaged with the posts'
                   + (f' ({res["warning"]})' if res.get("warning") else "")
                   if "error" not in res else f'not read: {res["error"]}')
        prog[key] = {"finished": True, "summary": summary}
        _save_progress(con, ind["id"], "attendees", prog)
        out[s["name"]] = summary
    if opts.get("attendee_sherwood"):
        out.update(_sherwood(con, ind, opts, prog))
    if not out:
        return {"skipped": f"no core show in the last {before // 30} months or the next {ATTENDEE_DAYS_AFTER} days "
                           "(shows running now are read after they end)"}
    return {"shows": out, "cost_usd": round(spent, 2)}


def _sherwood(con, ind, opts, prog):
    """People who posted or commented about a recent show (Apify sherwood actor): visitors go in as visitor_post
    evidence, exhibitor staff as exhibitor evidence. Reads the newest posts only, so recent shows only."""
    from . import showdata
    out = {}
    shows = recent_shows(con, ind)[:int(opts.get("attendee_shows", ATTENDEE_MAX_SHOWS))]
    per_show = float(opts.get("attendee_usd", ATTENDEE_SHOW_USD))
    doc = json.loads(db.get_industry(con, ind["slug"])["icp_json"] or "{}")
    target = ", ".join((doc.get("sectors") or [])[:3]) or ind["name"]
    for s in shows:
        key = str(s["id"])
        if (prog.get(key) or {}).get("finished"):
            out[s["name"] + " (sherwood)"] = prog[key].get("summary")
            continue

        def started(ref, key=key):
            prog[key] = {"ref": ref}
            _save_progress(con, ind["id"], "attendees", prog)
        try:
            res = showdata.show_people(con, ind, s, per_show, target, time.time() + 40 * 60, log=log,
                                       ref=(prog.get(key) or {}).get("ref"), on_start=started)
        except Exception as e:  # noqa: BLE001
            res = {"error": str(e)[:200]}
        summary = (f'{res.get("people", 0)} people ({res.get("visitors", 0)} visitors)' if "people" in res
                   else f'not read: {res.get("error")}')
        prog[key] = {"finished": True, "summary": summary}
        _save_progress(con, ind["id"], "attendees", prog)
        out[s["name"] + " (sherwood)"] = summary
    return out


CUSTOMER_EXHIBITORS = 20  # opts customer_companies: new exhibitor websites read per run
BUYER_RULES = ("visitor", "visitor_or_exhibitor")


def stage_customers(con, ind, opts):
    """Exhibitors' clients / customers pages -> customer companies (customer_of_exhibitor, inferred) in this search's
    company pool, so the people steps look for buyers there. Only for personas that buy at shows."""
    from . import customers
    from .people import load_icps
    icps = load_icps(db.get_industry(con, ind["slug"]))
    if not any(i.get("company_rule") in BUYER_RULES and i.get("find_via", "company_list") == "company_list"
               for i in icps):
        return {"skipped": "no target group buys at trade shows (only sellers / exhibitors are wanted)"}
    res = customers.run(con, ind, int(opts.get("customer_companies", CUSTOMER_EXHIBITORS)), log=log,
                        deadline=time.time() + 25 * 60)
    return res


def stage_linkedin(con, ind, opts):
    from . import enrich
    from .people import load_icps, plan_title_searches, run_queue
    from . import keys
    from .providers import deepline as dlp
    from .providers import linkedin as lip
    ind = db.get_industry(con, ind["slug"])
    m = _decide_min_shows(con, ind)
    ind = db.get_industry(con, ind["slug"])
    out = {}
    prev = _progress(con, ind["id"], "linkedin")
    prev_done = {k: v for k, v in prev.items()
                 if isinstance(v, dict) and not v.get("error") and not v.get("skipped") and not v.get("in_flight")}
    wanted = __import__("leadfinder.people", fromlist=["x"]).requested_min_shows(ind)
    if m < wanted:
        out["note"] = (f"Too few companies exhibited at {wanted}+ of the shows found, so companies at {m}+ show"
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
        has_credit = lip.apify_available() and any(lip.apify_has_credit(k) for k in keys.usable("apify"))
        # an Apify run started before a Stop keeps running on Apify's side: reuse its results instead of paying again
        flight = (prev.get(icp["code"]) or {}).get("in_flight") if isinstance(prev.get(icp["code"]), dict) else None
        ds = lip.finished_run_dataset(flight) if flight and lip.apify_available() else None
        dl, only = None, None
        if not ds and dlp.search_available():  # free: always first
            log(f"LinkedIn search for ICP {icp['code']} (Deepline, free)")
            # free, so room for ~3 people at each of the 40 companies (a smaller cap would leave the
            # later companies unsearched and send them to the paid sources)
            dl = enrich.linkedin_people(con, ind, icp["code"], max_items=120, log=log, via="deepline",
                                        companies_limit=40)
            if dl.get("people_added_or_matched"):
                only = dl.get("companies_without_results") or []
                if only:
                    log(f"  Deepline had nobody at {len(only)} companies; trying the paid sources for those")
        paid = None
        if only == []:
            pass  # Deepline covered every company
        elif ds:
            log(f"LinkedIn search for ICP {icp['code']}: reusing the Apify run that finished after the Stop")
            paid = enrich.linkedin_people(con, ind, icp["code"], max_items=25, log=log, via="apify",
                                          companies_limit=40, dataset_id=ds)
        elif has_credit:
            log(f"LinkedIn search for ICP {icp['code']} (Apify)")
            _save_progress(con, ind["id"], "linkedin", {**out, icp["code"]: {"in_flight": db.now()}})
            paid = enrich.linkedin_people(con, ind, icp["code"], max_items=25, log=log, via="apify",
                                          companies_limit=40, only_companies=only)
            missed = (paid or {}).get("unresolved_companies") or []
            if missed and lip.aiark_search_available():
                log(f"  {len(missed)} companies have no LinkedIn page Apify can use; trying AI Ark by name")
                paid["aiark_for_unresolved"] = enrich.linkedin_people(
                    con, ind, icp["code"], max_items=25, log=log, via="aiark", only_companies=missed)
        elif lip.aiark_search_available():
            log(f"LinkedIn search for ICP {icp['code']} (AI Ark)")
            paid = enrich.linkedin_people(con, ind, icp["code"], max_items=50, log=log, via="aiark",
                                          companies_limit=60, only_companies=only)
        elif lip.brightdata_search_available():
            paid = enrich.linkedin_people(con, ind, icp["code"], max_items=100, log=log, via="brightdata",
                                          only_companies=only)
        elif not (dl and dl.get("people_added_or_matched")):
            why = "Deepline found nobody; no Apify or AI Ark credit on any key"
            if lip.brightdata_available():
                why += f"; Bright Data people search not enabled ({lip.BD_SEARCH_HOWTO})"
            paid = {"skipped": f"no LinkedIn search credits left ({why})"}
        if dl and dl.get("people_added_or_matched"):
            out[icp["code"]] = dl
            if paid:
                dl["paid_for_rest"] = paid
        else:
            out[icp["code"]] = paid or dl or {"skipped": "no LinkedIn search source available"}
            if dl and isinstance(out[icp["code"]], dict) and out[icp["code"]] is not dl:
                out[icp["code"]]["deepline"] = dl  # tried first, found nobody
        _save_progress(con, ind["id"], "linkedin", out)
    return out


def stage_web(con, ind, opts):
    """Company x ICP LinkedIn lookups, best companies first. Drains the whole queue (not a fixed ~60) within a time
    budget; unfinished work stays queued and a Resume continues it."""
    from .people import plan_people_searches, run_queue
    ind = db.get_industry(con, ind["slug"])
    cap = int(opts.get("web_companies", 300))
    planned = plan_people_searches(con, ind, max_companies=cap or None)
    log(f"web people searches planned: {planned}")
    deadline = time.time() + 60 * float(opts.get("web_minutes", 45))
    total = {"done": 0, "people_added": 0, **planned}
    while True:
        r = run_queue(con, max_items=20, log=lambda m: print(m, flush=True), industry_id=ind["id"], deadline=deadline)
        total["done"] += r["done"]
        total["people_added"] += r["people_added"]
        total["pending"] = r["pending"]
        if r["stopped"] or not r["pending"]:
            total["stopped"] = r.get("stopped")
            break
    # coverage, so "N of M companies" is visible (most skips come from the persona's company rule, e.g. 2+ shows)
    from .people import effective_min_shows
    total["companies_total"] = con.execute("SELECT COUNT(*) FROM company_industries WHERE industry_id=?",
                                           (ind["id"],)).fetchone()[0]
    total["companies_searched"] = con.execute(
        """SELECT COUNT(DISTINCT json_extract(context, '$.company_id')) FROM search_queue WHERE purpose='people'
           AND status IN ('done','empty') AND json_extract(context, '$.industry_id')=?""", (ind["id"],)).fetchone()[0]
    total["min_shows"] = effective_min_shows(ind)
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


RULE_WORDS = {
    None: "no company requirement", "": "no company requirement", "any": "no company requirement",
    "exhibitor": "company must have exhibited at a trade show",
    "visitor": "company must have visited / bought at a trade show",
    "visitor_or_exhibitor": "company must have exhibited at or visited a trade show",
}
FOUND_WORDS = {"company_list": "searched company by company (companies taken from trade-show lists)",
               "industry_search": "searched by job title x sector x location (not through show lists)"}


def run_facts(con, ind):
    """Everything the summary may say, computed here (Sonnet only puts it into words, it never invents numbers).
    Facts that don't apply to this persona are left out, so the summary can't present them as a limit."""
    from . import keys
    from .people import company_facts, company_rule_ok, effective_min_shows, load_icps, requested_min_shows
    ind = db.get_industry(con, ind["slug"])
    q = lambda sql, *a: con.execute(sql, (ind["id"], *a)).fetchone()[0]  # noqa: E731
    icps = load_icps(ind)
    opts = json.loads(ind["options_json"] or "{}")
    asked = requested_min_shows(ind)
    has_count_rule = asked > 0 and any(i.get("company_rule") == "exhibitor_3plus" for i in icps)
    uses_shows = any(i.get("company_rule") not in (None, "", "any") for i in icps)
    by_company = any(i.get("find_via", "company_list") == "company_list" for i in icps)
    m = effective_min_shows(ind)
    comp_ids = [r[0] for r in con.execute("SELECT company_id FROM company_industries WHERE industry_id=?", (ind["id"],))]
    facts = {c: company_facts(con, c) for c in comp_ids} if uses_shows else {}

    steps = []
    for key, label in STAGES:
        if key == "summary":
            continue
        r = con.execute("SELECT status, detail FROM pipeline_stages WHERE industry_id=? AND stage=?",
                        (ind["id"], key)).fetchone()
        d = json.loads(r["detail"]) if r and r["detail"] else {}
        status = r["status"] if r else "not run"
        for k in ("partial", "seconds", "in_flight"):
            d.pop(k, None)
        if status == "skipped" or (key == "emails" and int(opts.get("emails", 25)) <= 0):
            d = {"skipped": "turned off by you"}
        elif key == "research" and not by_company:
            d = {"note": "not needed: this persona is " + FOUND_WORDS["industry_search"]}
        elif key == "web":
            d["people_found_before_persona_check"] = d.pop("people_added", None)
            for k in ("planned", "skipped_company_rule", "companies_considered", "stopped"):
                d.pop(k, None)
            if not has_count_rule:
                d.pop("min_shows", None)
            if not by_company:
                d.pop("companies_searched", None)
                d.pop("companies_total", None)
            elif d.get("companies_total") is not None:
                # one unambiguous phrase: the model misread two bare numbers as "searched all companies"
                d["coverage"] = (f"searched {d.pop('companies_searched', 0) or 0} of the {d.pop('companies_total')} "
                                 "companies (only companies meeting the target group's company requirement are searched)")
        elif key == "linkedin" and not by_company:
            d = {"note": "planned the job-title searches that the web step then ran"}
        elif key == "linkedin":
            # totals per target group, added up here: the summary model must not do arithmetic on nested counts
            per = {c: linkedin_people(v) for c, v in d.items() if isinstance(v, dict)}
            if per:
                d = {"people_found_per_group": per, **({"note": d["note"]} if d.get("note") else {})}
        elif key == "verify" and d.get("checked"):
            d["result"] = (f"{d['checked']} of {d.get('requested', d['checked'])} profiles checked; {d.get('flagged', 0)} "
                           "flagged (moved to another company or outside the target region). Flagged people are "
                           "already checked and marked in the results; there is nothing to retry for them")
            if d.get("error"):
                d["warning"] = f"the rest were not checked because {d['error']}"
        elif key == "verify" and d.get("error"):
            d["warning"] = (f"checked {d.get('checked', 0)} of {d.get('requested', '?')} LinkedIn profiles "
                            f"(employer / location of the rest not confirmed) because {d['error']}")
        elif key == "verify" and d.get("requested") and not d.get("checked"):
            d["warning"] = (f"asked the LinkedIn provider for {d['requested']} profiles but got none back, so no "
                            "profile was actually checked (employer / location not confirmed)")
        steps.append({"step": label, "status": status, "detail": d})

    groups = []
    for i in icps:
        rule = i.get("company_rule")
        g = {"code": i["code"], "name": i.get("name"), "how_found": FOUND_WORDS.get(i.get("find_via", "company_list")),
             "company_requirement": (f"company must exhibit at {m}+ shows within 12 months" if rule == "exhibitor_3plus"
                                     else RULE_WORDS.get(rule, rule)),
             "leads": q(f"""SELECT COUNT(*) FROM people p JOIN company_industries ci ON ci.company_id=p.company_id
                            {db.IN_GROUP} WHERE ci.industry_id=? AND g.icp=?""", i["code"])}
        if rule not in (None, "", "any"):
            g["companies_meeting_requirement"] = sum(company_rule_ok(rule, f, m) for f in facts.values())
            g["companies_total"] = len(comp_ids)
        groups.append(g)

    out = {"search": ind["name"], "persona": ind["persona_text"], "target_groups": groups, "steps": steps,
           "leads_in_these_target_groups_total": sum(g["leads"] for g in groups)}
    if has_count_rule:
        dist = shows_distribution(con, ind)
        out["shows_rule"] = {"asked_min_shows": asked, "used_min_shows": m,
                             "companies_meeting_asked": _at_least(dist, asked),
                             "companies_by_shows_in_12_months": {str(k): v for k, v in dist.items()}}
    if by_company or uses_shows:
        out["shows"] = [{"name": s["name"], "date": s["start_date"], "exhibitors_recorded": s["exhibitors"]}
                        for s in show_coverage(con, ind)]
    if by_company:
        out["companies_total"] = len(comp_ids)
        out["companies_searched_for_people"] = q("""SELECT COUNT(DISTINCT json_extract(context, '$.company_id'))
            FROM search_queue WHERE purpose='people' AND status IN ('done','empty')
            AND json_extract(context, '$.industry_id')=?""")
    out["people_searches_still_queued"] = q("""SELECT COUNT(*) FROM search_queue WHERE status='pending'
        AND json_extract(context, '$.industry_id')=?""")
    out["people_matching_persona"] = q(f"""SELECT COUNT(*) FROM people p JOIN company_industries ci
        ON ci.company_id=p.company_id {db.IN_GROUP} WHERE ci.industry_id=?""")
    out["email_search"] = "off - the user's choice" if int(opts.get("emails", 25)) <= 0 else "on"
    out["people_with_email"] = q(f"""SELECT COUNT(*) FROM people p JOIN company_industries ci
        ON ci.company_id=p.company_id {db.IN_GROUP} WHERE ci.industry_id=? AND p.email IS NOT NULL""")
    resting = {}
    for svc in keys.SPECS:
        ks = keys.all_keys(svc)
        if ks:
            n_rest = len(ks) - len(keys.usable(svc, con))
            if n_rest:
                resting[svc] = f"{n_rest} of {len(ks)} keys resting"
    out["api_keys_resting"] = resting or "none"
    redo = [st["step"] for st in steps if st["status"] in ("warn", "failed", "stopped", "not run")]
    queued = out["people_searches_still_queued"]
    out["what_run_again_would_do"] = (
        ("retry: " + ", ".join(redo) if redo else "") + ("; " if redo and queued else "")
        + (f"continue {queued} queued searches" if queued else "")) or \
        "nothing: every step is done and no searches are queued, so do not suggest Run again"
    from . import showhelp
    waiting = [h["name"] for h in showhelp.needing_help(con, ind)] if by_company or uses_shows else []
    if waiting:
        out["exhibitor_lists_waiting_for_the_user"] = {
            "shows": waiting,
            "how": "the 'Exhibitor lists the tool couldn't read' card on this page: paste a link, upload the list "
                   "(PDF, CSV or Excel), or copy a prompt for a browser agent that collects it with the user"}
    return out


def linkedin_people(v):
    """People one LinkedIn step result added for a target group, including its fallback searches."""
    if not isinstance(v, dict):
        return 0
    n = v.get("people_added_or_matched") or v.get("title_searches_planned") or 0
    for sub in ("aiark_for_unresolved", "deepline", "paid_for_rest"):
        n += linkedin_people(v.get(sub)) if isinstance(v.get(sub), dict) else 0
    return n


def unsupported_numbers(text, f):
    """Numbers in the summary that appear nowhere in the facts (small counts like 'two groups' are fine)."""
    known = {int(n) for n in re.findall(r"\d+", json.dumps(f, default=str))} | set(range(11))
    said = {int(n.replace(",", "")) for n in re.findall(r"(?<![\w.])\d[\d,]*(?![\w.]*\d)", text or "")}
    return sorted(said - known)


def fallback_summary(f):
    """Plain summary from the facts, used when the Sonnet call fails or its text doesn't match the facts."""
    lines = [f"People found: {f['people_matching_persona']} matching the persona"
             + (f", {f['people_with_email']} with a work email." if f["email_search"] == "on"
                else " (email search was off).")]
    for g in f["target_groups"]:
        extra = (f"; {g['companies_meeting_requirement']} of {g['companies_total']} companies meet it"
                 if "companies_meeting_requirement" in g else "")
        lines.append(f"- {g['code']} {g['name']}: {g['leads']} leads ({g['company_requirement']}{extra}).")
    for st in f["steps"]:
        d = st["detail"]
        why = d.get("warning") or d.get("skipped") or d.get("error") or d.get("note") or ""
        if d.get("result"):
            why = d["result"] + (f"; {why}" if why else "")
        if d.get("people_found_per_group"):
            why = ", ".join(f"{c}: {n} people" for c, n in d["people_found_per_group"].items()) + (f"; {why}" if why else "")
        lines.append(f"- {st['step']}: {st['status']}" + (f" - {why}" if why else ""))
    r = f.get("shows_rule")
    if r:
        lines.append(f"- Shows rule: asked {r['asked_min_shows']}+ shows, used {r['used_min_shows']}+; "
                     f"{r['companies_meeting_asked']} companies meet {r['asked_min_shows']}+.")
    thin = [s["name"] for s in f.get("shows", []) if s["exhibitors_recorded"] < THIN_LIST]
    if thin:
        lines.append(f"- Shows with few or no exhibitors recorded: {', '.join(thin)}.")
    if "companies_searched_for_people" in f:
        lines.append(f"- People search covered {f['companies_searched_for_people']} of {f['companies_total']} "
                     "companies.")
    w = f.get("exhibitor_lists_waiting_for_the_user")
    if w:
        lines.append(f"- Exhibitor lists waiting for you: {', '.join(w['shows'])}. Use the card above: paste a link, "
                     "upload the list (PDF, CSV or Excel), or copy the browser-agent prompt.")
    if f["people_searches_still_queued"]:
        lines.append(f"- {f['people_searches_still_queued']} searches still queued; Run again continues them.")
    return "\n".join(lines)


def _claude_text(prompt, model=None, effort=None, timeout=240):
    """One tool-less Claude call whose answer we keep (the stage runner's _claude only streams to the log)."""
    cmd = ["claude", "-p", prompt, "--output-format", "text", "--tools", "", "--no-session-persistence",
           "--model", model or DEFAULT_MODEL, "--effort", effort or DEFAULT_EFFORT]
    try:
        r = subprocess.run(cmd, cwd=config.ROOT, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def stage_summary(con, ind, opts):
    f = run_facts(con, ind)
    prompt = f"""You write the end-of-run report for a lead-finder tool. The reader is a non-technical user who wants to
know what happened and what is holding the results back. Use ONLY the facts in the JSON below; never invent numbers,
never do arithmetic on them, and never mention a rule, setting or number that is not in the facts.

Write plain text, no markdown headings, at most ~14 short lines:
1. One line: the outcome (people matching the persona, per target group). Mention emails only if email_search is "on".
2. "Done:" what each step achieved, one short bullet ("- ") per step that ran; group skipped / not-needed steps
   into one bullet with the reason as given. Report a step's "warning" plainly (e.g. requested vs actually checked).
3. "Bottleneck:" the single biggest reason there aren't more results, with the numbers that show it (e.g. a target
   group whose company requirement few companies meet; searches still queued; keys resting; shows missing exhibitor
   lists). Something the user turned off on purpose is their choice, NEVER a bottleneck - don't list it there or
   suggest turning it back on. If nothing else limited the results, write "Bottleneck: none found".
4. "Next:" 1-3 concrete actions the user can take (reword the persona, upload an exhibitor list, add an API key...).
   If exhibitor_lists_waiting_for_the_user is present, name those shows and point to that card as one action.
   A step's "warning" that names a cause (e.g. a free allowance used up) must be reported with that cause.
   Copy coverage phrases such as "searched X of the Y companies" as given; never round them up to "all".
   How the buttons work: "Run again" only continues steps whose status is warn/failed/stopped/not run, plus people
   searches still queued; it never redoes a step whose status is "done". "Edit & re-run" redoes the steps affected
   by the edit. Only suggest a button that will actually do what you say: what_run_again_would_do says exactly
   what Run again does now.

FACTS:
{json.dumps(f, default=str)[:12000]}"""
    text = _claude_text(prompt, opts.get("summary_model", "sonnet"), "low")
    bad = unsupported_numbers(text, f) if text else []
    if bad:
        log(f"summary mentioned numbers not in the facts {bad}; using the plain summary instead")
        text = None
    return {"text": text or fallback_summary(f), "by": "sonnet" if text else "fallback"}


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
    if key == "web" and d.get("pending"):
        return (f"Searched {d['done']} so far; {d['pending']} searches still queued"
                + (f" ({d['stopped']})" if d.get("stopped") else "") + ". Resume continues them.")
    if key == "verify" and d.get("error"):
        return (f"Checked {d.get('checked', 0)} of {d.get('requested', '?')} LinkedIn profiles: {d['error']}"
                if d.get("checked") else f"No LinkedIn profiles were checked: {d['error']}")
    if key == "verify" and d.get("requested") and not d.get("checked"):
        return (f"Asked for {d['requested']} LinkedIn profiles but none came back (private or removed profiles, "
                "or the provider returned nothing), so nothing was checked.")
    if key == "export" and not d.get("leads"):
        return "No people matched this persona yet. Use 'Edit & re-run' to broaden the description."
    return None


RUNNERS = {"persona": stage_persona, "research": stage_research, "evidence": stage_evidence,
           "attendees": stage_attendees, "customers": stage_customers,
           "linkedin": stage_linkedin, "web": stage_web, "verify": stage_verify, "emails": stage_emails,
           "export": stage_export, "summary": stage_summary}


# Cheap, idempotent steps (verify skips checked profiles, emails skips people already tried) that must run again when
# an earlier finding step ran again, e.g. a web search resumed after a stop, or its new people get no emails/export.
REFRESH_AFTER_FINDING = ("verify", "emails", "export")


def _progress(con, ind_id, stage):
    """Partial results a stopped stage saved, so a resumed run continues instead of starting over."""
    row = con.execute("SELECT status, detail FROM pipeline_stages WHERE industry_id=? AND stage=?",
                      (ind_id, stage)).fetchone()
    if not row or row["status"] not in ("running", "stopped") or not row["detail"]:
        return {}
    d = json.loads(row["detail"])
    return d.get("partial") or {}


def _save_progress(con, ind_id, stage, partial):
    """Merged into what the stage saved before, so two parts of one stage (e.g. evidence: lists, catalogs) keep both."""
    partial = {**_progress(con, ind_id, stage), **partial}
    con.execute("UPDATE pipeline_stages SET detail=? WHERE industry_id=? AND stage=?",
                (json.dumps({"partial": partial}, default=str), ind_id, stage))
    con.commit()


def _status(con, ind_id, stage):
    row = con.execute("SELECT status FROM pipeline_stages WHERE industry_id=? AND stage=?", (ind_id, stage)).fetchone()
    return row["status"] if row else None


def _on_term(signum, frame):
    raise Stopped()


def firecrawl_env(slug, opts):
    """Firecrawl budget for this run, inherited by `./lf` commands Claude runs (see providers/firecrawl.py)."""
    import os
    os.environ["LF_FIRECRAWL_AUTO"] = "1"
    os.environ["LF_FIRECRAWL_CAP"] = str(int(opts.get("firecrawl_credits", FIRECRAWL_RUN_CREDITS)))
    os.environ["LF_FIRECRAWL_BUCKET"] = f"firecrawl_run:{slug}:{db.now()}"


def run(slug, opts, from_stage=None):
    signal.signal(signal.SIGTERM, _on_term)
    con = db.connect()
    ind = db.get_industry(con, slug)
    firecrawl_env(slug, opts)
    (config.DATA / "work" / slug).mkdir(parents=True, exist_ok=True)  # Claude may write here (no mkdir permission)
    keys = [k for k, _ in STAGES]
    if from_stage:
        for k in keys[keys.index(from_stage):]:
            # a step the user chose to skip stays skipped across re-runs (they can Include it again)
            con.execute("DELETE FROM pipeline_stages WHERE industry_id=? AND stage=? AND status!='skipped'",
                        (ind["id"], k))
        con.commit()
    ran_finder = False  # did a people-finding step run in this invocation?
    for key, label in STAGES:
        status = _status(con, ind["id"], key)  # read fresh: the user may skip a later step while this runs
        if key == "summary" and status != "skipped":
            status = None  # always re-written: it describes this run, including steps that failed or warned
        if status == "done" and ran_finder and key in REFRESH_AFTER_FINDING:
            log(f"{label}: running again for the people found just now")
            status = None
        if status == "done":
            log(f"{label}: already done, skipping")
            continue
        if status == "skipped":
            log(f"{label}: skipped by you")
            continue
        if status == "questions":
            log(f"{label}: waiting for your answers to its questions (answer them in the web UI)")
            return 0
        if status == "help":
            log(f"{label}: waiting for your help with exhibitor lists it could not read (see the web UI)")
            return 0
        if key == "persona" and not ind["persona_text"] and ind["icp_json"]:
            _stage_set(con, ind["id"], key, "done", {"note": "personas defined earlier"})
            continue
        log(f"{label}…")
        ran_finder = ran_finder or key in ("research", "evidence", "attendees", "customers", "linkedin", "web")
        _stage_set(con, ind["id"], key, "running")
        t = time.time()
        try:
            detail = RUNNERS[key](con, ind, opts)
        except NeedsInput as e:
            if "help" in e.detail:
                _stage_set(con, ind["id"], key, "help", {**e.detail, "seconds": round(time.time() - t)})
                log(f"{label}: could not read the exhibitor list of {', '.join(h['name'] for h in e.detail['help'])}. "
                    "Paste a link or use the agent prompt in the web UI, or continue without them.")
                return 0
            _stage_set(con, ind["id"], key, "questions", {**e.detail, "seconds": round(time.time() - t)})
            log(f"{label}: has {len(e.detail['questions'])} question(s) for you. Answer them in the web UI, "
                "or let it continue with its best guess.")
            return 0
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
