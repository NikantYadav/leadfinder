"""People around a show's LinkedIn posts (Apify harvestapi actors, no login, no cookies).

1. Post search for the show: its name + year and its hashtags, sorted by RELEVANCE, so older editions are reachable
   too (checked live 2026-11-24: "IMTEX 2025" still returned posts from Jan 2025, 22 months later; sorting by date only
   reaches the newest posts). Posts are kept when they were written within POST_WINDOW_DAYS of the show.
2. The best of those posts' commenters and reactors (harvestapi~linkedin-post-comments / -post-reactions).

Who becomes what (every person must have a headline whose job title fits one of the search's personas):
  post author writing "visit us at our stall", or working at an exhibitor   -> exhibitor (likely): seller side
  post author writing about visiting                                         -> visitor_post (likely)
  commenter writing about meeting / visiting ("nice interaction with you")    -> visit_comment (likely)
  any other commenter or reactor, and authors who only mention the show      -> post_engaged (inferred)
post_engaged is NOT proof of attendance: it never makes a person "seen at a show", and the person's company is not
added to the search's company pool. Staff of the post author's company and of the show's exhibitors are never
counted as visitors.

Prices (FREE plan, read from the actors 2026-11-24): $0.002 per post, comment or reaction; $0.001 per post that
returned nothing. Raw results are cached per show in search_cache, so a re-run re-reads them for free.
"""
import json
import re
import time
from datetime import date, timedelta

from . import db
from .normalize import strict_company_match
from .people import load_icps, title_matches
from .providers import events as ev

SEARCH = "harvestapi~linkedin-post-search"
COMMENTS = "harvestapi~linkedin-post-comments"
REACTIONS = "harvestapi~linkedin-post-reactions"
PRICE, NO_RESULT = 0.002, 0.001
POSTS_PER_QUERY = 40
MAX_COMMENTS, MAX_REACTIONS = 20, 25   # per post
POST_WINDOW_DAYS = 60                  # posts this many days before the show's start / after its end
CACHE = "apify_show_posts"
MAX_PROFILE_READS = 10                 # one free-plan profile-reader run
PROFILE_PRICE = 0.004

EXHIBIT_WORDS = re.compile(   # a hall number alone is not enough: visitors and media write "Hall 3" too
    r"\b(our (stall|booth|stand|pavilion)|visit us|meet us|come (and )?(visit|see|meet) us|stall\s*(no|number|#)|"
    r"booth\s*(no|number|#)|stand\s*(no|number|#)|hall\s*(no\.?\s*)?\d+\w?\s*[,/|\-–]?\s*(stall|booth|stand)\b|"
    r"we (are|were|will be) exhibiting|we're exhibiting|thank(s| you) for visiting|"
    r"thanks? to (all|everyone) who visited|visit our (stall|booth|stand))", re.I)
VISIT_WORDS = re.compile(
    r"\b(visited|visiting|my visit|our visit|visit to|explored|walked (the|through)|attended|attending|"
    r"was at|were at|great time at|day at|days at|spent (a|the) day|went to)\b", re.I)
# a comment that says the two MET: generic praise ("great to see", "nice connecting", "great discussion") is not one
VISIT_COMMENT = re.compile(
    r"\b((nice|great|good|wonderful|lovely|pleasure|glad)\s+(meeting|to meet|interaction|talking|catching up)|"
    r"met you|meeting you|visited|visiting your|your (stall|booth|stand)|at (the|your) (stall|booth|stand)|"
    r"interaction with you|thanks? for (the|your) (time|demo|hospitality))", re.I)
FOLLOWERS = re.compile(r"^\s*[\d,.]+\s*[km]?\s+followers\s*$", re.I)
LEGAL_END = re.compile(r"^(.+?\b(?:pvt\.?\s*ltd\.?|private limited|limited|ltd\.?|gmbh|inc\.?|llc|llp|ag|s\.?a\.?|"
                       r"co\.)(?=\s|$))", re.I)


