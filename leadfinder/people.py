"""People finding: turn (company x ICP titles) into LinkedIn searches, run them through the search router
via a resumable queue, parse result titles/snippets into people, and keep only title-relevant matches."""
import json
import re
import time

from . import db
from .normalize import norm_company, same_company, search_name, strict_company_match
from .providers import search as router

# ---------- ICP helpers ----------
VISIT_KINDS = {"visitor_post", "thanked_by_exhibitor", "order", "delegation", "speaker", "buyer_meet",
               "report_named", "matchmaking", "app_attendee", "testimonial"}


def load_icps(industry_row):
    if not industry_row["icp_json"]:
        raise SystemExit("No ICPs set for this industry yet (lf set-icp ...).")
    return [i for i in json.loads(industry_row["icp_json"])["icps"] if i.get("enabled", True)]


def title_matches(title, icp):
    t = f" {(title or '').lower()} "
    excl = [k.lower() for k in icp.get("exclude_keywords", [])]
    if any(k in t for k in excl):
        return False
    return any(k.lower() in t for k in icp.get("title_keywords", []))


def company_facts(con, company_id, window_days=365):
    from .scoring import best_window_count
    shows = con.execute("""SELECT DISTINCT s.id, s.name, s.start_date, s.year FROM evidence e JOIN shows s ON s.id=e.show_id
                           WHERE e.entity='company' AND e.entity_id=? AND e.kind='exhibitor'""", (company_id,)).fetchall()
    visit = con.execute(f"""SELECT kind, confidence, show_id FROM evidence
                            WHERE entity='company' AND entity_id=? AND kind IN ({','.join('?' * len(VISIT_KINDS))})""",
                        (company_id, *VISIT_KINDS)).fetchall()
    # staff of an exhibiting company quoted/seen at that same show are not buyers visiting it
    exhibited = {s["id"] for s in shows}
    visit = [v for v in visit if v["show_id"] is None or v["show_id"] not in exhibited]
    return {
        "exhibitor_shows": [dict(s) for s in shows],
        "exhibitor_window_count": best_window_count(shows, window_days),
        "visitor_evidence": len(visit),
        "visitor_shows": len({v["show_id"] for v in visit if v["show_id"]}),
    }


def company_rule_ok(rule, facts, min_shows=3):
    if rule in (None, "", "any"):
        return True
    if rule == "exhibitor_3plus":
        return facts["exhibitor_window_count"] >= max(min_shows or 0, 1)
    if rule == "exhibitor":
        return len(facts["exhibitor_shows"]) > 0
    if rule == "visitor":
        return facts["visitor_evidence"] > 0
    if rule == "visitor_or_exhibitor":
        return facts["visitor_evidence"] > 0 or len(facts["exhibitor_shows"]) > 0
    return True


def requested_min_shows(industry):
    """How many shows a year the user asked for ('exhibits at 5+ shows' -> 5): an explicit option, else the number in
    the form's trade-show field, else in the persona text. With no number: 2 if a persona needs a repeat exhibitor
    ('exhibits at multiple shows' -> exhibitor_3plus), else 0 - no show-count rule at all."""
    from .targeting import get as target, shows_wanted
    try:
        opts = json.loads(industry["options_json"] or "{}")
    except (KeyError, IndexError, TypeError, ValueError):
        opts = {}
    if opts.get("shows_wanted"):
        return int(opts["shows_wanted"])
    for text in (target(industry).get("company_rule"), _get(industry, "persona_text")):
        n = shows_wanted(text)
        if n:
            return n
    try:
        multi = any(i.get("company_rule") == "exhibitor_3plus" for i in load_icps(industry))
    except (SystemExit, KeyError, IndexError, TypeError, ValueError):
        multi = False
    return 2 if multi else 0


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def effective_min_shows(industry):
    """The shows threshold actually in use for this search: what was asked for, unless it was relaxed automatically
    because too few companies met it (options_json.min_shows, set by the pipeline; never above what was asked)."""
    try:
        used = json.loads(industry["options_json"] or "{}").get("min_shows")
    except (KeyError, IndexError, TypeError, ValueError):
        used = None
    wanted = requested_min_shows(industry)
    return min(int(used), wanted) if used else wanted


def companies_for_icp(con, industry, icp, min_shows=None, min_count=10):
    """Companies passing the ICP's company rule, best first. For exhibitor_3plus, relax 3 -> 2 -> 1 shows when nobody
    qualifies (exhibitor lists are often incomplete). Returns (companies, min_shows_used)."""
    comps = con.execute("""SELECT c.* FROM companies c JOIN company_industries ci ON ci.company_id=c.id
                           WHERE ci.industry_id=?""", (industry["id"],)).fetchall()
    facts = {c["id"]: company_facts(con, c["id"]) for c in comps}
    rule = icp.get("company_rule")
    rank = lambda c: (-facts[c["id"]]["exhibitor_window_count"], -facts[c["id"]]["visitor_evidence"])  # noqa: E731
    start = min_shows or requested_min_shows(industry)
    levels = list(range(start, 0, -1)) if rule == "exhibitor_3plus" and start > 1 else [start]
    ok, used = [], levels[0]
    for m in levels:  # relax until there are enough companies to search (best-ranked first)
        ok = sorted([c for c in comps if company_rule_ok(rule, facts[c["id"]], m)], key=rank)
        used = m
        if len(ok) >= min_count:
            break
    return ok, used


