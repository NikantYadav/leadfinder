"""People finding: turn (company x ICP titles) into LinkedIn searches, run them through the search router
via a resumable queue, parse result titles/snippets into people, and keep only title-relevant matches."""
import difflib
import html
import json
import re
import time

from . import db
from .normalize import norm_company, same_company, search_name, strict_company_match
from .providers import search as router

# ---------- ICP helpers ----------
VISIT_KINDS = {"visitor_post", "thanked_by_exhibitor", "order", "delegation", "speaker", "buyer_meet",
               "report_named", "matchmaking", "app_attendee", "testimonial", "visit_comment",
               "customer_of_exhibitor"}   # the last: named as a customer on an exhibitor's website (customers.py)
# company rules a person who only liked / commented on a show post (evidence post_engaged) may pass: rules about
# visiting or buying. Rules about the company EXHIBITING still need the company's own exhibitor evidence.
ENGAGED_RULES = (None, "", "any", "visitor", "visitor_or_exhibitor")


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
        # without 'named as a customer by an exhibitor': people of companies seen at a show are searched first
        "direct_visits": sum(1 for v in visit if v["kind"] != "customer_of_exhibitor"),
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
    rank = lambda c: (-facts[c["id"]]["exhibitor_window_count"], -facts[c["id"]]["direct_visits"],  # noqa: E731
                      -facts[c["id"]]["visitor_evidence"])
    start = min_shows or requested_min_shows(industry)
    levels = list(range(start, 0, -1)) if rule == "exhibitor_3plus" and start > 1 else [start]
    ok, used = [], levels[0]
    for m in levels:  # relax until there are enough companies to search (best-ranked first)
        ok = sorted([c for c in comps if company_rule_ok(rule, facts[c["id"]], m)], key=rank)
        used = m
        if len(ok) >= min_count:
            break
    return ok, used


def classify(con, icps, title, company_id, min_shows=0, seen=False, engaged=False):
    """First ICP (in template order) whose title keywords match and whose company rule is satisfied. seen=True (the
    person has their own evidence of being at a show) skips the company rule: that beats any company-level proof.
    engaged=True (liked / commented on a post about one of the search's shows) passes only visiting/buying rules."""
    facts = company_facts(con, company_id) if company_id else {"exhibitor_shows": [], "exhibitor_window_count": 0,
                                                                "visitor_evidence": 0, "direct_visits": 0,
                                                                "visitor_shows": 0}
    for icp in icps:
        if title_matches(title, icp) and (seen or (engaged and icp.get("company_rule") in ENGAGED_RULES)
                                          or company_rule_ok(icp.get("company_rule"), facts, min_shows)):
            return icp["code"]
    return None


# ---------- LinkedIn result parsing ----------
ROLE_WORDS = re.compile(r"\b(manager|head|director|president|vp|officer|engineer|executive|lead|buyer|sourcing|"
                        r"procurement|purchase|purchasing|sales|marketing|founder|owner|consultant|specialist|"
                        r"supply chain|ceo|cxo|cfo|coo|cto|md|managing|proprietor|chairman|partner|"
                        r"business development|key account|merchandiser|coordinator|entrepreneur)\b", re.I)
LOCATION_LIKE = re.compile(r",|\b(India|Area|Region|Division|District|Metropolitan|Pune|Mumbai|Chennai|Bengaluru|"
                           r"Bangalore|Delhi|Hyderabad|Kolkata|Ahmedabad|Coimbatore|Rajkot)\b", re.I)
MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"


def _clean(s):
    s = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", s or "", flags=re.I).strip()
    return re.sub(r"\s*(\.\.\.|…)\s*$", "", s).strip(" -–|·")


def _tidy(s, max_len=120):
    """One clean line: no markdown, HTML entities, icon-font glyphs or labels; None when it reads like prose."""
    if not s:
        return None
    s = html.unescape(s).split("\n")[0]
    s = re.sub(r"[-]", "", s)                      # icon-font bullets ()
    s = re.sub(r"^[#\-*•\s]+|\*+", "", s)
    s = re.sub(r"^(Location|Experience)\s*:\s*", "", s, flags=re.I)
    s = _clean(re.sub(r"\s+", " ", s)).rstrip(",;")
    return s if s and len(s) <= max_len else None