def split_headline(head):
    """'Deputy Plant Manager at Taural India Private Limited' -> ('Deputy Plant Manager', 'Taural India Private Limited').
    'SUPPLY CHAIN @ ALLCON METALS | ...' -> ('SUPPLY CHAIN', 'ALLCON METALS'). A company page ('8,113 followers') or an
    empty headline -> (None, None). No company in the first segment -> (segment, None)."""
    head = re.sub(r"\s+", " ", head or "").strip()
    if not head or FOLLOWERS.match(head):
        return None, None
    first = re.split(r"\s*\|+\s*|\s[•·]\s", head)[0].strip(" .,-")
    m = re.match(r"^(.*?)(?:\s(?:at|bei|chez)\s|\s?@\s?)(.+)$", first, re.I)
    if not m:
        # 'Sales Director - Deep Pneumatics Pvt. Ltd', 'DGM - Domestic Sales (Shakti Pumps India Limited)': a company
        # only when it carries a legal suffix, so 'Manager - Domestic Sales' is never read as one
        for part in reversed(re.split(r"\s[-–]\s|\(|\)", first)):
            legal = LEGAL_END.match(part.strip(" .,"))
            if legal and part.strip() != first:
                title = first[:first.index(part)].strip(" ,-–(")
                return _short(title or first), legal.group(1)[:80]
        return _short(first), None
    title, comp = m.group(1).strip(" ,-"), m.group(2).strip(" .,-")
    legal = LEGAL_END.match(comp)
    comp = legal.group(1) if legal else re.split(r"\.\s|\s[-–]\s|,|\s\(", comp)[0].strip(" .")
    return _short(title or first), (comp[:80] or None)


def _short(title, n=90):
    """Long keyword-list headlines -> the first ~90 characters, cut at a comma or word."""
    title = (title or "").strip()
    if len(title) <= n:
        return title or None
    cut = title[:n]
    return (cut[:cut.rfind(",")] if "," in cut[20:] else cut[:cut.rfind(" ")]).strip(" ,") or cut


def queries(show):
    """LinkedIn search-bar queries: '"IMTEX 2025"', '#IMTEX2025', '#IMTEX' (the last is filtered by date)."""
    year = str(show.get("year") or "")
    name = show["name"] if year and year in show["name"] else f'{show["name"]} {year}'.strip()
    base = re.sub(r"\b(19|20)\d\d\b", "", show.get("series") or show["name"]).strip(" -")
    tag = re.sub(r"[^A-Za-z0-9]", "", base)
    out = [f'"{name}"']
    if tag and year:
        out.append(f"#{tag}{year}")
    if tag and len(tag) >= 4:
        out.append(f"#{tag}")
    return list(dict.fromkeys(out))


def window(show):
    s = date.fromisoformat(show["start_date"][:10])
    e = date.fromisoformat((show.get("end_date") or show["start_date"])[:10])
    return s - timedelta(days=POST_WINDOW_DAYS), e + timedelta(days=POST_WINDOW_DAYS)


def _posted(p):
    d = (p.get("postedAt") or {}).get("date") if isinstance(p.get("postedAt"), dict) else p.get("postedAt")
    try:
        return date.fromisoformat(str(d)[:10])
    except (TypeError, ValueError):
        return None


def _is_company(actor):
    return (actor.get("type") == "company" or "/company/" in (actor.get("linkedinUrl") or "")
            or FOLLOWERS.match(actor.get("position") or actor.get("info") or "") is not None)


def _engagement(p):
    e = p.get("engagement") or {}
    return int(e.get("likes") or 0), int(e.get("comments") or 0)


def _post_url(it):
    q = it.get("query")
    return (q.get("post") if isinstance(q, dict) else None) or it.get("postUrl") or it.get("post_url")


# ---------- classification (pure: tested on real output) ----------
GENERIC = {"india", "international", "expo", "exhibition", "world", "global", "annual", "conference", "show", "fair",
           "trade", "summit", "the", "oil", "gas", "energy", "week", "asia", "middle", "east", "africa", "europe"}


def _key(show):
    t = re.sub(r"\b(19|20)\d\d\b", " ", (show.get("series") or show["name"] or "").lower())
    return " ".join(re.sub(r"[^a-z0-9&]+", " ", t).split())