def classify(con, icps, title, company_id, min_shows=0):
    """First ICP (in template order) whose title keywords match and whose company rule is satisfied."""
    facts = company_facts(con, company_id) if company_id else {"exhibitor_shows": [], "exhibitor_window_count": 0,
                                                                "visitor_evidence": 0, "visitor_shows": 0}
    for icp in icps:
        if title_matches(title, icp) and company_rule_ok(icp.get("company_rule"), facts, min_shows):
            return icp["code"]
    return None


# ---------- LinkedIn result parsing ----------
ROLE_WORDS = re.compile(r"\b(manager|head|director|president|vp|officer|engineer|executive|lead|buyer|sourcing|"
                        r"procurement|purchase|purchasing|sales|marketing|founder|owner|consultant|specialist|"
                        r"supply chain)\b", re.I)
LOCATION_LIKE = re.compile(r",|\b(India|Area|Region|Division|District|Metropolitan|Pune|Mumbai|Chennai|Bengaluru|"
                           r"Bangalore|Delhi|Hyderabad|Kolkata|Ahmedabad|Coimbatore|Rajkot)\b", re.I)
def _clean(s):
    s = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", s or "", flags=re.I).strip()
    return re.sub(r"\s*(\.\.\.|…)\s*$", "", s).strip(" -–|·")


def parse_profile(result):
    """Parse a search result for a linkedin.com/in/ profile into name/title/company/location."""
    raw_title = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", result.get("title", ""), flags=re.I).strip()
    truncated = raw_title.endswith(("...", "…"))
    title = _clean(raw_title)
    snippet = result.get("snippet") or ""
    url = result.get("url") or ""
    if "linkedin.com/in/" not in url and "google.com/goto" not in url:
        return None
    parts = re.split(r"\s+[-–]\s+", title, maxsplit=1)
    name = parts[0].strip()
    rest = parts[1] if len(parts) > 1 else ""
    p_title, p_company = None, None
    if rest:
        if " at " in rest:
            p_title, p_company = [x.strip() for x in rest.rsplit(" at ", 1)]
        elif ROLE_WORDS.search(rest):
            p_title = rest.strip()      # a headline like "Strategic sourcing for Defence | Purchase"
        else:
            p_company = rest.strip()    # "Deepak Kajale - Bharat Forge Ltd"
    location = None
    # Google-style snippet: "Pune, Maharashtra, India · Senior Manager Procurement · Bharat Forge Ltd"
    bits = [_clean(b) for b in snippet.split("·") if b.strip()]
    labelled = any(re.match(r"^(Experience|Location|Education|Connections?)\s*:", b) for b in bits)
    if labelled and bits and " at " in bits[0] and len(bits[0]) < 120:
        # Serper-style: "Purchase Manager at Bharat Forge Ltd · Experience: Bharat Forge Ltd · Location: Pune"
        s_title, s_company = [x.strip() for x in bits[0].rsplit(" at ", 1)]
        if len(s_title) > len(p_title or ""):
            p_title = s_title
        if not p_company or truncated:
            p_company = s_company
    elif len(bits) >= 3 and len(bits[0]) < 80 and LOCATION_LIKE.search(bits[0]) and len(bits[1]) < 100:
        location, s_title, s_company = bits[0], bits[1], bits[2]
        if len(s_title) > len(p_title or ""):
            p_title = s_title
        if not p_company or truncated:
            p_company = s_company
    if not rest:
        # TinySearch-style: title is only the name; role/location are in a (markdown) snippet
        lines = [re.sub(r"^[#\-*\s]+|\*+", "", ln).strip() for ln in snippet.splitlines()]
        for ln in [x for x in lines if x][:4]:
            if " at " in ln and ROLE_WORDS.search(ln.rsplit(" at ", 1)[0]) and not p_title:
                p_title, p_company = [x.strip() for x in ln.rsplit(" at ", 1)]
            elif LOCATION_LIKE.search(ln) and len(ln) < 60 and not location:
                location = ln.split("·")[0].strip()
            elif ROLE_WORDS.search(ln) and not p_title and len(ln) < 140:
                p_title = ln
    m = re.search(r"Experience:\s*([^·]+)", snippet)
    if m and not p_company:
        p_company = m.group(1).strip()
    m = re.search(r"Location:\s*([^·]+)", snippet)
    if m and not location:
        location = m.group(1).strip()
    if not name or len(name) > 60 or any(ch.isdigit() for ch in name):
        return None
    return {"name": name.title() if name.islower() else name, "title": _clean(p_title or "") or None,
            "company": _clean(p_company or "") or None, "location": location,
            "linkedin_url": url if "linkedin.com/in/" in url else None, "evidence_url": url,
            "snippet": snippet[:300]}