PROSE = re.compile(r"^(as |i |i['’]m|>|currently|previously|worked|handling|heading|responsible|in addition)|"
                   r"\b(i|i['’]m|i['’]ve|my|we|our|me)\b|[^\w\s,.&()/'’|@:+#–-]", re.I)


def _prose(line):
    """A sentence from an About/description block, not a title: first person, a long sentence, emoji or bullets."""
    return bool(PROSE.search(line or "")) or len(line or "") > 100


def _split_role(line):
    """'Sales Manager at Acme Ltd' -> ('Sales Manager', 'Acme Ltd'); the title is None when it isn't a role
    (a line cut mid-text: 'Delhi /NCR at Kelzai', 'at Arjun Engineering')."""
    if " at " not in f" {line} ":
        return None, None
    t, c = (f" {line}".rsplit(" at ", 1) + [""])[:2]
    t, c = _tidy(t), _tidy(c, 90)
    return (t if t and ROLE_WORDS.search(t) else None), c


def _date_status(dates):
    d = (dates or "").lower()
    if "present" in d:
        return "current"
    if re.search(r"[–—-]\s*\S", d):
        return "past"
    return "unknown"   # the snippet was cut before the end date


def parse_markdown_profile(snippet):
    """TinySearch returns excerpts of the profile page as markdown, separated by blank lines:
         # Name / <headline> / ## Profile / - **Location:** X / ## Experience / ### Title at Company / - **Dates:** ...
    An excerpt can start mid-entry ('Key Account Manager at HP\n- **Dates:** June 2009 – Present') and often shows
    only PAST jobs, so the current role comes from entries dated '– Present', then the headline; never from the first
    'X at Y' line. Returns {roles, headline, location, past_companies}."""
    lines = [html.unescape(ln).strip() for ln in (snippet or "").splitlines()]
    roles, past, headline, prof_loc, loose_loc = [], [], None, None, None
    section, entry, prev_plain, expect_headline, orphan_current = None, None, None, False, False
    for i, ln in enumerate(lines):
        nxt = next((x for x in lines[i + 1:] if x), "")
        if not ln:
            entry, prev_plain = None, None
            continue
        if re.match(r"^#\s", ln):
            expect_headline, entry, section = True, None, "top"
            continue
        m = re.match(r"^##\s+(.+)", ln)
        if m:
            section, entry, prev_plain = m.group(1).strip().lower(), None, None
            continue
        m = re.match(r"^###\s+(.+)", ln)
        if m:
            t, c = _split_role(m.group(1))
            entry = {"title": t, "company": c, "status": "unknown", "location": None, "full": True}
            roles.append(entry)
            prev_plain = None
            continue
        m = re.match(r"^[-*\s]*\*\*Dates:\*\*\s*(.*)", ln)
        if m:
            if entry is None and prev_plain:          # excerpt started mid-entry: the line above is "Title at Co"
                t, c = _split_role(prev_plain)
                entry = {"title": t, "company": c, "status": "unknown", "location": None, "full": False}
                roles.append(entry)
            if entry is not None:
                entry["status"] = _date_status(m.group(1))
            elif _date_status(m.group(1)) == "current":
                orphan_current = True         # "**Dates:** May 2025 – Present" with its title line cut off
            continue
        m = re.match(r"^[-*\s]*\*\*Location:\*\*\s*(.*)", ln)
        if m:
            loc = _tidy(m.group(1), 80)
            if section == "profile":
                prof_loc = prof_loc or loc
            elif entry is not None:
                entry["location"] = entry["location"] or loc
            else:
                loose_loc = loose_loc or loc
            continue
        if re.match(rf"^[–—-]?\s*(?:(?:{MONTHS}\s*)?\d{{4}}\s*)?[–—-]\s*Present\b", ln, re.I):
            orphan_current = True             # "– Present": a current job whose title/company were cut off
            continue
        if expect_headline or (headline is None and nxt.startswith("## Profile")):
            headline = headline or _tidy(ln, 220)
            expect_headline = False
        prev_plain, entry = ln, None              # description text closes the entry above
    current = [r for r in roles if r["status"] == "current"]
    if any(not r["company"] and not r["title"] for r in current):
        orphan_current = True                 # a current job whose "Title at Company" line was cut off
    past = [r["company"] for r in roles if r["status"] == "past" and r["company"]]
    unknown = [r for r in roles if r["status"] == "unknown" and r["full"] and r["title"]]
    loc = prof_loc or next((r["location"] for r in current if r["location"]), None) or loose_loc
    return {"current": current, "unknown": unknown, "orphan_current": orphan_current, "past_companies": past, "headline": headline, "location": loc,
            "structured": bool(roles or headline or prof_loc)}