def mentions(show, text):
    """Does the post name this show's series? 'IMTEX FORMING 2026' -> 'imtexforming' anywhere, or its first word
    ('imtex') when that word is distinctive (not 'india', 'expo', 'oil', ...)."""
    key = _key(show)
    if not key:
        return False
    low = (text or "").lower()
    tok = key.split()[0]
    # 'As we close 2025 ... IMTEX 2025' posted a month before IMTEX FORMING 2026 is about the 2025 edition: a year
    # written right after the series name must be this show's year
    year = str(show.get("year") or (show.get("start_date") or "")[:4])
    named = set(re.findall(rf"{re.escape(tok)}[\s\-_]*(?:[a-z]+[\s\-_]*)?((?:19|20)\d\d)\b", low))
    if named and year not in named:
        return False
    if re.sub(r"[^a-z0-9]", "", key) in re.sub(r"[^a-z0-9]", "", low):
        return True
    return len(tok) >= 4 and tok not in GENERIC and re.search(rf"\b#?{re.escape(tok)}", low) is not None


def show_for(d, text, shows):
    """The search's show a post belongs to: posted within its window and naming its series; closest start first.
    A post search can't be bounded above, so a search for an older show also returns the next editions' posts:
    those go to that edition instead of being thrown away."""
    fits = [s for s in shows if s.get("start_date") and window(s)[0] <= d <= window(s)[1] and mentions(s, text)]
    return min(fits, key=lambda s: abs((date.fromisoformat(s["start_date"][:10]) - d).days)) if fits else None


def classify_posts(items, shows, exhibitors):
    """Post-search rows -> (posts kept, author candidates). shows: the search's dated core shows (dicts);
    exhibitors: {show_id: [(company_id, name)]}. Each kept post carries the show it belongs to. Who fits a persona is
    decided later (decide(), by Sonnet), so every person who wrote a kept post is a candidate."""
    posts, authors = [], []
    seen_urls = set()
    for p in items:
        if p.get("type") not in (None, "post") or not p.get("linkedinUrl") or p["linkedinUrl"] in seen_urls:
            continue
        d = _posted(p)
        text = p.get("content") or ""
        show = show_for(d, text, shows) if d else None
        if not show:
            continue
        seen_urls.add(p["linkedinUrl"])
        exh = exhibitors.get(show["id"], [])
        a = p.get("author") or {}
        exhibit = bool(EXHIBIT_WORDS.search(text))
        rec = {"url": p["linkedinUrl"], "posted": d.isoformat(), "text": text[:300], "author": a.get("name"),
               "author_url": a.get("linkedinUrl"), "likes": _engagement(p)[0], "comments": _engagement(p)[1],
               "show_id": show["id"], "show_name": show["name"]}
        if _is_company(a):
            rec.update(kind="company", company=a.get("name"),
                       exhibitor=exhibit or bool(_exhibitor(a.get("name"), exh)))
        else:
            title, comp = split_headline(a.get("info"))
            rec.update(kind="profile", company=comp, exhibitor=exhibit or bool(_exhibitor(comp, exh)))
            if a.get("name"):
                authors.append({"src": "author", "name": a["name"], "headline": a.get("info"), "title": title,
                                "company": comp, "linkedin_url": a.get("linkedinUrl"), "text": text[:400],
                                "how": "wrote the post", "exhibit_words": exhibit, "show_id": show["id"],
                                "url": p["linkedinUrl"], "snippet": f"posted about {show['name']}: {text[:200]}"})
        posts.append(rec)
    return posts, authors


def pick_posts(posts, budget_usd, prefer=None):
    """Posts whose commenters / reactors are worth paying for, best first, within budget_usd: the show being read
    (`prefer`, a show id) before other editions; exhibitors' own show posts ('thank you for visiting our stall')
    draw the visitors; then the most engaged."""
    order = sorted(posts, key=lambda p: (p.get("show_id") != prefer if prefer else False,
                                         -(p["exhibitor"] or bool(VISIT_WORDS.search(p["text"]))),
                                         -(p["likes"] + 3 * p["comments"])))
    comments, reactions, spent = [], [], 0.0
    for p in order:
        nc, nr = min(p["comments"], MAX_COMMENTS), min(p["likes"], MAX_REACTIONS)
        cost = (nc + nr) * PRICE
        if not cost:
            continue
        if spent + cost > budget_usd:
            break
        spent += cost
        if nc:
            comments.append(p["url"])
        if nr:
            reactions.append(p["url"])
    return comments, reactions, round(spent, 3)