def parse_post(result):
    """LinkedIn post result -> author + text. Titles look like 'Name on LinkedIn: text' or "Name's Post"."""
    title = result.get("title", "")
    m = re.match(r"(.+?)\s+on LinkedIn:", title) or re.match(r"(.+?)['’]s Post", title) \
        or re.match(r"(.+?)\s+posted on the topic", title, re.I)
    author = m.group(1).strip() if m else None
    return {"author": author, "title": title, "url": result.get("url"), "snippet": result.get("snippet", "")}


# ---------- queue ----------
def enqueue(con, purpose, query, site, context):
    params = json.dumps({"site": site}, sort_keys=True)
    cur = con.execute("""INSERT OR IGNORE INTO search_queue(purpose, query, params, context, created_at)
                         VALUES (?,?,?,?,?)""", (purpose, query, params, json.dumps(context), db.now()))
    return cur.rowcount


def plan_people_searches(con, industry, icp_codes=None, limit=None, min_shows=None, keywords_per_query=4,
                         max_companies=None):
    """Queue one search per (company x ICP). Best companies first (most shows, then buyer evidence), so a cap keeps the
    strongest. `limit` counts only NEWLY queued searches (searches already queued don't use it up, so repeated calls
    keep advancing); `max_companies` caps how many top-ranked companies are considered."""
    min_shows = min_shows or effective_min_shows(industry)
    icps = load_icps(industry)
    if icp_codes:
        icps = [i for i in icps if i["code"] in icp_codes]
    companies = con.execute("""SELECT c.* FROM companies c JOIN company_industries ci ON ci.company_id=c.id
                               WHERE ci.industry_id=? ORDER BY c.id""", (industry["id"],)).fetchall()
    facts = {c["id"]: company_facts(con, c["id"]) for c in companies}
    companies = sorted(companies, key=lambda c: (-facts[c["id"]]["exhibitor_window_count"],
                                                 -facts[c["id"]]["visitor_evidence"], c["id"]))
    planned, skipped, eligible = 0, 0, 0
    for c in companies:
        queued_any = False
        for icp in icps:
            if icp.get("find_via") == "industry_search":
                continue  # ICPs found without a company list (e.g. B) are searched by title + sector
            if not company_rule_ok(icp.get("company_rule"), facts[c["id"]], min_shows):
                skipped += 1
                continue
            kws = icp.get("search_keywords") or icp.get("title_keywords", [])
            kws = kws[:keywords_per_query]
            q = f'"{search_name(c["name"])}" (' + " OR ".join(f'"{k}"' for k in kws) + ")"
            planned += enqueue(con, "people", q, "linkedin.com/in",
                               {"company_id": c["id"], "company": c["name"], "icp": icp["code"],
                                "industry_id": industry["id"]})
            queued_any = True
        eligible += queued_any
        if (limit and planned >= limit) or (max_companies and eligible >= max_companies):
            break
    con.commit()
    return {"planned": planned, "skipped_company_rule": skipped, "companies_considered": eligible}


def plan_title_searches(con, industry, icp_code, sectors, locations, limit=None):
    """For ICPs not tied to a show (e.g. B2 field sellers): search titles x sector x location."""
    icp = next(i for i in load_icps(industry) if i["code"] == icp_code)
    n = 0
    for sector in sectors:
        for loc in locations:
            for kw in (icp.get("search_keywords") or icp["title_keywords"])[:3]:
                enqueue(con, "people_by_title", f'"{kw}" "{sector}" "{loc}"', "linkedin.com/in",
                        {"icp": icp_code, "industry_id": industry["id"], "sector": sector})
                n += 1
                if limit and n >= limit:
                    con.commit()
                    return {"planned": n}
    con.commit()
    return {"planned": n}


def plan_post_searches(con, show, hashtags=None):
    base = f'"{show["name"]}"' if str(show["year"] or "") in show["name"] else f'"{show["name"]} {show["year"]}"'
    phrases = ["visited", "\"see you at\"", "\"thank you for visiting\"", "\"great to be at\"", "booth OR stall"]
    for ph in phrases:
        enqueue(con, "posts", f"{base} {ph}", "linkedin.com/posts", {"show_id": show["id"]})
    for h in hashtags or []:
        enqueue(con, "posts", f'"#{h.lstrip("#")}"', "linkedin.com/posts", {"show_id": show["id"]})
    con.commit()