def _headline_role(headline):
    """'Founder KAPADA | Managing Director at ANECON GLOBAL' -> ('Managing Director', 'ANECON GLOBAL');
    a headline with a role but no company -> (role segment, None)."""
    segs = [s.strip() for s in re.split(r"\s+[|•·]\s+|\s+l\s+", headline or "") if s.strip()]
    for s in segs:
        t, c = _split_role(s)
        if t and c:
            return t, c
    seg = next((s for s in segs if ROLE_WORDS.search(s) and len(s) < 80 and not _prose(s)), None)
    return (_tidy(seg), None) if seg else (None, None)


def _short_title(t):
    """'Material Head | PPC & Production Planning | Marketing ...' -> 'Material Head' (long headlines only)."""
    if not t or len(t) <= 60:
        return t
    segs = [s.strip() for s in re.split(r"\s+[|•]\s+", t) if s.strip()]
    return next((s for s in segs if ROLE_WORDS.search(s) and not _prose(s)), None if _prose(t) else t)


def _uncut(company):
    """A company name at the end of a cut-off result title: its last word may be cut ('... at Highway Roop precision
    ...'), so drop it. None when nothing usable is left ('at Sales and ...', 'at TVS ...')."""
    words = (company or "").split()[:-1]
    while words and words[-1].lower() in ("and", "&", "of", "the", "-", "|", "for"):
        words.pop()
    name = " ".join(words)
    return name if len(name) >= 3 and not ROLE_WORDS.match(name) else None


SERP_ENTRY = re.compile(rf"([^.;·]+?)\.\s+([^.;·]+?)\.\s+({MONTHS}\s*\d{{4}})(?:\s*[-–]\s*(Present|{MONTHS}\s*\d{{4}}))?",
                        re.I)


def _serper_entries(snippet):
    """Google's dated jobs: 'Experience ; Title. Company. Jun 2019 - Present 7 years ; Title. Co. Sep 2012 - Jun 2014'
    or 'Head of Sourcing. Escorts Kubota Limited. Oct 2020 - Jun 2023 2 years'. In an 'Experience ;' list (newest
    first) an entry with only a start date is current when first, past otherwise.
    Returns ([(title, company, 'current'|'past'|'unknown')], snippet with the past entries removed)."""
    # 'Honda Motorcycle & Scooter India Pvt. Ltd. May 2013' would split at the dots of 'Pvt. Ltd.'
    rest = re.sub(r"\b(Pvt|Ltd|Co|Inc|Corp|Pte|Pty|Bros|Mfg|Engg|Intl|P)\.(?=\s*(?:Ltd|Limited|Pvt|Private|Co|Inc)\b)",
                  r"\1", snippet or "", flags=re.I)
    rest = re.sub(r"\bM/s\.\s*", "M/s ", rest, flags=re.I)
    out = []
    is_list = bool(re.search(r"Experience\s*;", rest))
    body = re.sub(r"^.*?Experience\s*;\s*", "", rest) if is_list else rest
    for i, part in enumerate(re.split(r"\s;\s", body) if is_list else [body]):
        for m in SERP_ENTRY.finditer(part):
            end = (m.group(4) or "").lower()
            status = "current" if end == "present" else "past" if end else \
                ("current" if is_list and i == 0 and not out else "past" if is_list else "unknown")
            t, c = _tidy(m.group(1).strip(" .")), _tidy(m.group(2), 90)
            if not norm_company(c or "") or norm_company(c) in ("ltd", "limited", "pvt"):
                continue                      # a fragment, not a company
            out.append((t, c, status))
            if status == "past":
                rest = rest.replace(m.group(0), " ")
    return out, rest


def _serper_experience(snippet):
    """The current (title, company) from Google's dated jobs, if any."""
    cur = next(((t, c) for t, c, st in _serper_entries(snippet)[0] if st == "current" and t), None)
    return cur or (None, None)