def classify_engagers(items, posts):
    """Comment / reaction rows -> candidates. Company pages, authors answering on their own post, and duplicates
    (one person, one post, same words) are dropped."""
    by_url = {p["url"]: p for p in posts}
    out, dup = [], set()
    for it in items:
        if it.get("error") or it.get("type") not in (None, "comment", "reaction"):
            continue
        actor = it.get("actor") or it.get("author") or {}
        name, post = actor.get("name"), by_url.get(_post_url(it))
        if not name or not post or _is_company(actor) or name == post.get("author"):
            continue
        text = (it.get("commentary") or it.get("text") or "").strip()
        k = (name.lower(), post["url"], text)
        if k in dup:
            continue
        dup.add(k)
        head = actor.get("position") or actor.get("info")
        title, comp = split_headline(head)
        is_comment = it.get("type") == "comment" or bool(text)
        who = post.get("author") or "a"
        out.append({"src": "comment" if is_comment else "reaction", "name": name, "headline": head, "title": title,
                    "company": comp, "linkedin_url": actor.get("linkedinUrl"), "text": text[:400],
                    "how": "commented" if is_comment else "reacted to the post", "post_author": who,
                    "post_company": post.get("company"), "show_id": post["show_id"],
                    "url": (it.get("linkedinUrl") if is_comment else None) or post["url"],
                    "snippet": (f'commented on {who}\'s post about {post["show_name"]}: "{text[:200]}"' if is_comment
                                else f"reacted ({(it.get('reactionType') or 'like').lower()}) to {who}'s post about "
                                     f"{post['show_name']}")})
    return out


def fallback_verdict(c, icps):
    """Without Sonnet: whole-word title keywords for the group, wording patterns for at_show."""
    group = next((i["code"] for i in icps if _fits(c.get("title"), [i])), None)
    text = c.get("text") or ""
    if c["src"] == "author":
        at = "exhibited" if c.get("exhibit_words") else ("visited" if VISIT_WORDS.search(text) else "none")
    else:
        at = "visited" if c["src"] == "comment" and VISIT_COMMENT.search(text) else "none"
    if not group and at == "visited":
        group = "unclear"   # worth a profile read: a visit with a headline that says nothing
    return {"group": group, "role": c.get("title"), "company": c.get("company"), "at_show": at}


def decide(candidates, verdicts, exhibitors, icps):
    """Candidates + verdicts ({judge.key: verdict}; missing -> keyword fallback) -> person records with a group
    (icp) and an evidence kind. Who exhibits and who is the post author's colleague is checked here, in code, from
    the employer: a commenter from the exhibiting company is never a visitor."""
    from .judge import key
    out = []
    for c in candidates:
        v = verdicts.get(key(c)) or fallback_verdict(c, icps)
        if not v.get("group"):
            continue
        comp = v.get("company") or c.get("company")
        ex = _exhibitor(comp, exhibitors.get(c["show_id"], []))
        colleague = bool(comp and c.get("post_company") and strict_company_match(comp, c["post_company"]))
        at = v.get("at_show") or "none"
        if c["src"] == "author":
            kind = ("exhibitor" if (c.get("exhibit_words") or ex or at == "exhibited")
                    else "visitor_post" if at == "visited" else "post_engaged")
        else:
            kind = "visit_comment" if (c["src"] == "comment" and at == "visited" and not ex and not colleague) \
                else "post_engaged"
        if v["group"] == "unclear":
            if kind not in ("visit_comment", "visitor_post"):
                continue          # only a real visit is worth paying to read the profile
        out.append({**c, "title": v.get("role") or c.get("title"), "company": comp, "icp": v["group"],
                    "kind": kind, "company_is_exhibitor": bool(ex), "needs_profile": v["group"] == "unclear"})
    return out