def _region_for(con, industry_id, cache):
    """Search-engine country for a queued search: the country of the search it belongs to."""
    if industry_id not in cache:
        from . import targeting
        row = con.execute("SELECT * FROM industries WHERE id=?", (industry_id,)).fetchone() if industry_id else None
        cache[industry_id] = targeting.region_code(row) if row else "IN"
    return cache[industry_id]


def run_queue(con, max_items=20, purpose=None, log=print, retry_empty=False, industry_id=None, deadline=None):
    """Process queued searches. Items that returned nothing are marked 'empty' (not 'done'), so a later run with
    another provider (e.g. after adding a Serper key) can retry them with retry_empty=True."""
    sql = "SELECT * FROM search_queue WHERE status IN ('pending'" + (", 'empty')" if retry_empty else ")")
    args = []
    if purpose:
        sql += " AND purpose=?"
        args.append(purpose)
    if industry_id:  # only this search's items (the queue is shared by every search)
        sql += " AND json_extract(context, '$.industry_id')=?"
        args.append(industry_id)
    items = con.execute(sql + " ORDER BY id LIMIT ?", (*args, max_items)).fetchall()
    stats = {"done": 0, "people_added": 0, "posts_added": 0, "stopped": None}
    regions = {}
    for it in items:
        if deadline and time.time() > deadline:
            stats["stopped"] = "time budget reached (run again to continue)"
            break
        site = json.loads(it["params"]).get("site")
        ctx = json.loads(it["context"])
        try:
            results, provider = router.search(con, it["query"], site=site, num=20,
                                              region=_region_for(con, ctx.get("industry_id"), regions))
        except router.ProviderBusy as e:
            stats["stopped"] = str(e)
            con.execute("UPDATE search_queue SET attempts=attempts+1, last_error=? WHERE id=?", (str(e)[:300], it["id"]))
            con.commit()
            log(f"  paused: {e}")
            break
        if it["purpose"] in ("people", "people_by_title"):
            stats["people_added"] += _ingest_people(con, results, ctx)
        elif it["purpose"] == "posts":
            stats["posts_added"] += _ingest_posts(con, results, ctx)
        con.execute("UPDATE search_queue SET status=?, provider=?, done_at=?, attempts=attempts+1 WHERE id=?",
                    ("done" if results else "empty", provider, db.now(), it["id"]))
        con.commit()
        stats["done"] += 1
        log(f"  [{provider}] {it['query'][:80]} -> {len(results)} results")
    q = "SELECT COUNT(*) FROM search_queue WHERE status='pending'"
    stats["pending"] = con.execute(q + (" AND json_extract(context, '$.industry_id')=?" if industry_id else ""),
                                   (industry_id,) if industry_id else ()).fetchone()[0]
    return stats


def _ingest_people(con, results, ctx):
    industry = con.execute("SELECT * FROM industries WHERE id=?", (ctx["industry_id"],)).fetchone()
    icps = load_icps(industry)
    target_icp = next((i for i in icps if i["code"] == ctx["icp"]), None)
    added = 0
    for r in results:
        p = parse_profile(r)
        if not p or not p["title"]:
            continue
        if not title_matches(p["title"], target_icp):
            continue
        if ctx.get("company_id"):
            # keep only people who actually work at the target company
            # strict: 'Wheels, LLC' must not count as 'Wheels India' (loose matching drops a trailing 'India')
            works_there = (p["company"] and strict_company_match(p["company"], ctx["company"])) \
                or (not p["company"] and strict_company_match(f'{p["title"]} {p["snippet"]}', ctx["company"]))
            if not works_there:
                continue
            company_id = ctx["company_id"]
        else:
            if not p["company"]:
                continue
            company_id = db.upsert_company(con, p["company"], industry_id=ctx["industry_id"], sector=ctx.get("sector"))
        pid = db.upsert_person(con, company_id, p["name"], title=p["title"], location=p["location"],
                               linkedin_url=p["linkedin_url"], source="search")
        if pid:
            db.add_evidence(con, "person", pid, "linkedin_profile", "inferred", p["evidence_url"], p["snippet"])
            added += 1
    return added


def _ingest_posts(con, results, ctx):
    added = 0
    for r in results:
        if "linkedin.com/posts" not in (r.get("url") or "") and "linkedin.com/feed" not in (r.get("url") or "") \
                and "google.com/goto" not in (r.get("url") or ""):
            continue
        p = parse_post(r)
        cur = con.execute("""INSERT OR IGNORE INTO post_candidates(show_id, url, author, title, snippet, status, at)
                             VALUES (?,?,?,?,?,'new',?)""",
                          (ctx["show_id"], p["url"], p["author"], p["title"], p["snippet"], db.now()))
        added += cur.rowcount
    return added