def parse_profile(result):
    """Parse a search result for a linkedin.com/in/ profile into name/title/company/location.
    'roles' lists every current (title, company) seen, so a caller searching one company can pick the role there;
    'past_only' is True when the excerpt shows only jobs that ended (the person has moved on, or is between jobs);
    'context' is the text that may be matched against a company name (never past jobs)."""
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
    p_title, p_company, location, roles, past_only, serp_past, serp_orphan = None, None, None, [], False, [], False
    context = f"{title} {snippet}"
    if rest:
        if " at " in rest:
            p_title, p_company = [x.strip() for x in rest.rsplit(" at ", 1)]
            if truncated:
                p_company = _uncut(p_company)   # "Hindalco Industries Limited EX ..." -> drop the cut last word
        elif ROLE_WORDS.search(rest):
            p_title = rest.strip()      # a headline like "Strategic sourcing for Defence | Purchase"
        else:
            p_company = rest.strip()    # "Deepak Kajale - Bharat Forge Ltd"
    md = parse_markdown_profile(snippet) if re.search(r"\*\*(Dates|Location):\*\*|(^|\n)#{1,3} ", snippet) else None
    if md and md["structured"]:
        # TinySearch markdown excerpt (the title is only the name)
        location = md["location"]
        roles = [(r["title"], r["company"]) for r in md["current"] if r["company"] or r["title"]]
        h_title, h_company = _headline_role(md["headline"])
        cur = next(((t, c) for t, c in roles if t), None)
        if cur:
            p_title, p_company = cur
            if h_title and h_company and not any(c and c.lower() == h_company.lower() for t, c in roles):
                roles.append((h_title, h_company))   # the headline names a current job too
        elif h_title and (h_company or not md["past_companies"]):
            p_title = h_title
            p_company = h_company or next((c for t, c in roles if c), None)
            if h_company:
                roles.append((h_title, h_company))
        elif md["unknown"] and not md["past_companies"] and not roles and not md["orphan_current"]:
            p_title, p_company = md["unknown"][0]["title"], md["unknown"][0]["company"]
            roles.append((p_title, p_company))
        elif not rest:
            p_title = p_company = None
            p_company = next((c for t, c in roles if c), None)   # current employer known, title cut off
        past_only = not roles and not md["orphan_current"] and bool(md["past_companies"])
        context = " ".join(x for x in [title if rest else "", md["headline"] or "",
                                       *[f"{t or ''} {c or ''}" for t, c in roles]] if x)
    else:
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
        elif not labelled and len(bits) >= 3 and len(bits[0]) < 60 and LOCATION_LIKE.search(bits[0]) \
                and not ROLE_WORDS.search(bits[0]) and not re.search(r"\d", bits[0]) and len(bits[1]) < 100 \
                and not re.match(r"^(Education|Experience|Location|Connections?)\b", bits[2], re.I):
            location, s_title, s_company = bits[0], bits[1], bits[2]
            if len(s_title) > len(p_title or ""):
                p_title = s_title
            if not p_company or truncated:
                p_company = s_company
        s_entries, without_past = _serper_entries(snippet)
        serp_past = [c for t, c, st in s_entries if st == "past" and c]
        # "Senior Technical Sales Head. Jun 2024 - Present." with no company: a current job, employer unknown
        serp_orphan = bool(re.search(rf"(?:^|[.;·]\s)[^.;·]+\.\s+{MONTHS}\s*\d{{4}}\s*[-–]\s*Present", snippet, re.I))
        context = f"{title} {without_past}"
        e_title, e_company = _serper_experience(snippet)
        if e_title and (truncated or not p_title or " at " not in rest):
            p_title, p_company = e_title, e_company or p_company   # the dated current job beats a cut headline
        if not rest and not p_title:
            # plain one- or two-line excerpt: "Deputy Manager Purchase at Bharat Forge Ltd"
            lines = [_tidy(ln, 160) for ln in snippet.splitlines()]
            for ln in [x for x in lines if x][:4]:
                if " at " in ln and ROLE_WORDS.search(ln.rsplit(" at ", 1)[0]) and not p_title and not _prose(ln):
                    p_title, p_company = [x.strip() for x in ln.rsplit(" at ", 1)]
                elif LOCATION_LIKE.search(ln) and len(ln) < 60 and not location:
                    location = ln.split("·")[0].strip()
                elif ROLE_WORDS.search(ln) and not p_title and len(ln) < 70 and not _prose(ln):
                    p_title = ln
        m = re.search(r"Experience:\s*([^·\n]+)", snippet)
        if m and not p_company:
            p_company = m.group(1).strip()
        m = re.search(r"Location:\s*([^·\n]+)", snippet)
        if m and not location:
            location = m.group(1).strip()
        if p_title or p_company:
            roles = [(_tidy(p_title or ""), _tidy(p_company or "", 90))]
    if not name or len(name) > 60 or any(ch.isdigit() for ch in name):
        return None
    p_title = _short_title(_tidy(p_title or ""))
    loc = _tidy(location or "", 80)
    return {"name": name.title() if name.islower() else name, "title": p_title,
            "company": _tidy(p_company or "", 90), "location": loc if loc and not loc.startswith("...") else None,
            "linkedin_url": url if "linkedin.com/in/" in url else None, "evidence_url": url,
            "snippet": snippet[:300], "roles": [(_short_title(t), c) for t, c in roles], "past_only": past_only,
            "past_companies": md["past_companies"] if md and md["structured"] else serp_past, "context": context,
            "orphan_current": bool(md and md["structured"] and md["orphan_current"]) or serp_orphan,
            "headline": md["headline"] if md and md["structured"] else None}


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
                                                 -facts[c["id"]]["direct_visits"],
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