def profile_candidates(people, profiles):
    """needs_profile records + read profiles -> fresh candidates whose headline is the profile's current job."""
    from .providers.linkedin import _slug
    out = []
    for r in people:
        prof = profiles.get((_slug(r.get("linkedin_url")) or "").lower()) if r.get("needs_profile") else None
        if prof and (prof.get("title") or prof.get("headline")):
            head = " at ".join(x for x in (prof.get("title") or prof.get("headline"), prof.get("company")) if x)
            out.append({**{k: r[k] for k in r if k not in ("icp", "kind", "needs_profile")}, "headline": head,
                        "title": prof.get("title") or split_headline(prof.get("headline"))[0],
                        "company": prof.get("company") or r.get("company"), "country": prof.get("country")})
    return out


def _fits(title, icps):
    """title_matches, but on whole words: headlines are free text ('outsourcing' is not 'sourcing')."""
    if not title or not any(title_matches(title, i) for i in icps):
        return False
    t = title.lower()
    for i in icps:
        if any(k.lower() in t for k in i.get("exclude_keywords", [])):
            continue
        if any(re.search(rf"(?<![a-z]){re.escape(k.lower())}(?![a-z])", t) for k in i.get("title_keywords", [])):
            return True
    return False


def _exhibitor(comp, exhibitors):
    if not comp:
        return None
    return next((cid for cid, n in exhibitors if strict_company_match(comp, n)), None)


# ---------- storing ----------
CONF = {"exhibitor": "likely", "visitor_post": "likely", "visit_comment": "likely", "post_engaged": "inferred"}


def ingest(con, ind, posts, people, exhibitors, icps=None):
    """Company posts with stall wording -> exhibitor evidence; people -> people + evidence + their group (the one
    Sonnet chose) for this search. Only seen people's companies join the search's company pool (their colleagues are
    searched later); engaged people's do not. A group is locked only where the person would pass it anyway: seen
    people, engaged people in groups with a visiting/buying rule, or whose company already meets the group's rule;
    otherwise build_leads decides from the title as for anyone else."""
    from .people import ENGAGED_RULES, company_facts, company_rule_ok, effective_min_shows
    rules = {i["code"]: i.get("company_rule") for i in icps or []}
    min_shows = effective_min_shows(ind) if icps else 0
    locked = {pid for pid, (_, lk) in db.groups_for(con, ind["id"]).items() if lk}
    stats = {"exhibitor_posts": 0, "people": 0, "seen": 0, "engaged": 0, "by_show": {}}
    names = {p["show_id"]: p["show_name"] for p in posts}
    for p in posts:
        if p["kind"] == "company" and p["exhibitor"] and p["company"]:
            cid = _exhibitor(p["company"], exhibitors.get(p["show_id"], [])) or \
                db.upsert_company(con, p["company"], industry_id=ind["id"])
            db.add_evidence(con, "company", cid, "exhibitor", "likely", p["url"],
                            f"LinkedIn post about {p['show_name']}: {p['text'][:200]}", p["show_id"])
            stats["exhibitor_posts"] += 1
    for r in people:
        if r.get("needs_profile"):
            continue
        seen = r["kind"] != "post_engaged"
        cid = None
        if r["company"]:
            cid = _exhibitor(r["company"], exhibitors.get(r["show_id"], [])) or db.upsert_company(
                con, r["company"], industry_id=ind["id"] if seen else None)
        pid = db.upsert_person(con, cid, r["name"], r["title"], r.get("country"), r["linkedin_url"],
                               source="apify_show_posts")
        if pid is None:
            continue
        db.add_evidence(con, "person", pid, r["kind"], CONF[r["kind"]], r["url"], r["snippet"], r["show_id"])
        if cid and r["kind"] in ("visitor_post", "visit_comment") and not r["company_is_exhibitor"]:
            db.add_evidence(con, "company", cid, r["kind"], "inferred", r["url"], r["snippet"], r["show_id"])
        if cid and r["kind"] == "exhibitor":
            db.add_evidence(con, "company", cid, "exhibitor", "likely", r["url"], r["snippet"], r["show_id"])
        g = r.get("icp")
        if g in rules and pid not in locked and (
                seen or rules[g] in ENGAGED_RULES
                or (cid and company_rule_ok(rules[g], company_facts(con, cid), min_shows))):
            db.set_group(con, pid, ind["id"], g, locked=True)
        stats["people"] += 1
        stats["seen" if seen else "engaged"] += 1
        n = names.get(r["show_id"], str(r["show_id"]))
        stats["by_show"][n] = stats["by_show"].get(n, 0) + 1
    con.commit()
    return stats