def _role_at(p, company):
    """The title of the parsed person's CURRENT role at `company`: (title, True); (None, False) when the excerpt shows a
    current job elsewhere, or only past jobs; (None, None) when it can't tell."""
    if p.get("past_only"):
        return None, False
    match = next(((t, c) for t, c in p.get("roles") or [] if c and strict_company_match(c, company)), None)
    if match:
        t = match[0] or (p["title"] if not p["company"] or p["company"] == match[1] else None)
        return t, True
    if any(c for t, c in p.get("roles") or []):
        return None, False
    if strict_company_match(p.get("context") or "", company):
        return p["title"], True
    return None, None


def _ingest_people(con, results, ctx):
    industry = con.execute("SELECT * FROM industries WHERE id=?", (ctx["industry_id"],)).fetchone()
    icps = load_icps(industry)
    target_icp = next((i for i in icps if i["code"] == ctx["icp"]), None)
    added = 0
    for r in results:
        p = parse_profile(r)
        if not p:
            continue
        if ctx.get("company_id"):
            # keep only people who CURRENTLY work at the target company (a past job there doesn't count)
            # strict: 'Wheels, LLC' must not count as 'Wheels India' (loose matching drops a trailing 'India')
            title, works_there = _role_at(p, ctx["company"])
            if not works_there:
                continue
            company_id = ctx["company_id"]
        else:
            title = p["title"]
            if not p["company"]:
                continue
        if not title or not title_matches(title, target_icp):
            continue
        if not ctx.get("company_id"):
            company_id = db.upsert_company(con, clean_company_name(p["company"]) or p["company"],
                                           industry_id=ctx["industry_id"], sector=ctx.get("sector"))
        pid = db.upsert_person(con, company_id, p["name"], title=title, location=p["location"],
                               linkedin_url=p["linkedin_url"], source="search")
        if pid:
            db.add_evidence(con, "person", pid, "linkedin_profile", "inferred", p["evidence_url"], p["snippet"])
            added += 1
    return added


def _employer_matches(candidate, company):
    """Lenient: used before flagging someone as having moved, so a spelling variant never flags them."""
    if not candidate or not company:
        return False
    brand = norm_company(search_name(company))
    cand = norm_company(candidate)
    if strict_company_match(candidate, company) or same_company(candidate, company) \
            or (brand and (brand in cand or (len(cand) > 3 and cand in brand))):
        return True
    if _acronym_of(candidate, company) or _acronym_of(company, candidate):
        return True                     # 'HAL' ~ 'Hindustan Aeronautics Ltd (HAL)', 'L&T' ~ 'Larsen & Toubro'
    if difflib.SequenceMatcher(None, brand, cand).ratio() >= 0.88:
        return True                     # 'Raj Seals Engineering' ~ 'Raj Seal Engineering'
    # same group: 'Godrej Appliances' is part of 'Godrej & Boyce', 'TVS' of 'TVS Motor Company' (errs towards no flag)
    first = lambda n: (n.split() or [""])[0]  # noqa: E731
    if cand == first(brand) or brand == first(cand):
        return len(first(brand)) >= 3 and first(brand) not in GENERIC_FIRST
    return len(first(brand)) >= 4 and first(brand) == first(cand) and first(brand) not in GENERIC_FIRST


def _acronym_of(short, full):
    """True when `short` starts with an acronym of `full`: 'BHEL' / 'Bharat Heavy Electricals Limited',
    'L&T Construction' / 'Larsen & Toubro', or `full` carries it in brackets: 'Hindustan Aeronautics Ltd (HAL)'."""
    head = (short or "").split()[0] if (short or "").split() else ""
    ac = re.sub(r"[^a-z]", "", head.lower())
    if not (2 <= len(ac) <= 6) or not (head.isupper() or "&" in head):
        return False
    if re.search(rf"\(\s*{re.escape(ac)}\s*\)", (full or "").lower()):
        return True
    words = [w for w in re.split(r"[^a-z0-9]+", (full or "").lower()) if w and w not in ("and", "of", "the")]
    initials = "".join(w[0] for w in words)
    return len(words) >= 2 and initials.startswith(ac)


GENERIC_FIRST = {"india", "indian", "global", "shree", "shri", "sree", "new", "national", "united", "general",
                 "standard", "international", "universal", "royal", "super", "star", "asian", "bharat", "hindustan",
                 "modern", "classic", "premier", "supreme", "golden", "advanced", "precision", "tech", "techno",
                 "industrial", "engineering", "kumar", "maa", "jai", "om", "sai", "laxmi", "lakshmi", "ganesh"}


def _same_words(text, company):
    """Every word of the company's brand appears in `text`, in any order: 'Sr. KAM-East, Asahi Glass India Ltd' names
    'Asahi India Glass Limited (AIS)'. Brands of 2+ words only (one word is too loose)."""
    brand = norm_company(search_name(company)).split()
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower().replace("&", " and ")))
    return len(brand) >= 2 and all(w in words for w in brand)


def _messy(v):
    return bool(v) and (bool(re.search(r"[\n*#]|^Location\s*:|^\.\.\.|[\ue000-\uf8ff]", v)) or len(v) > 120)


def clean_company_name(name):
    """A whole headline stored as a company name by an older parser -> the company part:
    'Uma Industries | Engineering Machinery Manufacturer & Exporter | ...' -> 'Uma Industries';
    'Contrinex, a global leader in intelligent sensing ...' -> 'Contrinex'. None when the name is fine."""
    n = html.unescape(name or "").strip()
    if not (_prose(n) or " | " in n or len(n) > 60):
        return None
    core = re.split(r"\s+[|]\s*|\s*[|]\s+|\s*[—–]\s*|\s+-\s+|(?<=\w)-\s|,\s|(?<=[A-Za-z]{3})\.\s|\s+\d{4}\s|\s#|"
                    r"\s+(?:with|based|is|was|a|an|founded|which|where|i|we|looking|leading|responsible|in their|"
                    r"having|deals|dealing)\s", n, maxsplit=1, flags=re.I)[0].strip(" .,-|")
    core = re.sub(r"[^\w\s&().'’-]", "", core).strip()
    if len(core.split()) == 1 and LOCATION_LIKE.search(core):
        return None                     # 'Mumbai Looking after Key Accounts ...': the company isn't in it
    return core if 2 <= len(core) <= 60 and core != n and not _prose(core) else None


def clean_company_names(con, apply=False):
    """Rename companies whose stored name is a headline; skipped when the clean name already exists (no merge)."""
    fixed, skipped = [], []
    for c in con.execute("""SELECT DISTINCT c.id, c.name FROM companies c JOIN people p ON p.company_id=c.id
                            WHERE p.source='search'""").fetchall():
        new = clean_company_name(c["name"])
        if not new:
            continue
        key = norm_company(new)
        clash = con.execute("SELECT id FROM companies WHERE norm_key=? AND id<>?", (key, c["id"])).fetchone()
        if clash or not key:
            skipped.append(c["name"])
            continue
        fixed.append((c["name"], new))
        if apply:
            con.execute("UPDATE companies SET name=?, norm_key=? WHERE id=?", (new, key, c["id"]))
    return fixed, skipped


LEFT_FLAG = "left {}? (search excerpt shows only a past job there)"
MOVED_FLAG = "now at {} (search excerpt)"