def show_exhibitors(con, show_ids):
    out = {i: [] for i in show_ids}
    for r in con.execute(f"""SELECT DISTINCT e.show_id, c.id, c.name FROM companies c
                             JOIN evidence e ON e.entity='company' AND e.entity_id=c.id
                             WHERE e.kind='exhibitor' AND e.show_id IN ({",".join("?" * len(show_ids))})""", show_ids):
        out[r["show_id"]].append((r["id"], r["name"]))
    return out


def sibling_shows(con, ind, show):
    """The show itself plus the search's other dated core shows, so posts about another edition are kept for it."""
    rows = [dict(r) for r in con.execute("""SELECT * FROM shows WHERE industry_id=? AND start_date IS NOT NULL
                                            AND (origin IS NOT 'evidence' OR id=?)""", (ind["id"], show["id"]))]
    return [show] + [r for r in rows if r["id"] != show["id"]]


# ---------- running ----------
def _cached(con, show_id, phase):
    row = con.execute("SELECT response FROM search_cache WHERE provider=? AND query=? AND params=?",
                      (CACHE, f"show:{show_id}", phase)).fetchone()
    return json.loads(row["response"]) if row else None


def _cache(con, show_id, phase, items):
    con.execute("INSERT OR REPLACE INTO search_cache VALUES (?,?,?,?,?)",
                (CACHE, f"show:{show_id}", phase, json.dumps(items), db.now()))
    con.commit()


def _run(con, show, phase, actor, body, cap, refs, on_start, deadline, log):
    """One Apify run, adopting a run a stopped attempt started (refs[phase]). Returns (rows, cost)."""
    from .showdata import _spent

    def started(ref):
        refs[phase] = ref
        if on_start:
            on_start(refs)
    rows, run, _ = ev.run_actor(actor, body, cap, deadline, log, ref=refs.get(phase), on_start=started)
    _spent(con, ev.cost(run))
    refs.pop(phase, None)
    err = next((r.get("error") for r in rows if r.get("error") and not r.get("type")), None)
    if err and len(rows) == 1:
        raise RuntimeError(f"{actor.split('~')[-1]}: {str(err)[:200]}")
    return rows, ev.cost(run)


def _post_rows(con, phase, url):
    row = con.execute("SELECT response FROM search_cache WHERE provider=? AND query=? AND params=?",
                      (CACHE, "post:" + url, phase)).fetchone()
    return json.loads(row["response"]) if row else None


def _engage(con, show, phase, actor, urls, per, refs, on_start, deadline, log):
    """Comments or reactions of `urls`. Cached per post (also posts that had none), so a post found by any show's
    search is never paid for twice. Returns (rows of all urls, cost)."""
    have = {u: _post_rows(con, phase, u) for u in urls}
    missing = [u for u, rows in have.items() if rows is None]
    cost = 0.0
    if missing:
        cap = round(len(missing) * (per * PRICE + NO_RESULT) + 0.02, 2)
        body = {"posts": missing, "maxItems": per, "profileScraperMode": "short",
                **({"scrapeReplies": False} if phase == "comments" else {})}
        rows, cost = _run(con, show, phase, actor, body, cap, refs, on_start, deadline, log)
        for u in missing:
            have[u] = [r for r in rows if _post_url(r) == u]
            con.execute("INSERT OR REPLACE INTO search_cache VALUES (?,?,?,?,?)",
                        (CACHE, "post:" + u, phase, json.dumps(have[u]), db.now()))
        con.commit()
    return [r for u in urls for r in have[u]], cost