def reparse_cached(con, industry_id=None, apply=False, max_examples=40):
    """Re-read every saved search result (search_cache) with the current parser and fix people found by search:
    clean titles/locations, use the dated current job, and flag people whose excerpts show they moved or left.
    No new searches. Profile-checked people (li_checked_at) keep their checked flag. Nothing is deleted.
    Returns counts; changes are written only when apply=True."""
    from .db import _clean_li
    by_li = {}
    for (resp,) in con.execute("SELECT response FROM search_cache"):
        try:
            rows = json.loads(resp)
        except (TypeError, ValueError):
            continue
        for r in rows if isinstance(rows, list) else []:
            li = _clean_li(r.get("url") or "")
            if li:
                by_li.setdefault(li, []).append(r)
    renamed, rename_skipped = clean_company_names(con, apply=apply)
    renames = dict(renamed)
    sql = """SELECT p.*, c.name AS company FROM people p LEFT JOIN companies c ON c.id=p.company_id
             WHERE p.source='search' AND p.linkedin_url IS NOT NULL"""
    args = ()
    if industry_id:
        sql += " AND p.company_id IN (SELECT company_id FROM company_industries WHERE industry_id=?)"
        args = (industry_id,)
    st = {"companies_renamed": len(renamed), "company_rename_skipped": len(rename_skipped),
          "people": 0, "no_saved_result": 0, "title_fixed": 0, "location_fixed": 0, "flag_left": 0, "flag_moved": 0,
          "flag_cleared": 0, "unchanged": 0}
    examples = []
    for person in con.execute(sql, args).fetchall():
        st["people"] += 1
        parses = [x for x in (parse_profile(r) for r in by_li.get(person["linkedin_url"], [])) if x]
        if not parses:
            st["no_saved_result"] += 1
            continue
        company = renames.get(person["company"], person["company"])
        at = [(t, p) for p in parses for t, c in p["roles"] if c and _employer_matches(c, company)]
        at += [(None, p) for p in parses if p.get("headline") and (strict_company_match(p["headline"], company)
                                                                    or _same_words(p["headline"], company))]
        at_titled = [(t, p) for t, p in at if t]
        elsewhere = [c for p in parses for t, c in p["roles"] if c and not _employer_matches(c, company)]
        mentions = [p for p in parses if strict_company_match(p["context"], company) or (
            p.get("headline") and _same_words(p["headline"], company))]
        left = not at and not mentions and not any(p.get("orphan_current") for p in parses) and any(
            _employer_matches(c, company) for p in parses for c in p.get("past_companies") or [])
        unclear = [p for p in parses if not p["roles"] and not p["past_only"]]
        # title: the current role at this company, else a title-only parse (headline without a company)
        title = at_titled[0][0] if at_titled else next((p["title"] for p in parses if p["title"] and not p["company"]
                                                        and not p["past_only"]), None)
        if not title:
            title = person["title"] if not _messy(person["title"]) else _short_title(_tidy(person["title"] or ""))
        loc = next((p["location"] for p in parses if p["location"]), None)
        if not loc:
            loc = person["location"] if not _messy(person["location"]) else _tidy(person["location"] or "", 80)
        flag = person["profile_flag"]
        auto = flag and ("(search excerpt" in flag)
        if not person["li_checked_at"] and (not flag or auto):
            if at or unclear or mentions:   # a current mention (result title, headline, current job) wins
                flag = None
            elif elsewhere:
                flag = MOVED_FLAG.format(elsewhere[0])
            elif left:
                flag = LEFT_FLAG.format(company)
        changes = {}
        if title != person["title"]:
            changes["title"] = title
            st["title_fixed"] += 1
        if loc != person["location"]:
            changes["location"] = loc
            st["location_fixed"] += 1
        if flag != person["profile_flag"]:
            changes["profile_flag"] = flag
            key = "flag_cleared" if not flag else ("flag_moved" if flag.startswith("now at") else "flag_left")
            st[key] += 1
        if not changes:
            st["unchanged"] += 1
            continue
        if len(examples) < max_examples:
            examples.append({"id": person["id"], "name": person["name"], "company": company,
                             "before": {k: person[k] for k in changes}, "after": changes})
        if apply:
            sets = ", ".join(f"{k}=?" for k in changes)
            con.execute(f"UPDATE people SET {sets} WHERE id=?", (*changes.values(), person["id"]))
    if apply:
        con.commit()
    st["examples"] = examples
    st["renamed_examples"] = renamed[:max_examples]
    return st


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