def read_show(con, ind, show, budget_usd, deadline=None, log=print, refs=None, on_start=None):
    """Post search -> commenters / reactors of the best posts -> people. refs: run refs saved by an earlier, stopped
    attempt ({phase: ref}), adopted instead of paying twice. Everything read is cached per show, so a re-run only
    pays for what is new. Returns counts and the USD spent."""
    show = dict(show)
    refs = dict(refs or {})
    icps = load_icps(db.get_industry(con, ind["slug"]))
    shows = sibling_shows(con, ind, show)
    exhibitors = show_exhibitors(con, [s["id"] for s in shows])
    qs = queries(show)
    lo, _ = window(show)
    search_cap = round(len(qs) * POSTS_PER_QUERY * PRICE + len(qs) * NO_RESULT + 0.02, 2)
    if budget_usd < search_cap:
        return {"error": f"budget ${budget_usd:.2f} is below the post search's ${search_cap:.2f}"}
    spent, warnings = 0.0, []
    items = _cached(con, show["id"], "search")
    if items is None:
        if not refs.get("search"):
            log(f"  {show['name']}: searching LinkedIn posts ({', '.join(qs)}; up to ${search_cap:.2f})")
        body = {"searchQueries": qs, "maxPosts": POSTS_PER_QUERY, "sortBy": "relevance",
                "postedLimitDate": lo.isoformat(), "profileScraperMode": "short",
                "scrapeReactions": False, "scrapeComments": False}
        items, spent = _run(con, show, "search", SEARCH, body, search_cap, refs, on_start, deadline, log)
        _cache(con, show["id"], "search", items)
    posts, authors = classify_posts(items, shows, exhibitors)
    # the search's cap is reserved even when its result came from the cache, so a re-run never spends more
    c_urls, r_urls, est = pick_posts(posts, max(budget_usd - search_cap, 0), prefer=show["id"])
    if c_urls or r_urls:
        log(f"  {show['name']}: {len(posts)} posts about this search's shows; comments on {len(c_urls)} and "
            f"reactions on {len(r_urls)} of them (up to ${est:.2f}; posts read before are free)")
    engagers = []
    for phase, actor, urls, per in (("comments", COMMENTS, c_urls, MAX_COMMENTS),
                                    ("reactions", REACTIONS, r_urls, MAX_REACTIONS)):
        try:
            rows, cost = _engage(con, show, phase, actor, urls, per, refs, on_start, deadline, log)
        except Exception as e:  # noqa: BLE001 - authors already found are kept; the other phase may still work
            warnings.append(f"{phase}: {str(e)[:160]}")
            continue
        spent += cost
        engagers += rows
    from . import judge
    cands = authors + classify_engagers(engagers, posts)
    verdicts, how = judge.judge(con, ind, icps, cands, show_word=show["name"], log=log)
    people = decide(cands, verdicts, exhibitors, icps)
    profiles = _cached(con, show["id"], "profiles") or {}
    from .providers.linkedin import _slug
    vague = [r["linkedin_url"] for r in people if r.get("needs_profile") and r.get("linkedin_url")
             and (_slug(r["linkedin_url"]) or "").lower() not in profiles][:MAX_PROFILE_READS]
    if vague:
        from .providers import linkedin as lip
        log(f"  {show['name']}: reading {len(vague)} LinkedIn profile(s) of people who wrote about meeting at "
            "the show but whose headline gives no job title")
        try:
            got = lip.check_profiles(vague, con=con, log=log)
            spent += len(vague) * PROFILE_PRICE
            # profiles that came back empty are remembered too, so they are not paid for again
            profiles.update({(_slug(u) or "").lower(): {} for u in vague})
            profiles.update(got)
            _cache(con, show["id"], "profiles", profiles)
        except Exception as e:  # noqa: BLE001 - those few people are left out; everyone else is kept
            warnings.append(f"profiles: {str(e)[:160]}")
    again = profile_candidates(people, profiles)
    if again:   # judged once more, now with the real job title and employer
        v2, _ = judge.judge(con, ind, icps, again, show_word=show["name"], log=log)
        people += [r for r in decide(again, v2, exhibitors, icps) if not r["needs_profile"]]
    stats = ingest(con, ind, posts, people, exhibitors, icps)
    stats["persona_check"] = how
    return {**stats, "posts_in_window": sum(1 for p in posts if p["show_id"] == show["id"]),
            "posts_for_other_editions": sum(1 for p in posts if p["show_id"] != show["id"]),
            "posts_returned": sum(1 for i in items if i.get("type") in (None, "post")),
            "engagements_read": len(engagers), "cost": round(spent, 3),
            **({"warning": "; ".join(warnings)} if warnings else {})}
