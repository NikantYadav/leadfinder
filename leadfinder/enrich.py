"""Phase 2: spend free credits only on the best leads.

  verify-profiles  LinkedIn API check: is the person in a target country and still at the company?
  resolve-domains  company -> website domain (Hunter's free domain finder, else a Serper search guess)
  enrich-emails    email waterfall Prospeo -> Hunter -> Apollo -> Tomba -> BetterContact, stop at first good email
  posts-api        LinkedIn post search (Apify) -> post_candidates for Claude to classify
"""
import re
from datetime import datetime, timedelta, timezone

from . import config, db, keys, targeting
from .normalize import _flat, norm_company, norm_domain, same_company, search_name, strict_company_match
from .providers import deepline as dlp
from .providers import email as emailp
from .providers import linkedin as lip
from .providers import search as router
from .providers.search import ProviderBusy, ProviderError

DIRECTORY_DOMAINS = {"linkedin.com", "wikipedia.org", "en.wikipedia.org", "zaubacorp.com", "justdial.com",
                     "indiamart.com", "tradeindia.com", "facebook.com", "crunchbase.com", "zoominfo.com",
                     "glassdoor.com", "glassdoor.co.in", "ambitionbox.com", "tofler.in", "economictimes.indiatimes.com",
                     "moneycontrol.com", "bloomberg.com", "youtube.com", "instagram.com", "x.com", "twitter.com",
                     "naukri.com", "dnb.com", "rocketreach.co", "apollo.io", "theorg.com", "nseindia.com",
                     "bseindia.com", "screener.in", "google.com", "maps.google.com"}


def target_countries(ind=None):
    """Per search (form targeting) when given, else TARGET_COUNTRIES from .env."""
    return targeting.target_countries(ind)


def _cool(con, provider, hours=24):
    until = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute("""INSERT INTO provider_state(provider, cooldown_until, last_used) VALUES (?,?,?)
                   ON CONFLICT(provider) DO UPDATE SET cooldown_until=excluded.cooldown_until""", (provider, until, db.now()))
    con.commit()


def _cooling(con, provider):
    row = con.execute("SELECT cooldown_until FROM provider_state WHERE provider=?", (provider,)).fetchone()
    return row and row["cooldown_until"] and row["cooldown_until"] > db.now()


def _count(con, provider, n=1):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    con.execute("""INSERT INTO credit_usage(provider, month, used) VALUES (?,?,?)
                   ON CONFLICT(provider, month) DO UPDATE SET used=used+?""", (provider, month, n, n))


def select_leads(con, industry, top=50, icps=None, min_score=0):
    from .export import build_leads
    leads = [l for l in build_leads(con, industry) if l["score"] >= min_score]
    if icps:
        leads = [l for l in leads if l["icp"] in icps]
    return leads[:top] if top else leads


def profile_flag(profile, company_name, ind=None):
    flags = []
    tc = target_countries(ind)
    country = (profile.get("country") or "").lower()
    code = (profile.get("country_code") or "").lower()
    if (country or code) and country not in tc and code not in tc:
        flags.append(f"outside target geo ({profile.get('country') or profile.get('country_code')})")
    cur = profile.get("company")
    if cur and company_name:
        brand = norm_company(search_name(company_name))
        if not (same_company(cur, company_name) or norm_company(cur).startswith(brand)
                or brand in norm_company(cur)):
            flags.append(f"now at {cur}")
    return "; ".join(flags) or None


# ---------------- profiles ----------------
def verify_profiles(con, industry, top=50, icps=None, recheck=False, log=print):
    leads = [l for l in select_leads(con, industry, top, icps) if l["linkedin_url"]]
    if not recheck:
        leads = [l for l in leads if not l.get("li_checked_at")]
    if not leads:
        return {"checked": 0, "note": "nothing to check (no LinkedIn URLs, or already checked)"}
    by_slug = {lip._slug(l["linkedin_url"]): l for l in leads}
    st = {"flagged": 0, "checked": 0}

    def save(profiles):  # called after every batch and committed, so a Stop keeps what was already paid for
        for slug, prof in profiles.items():
            lead = by_slug.get(slug)
            if not lead:
                continue
            flag = profile_flag(prof, lead["company"], industry)
            st["flagged"] += bool(flag)
            st["checked"] += 1
            con.execute("""UPDATE people SET country=COALESCE(?, country), location=COALESCE(location, ?),
                           li_company=?, profile_flag=?, li_checked_at=? WHERE id=?""",
                        (prof.get("country") or prof.get("country_code"), prof.get("city"), prof.get("company"),
                         flag, db.now(), lead["id"]))
            if prof.get("url"):
                db.add_evidence(con, "person", lead["id"], "linkedin_profile", "inferred", prof["url"],
                                f"{prof.get('headline') or ''} | {prof.get('company') or ''} | {prof.get('city') or ''}")
            _count(con, prof["source"])
        con.commit()
    try:
        lip.check_profiles([l["linkedin_url"] for l in leads], on_batch=save)
    except (ProviderBusy, ProviderError) as e:
        return {"checked": st["checked"], "error": str(e)}
    flagged, checked = st["flagged"], st["checked"]
    return {"checked": checked, "flagged": flagged, "requested": len(leads)}


# ---------------- domains ----------------
GENERIC_WORDS = {"india", "auto", "parts", "motor", "motors", "company", "ltd", "limited", "the", "and", "of",
                  "products", "industries", "engineering", "systems", "technologies", "electronics", "construction"}
BAD_HOST_PARTS = ("google.", ".gov.", ".gov", ".nic.in", "groww.", "wikipedia.", "shop.")


def domain_match_score(company, host):
    """>0 when the host plausibly belongs to the company: contains a distinctive brand word or its initials."""
    if not host or host in DIRECTORY_DOMAINS or any(host.endswith("." + x) for x in DIRECTORY_DOMAINS) \
            or any(b in host for b in BAD_HOST_PARTS):
        return 0
    import re
    words = norm_company(search_name(company)).split()
    tokens = [w for w in words if len(w) >= 3 and w not in GENERIC_WORDS]
    flat = host.replace("-", "").replace(".", " ")
    first_label = host.split(".")[0].replace("-", "")
    hits = sum(1 for t in tokens if t in flat)
    acronyms = {"".join(w[0] for w in words if w not in ("and", "the", "of"))}
    acronyms |= {m.lower() for m in re.findall(r"\(([A-Za-z&]{2,6})\)", company or "")}  # "(BEL)" -> bel
    acronym_ok = any(len(a) >= 3 and first_label.startswith(a.replace("&", "")) for a in acronyms)
    first_ok = bool(tokens) and tokens[0] in flat
    if not (acronym_ok or first_ok or hits >= 2):
        return 0
    score = 10 * max(hits, 1) - len(host) / 10
    if host.endswith((".com", ".in", ".co.in")):
        score += 1
    if host.count(".") > 2:
        score -= 3  # sub-sub-domains (aircraft.airbus.com) are less likely the main site
    return score


def _guess_domain(con, company):
    results, _ = router.search(con, f'"{search_name(company)}" official website', num=10)
    best, best_score = None, 0
    for r in results:
        d = norm_domain(r["url"])
        s = domain_match_score(company, d)
        if s > best_score:
            best, best_score = d, s
    return best


def resolve_domains(con, industry, limit=50, log=print, redo_guesses=False):
    if redo_guesses:
        con.execute("UPDATE companies SET domain=NULL, domain_source=NULL WHERE domain_source='search_guess'")
    rows = con.execute("""SELECT DISTINCT c.* FROM companies c JOIN people p ON p.company_id=c.id
                          JOIN company_industries ci ON ci.company_id=c.id
                          WHERE ci.industry_id=? AND p.icp IS NOT NULL AND (c.domain IS NULL OR c.domain='')
                          LIMIT ?""", (industry["id"], limit)).fetchall()
    hunter = emailp.Hunter()
    stats = {"companies": len(rows), "hunter": 0, "search_guess": 0, "unresolved": []}
    for c in rows:
        dom, src = None, None
        if hunter.available() and not _cooling(con, "hunter_domain"):
            try:
                dom = keys.run("hunter", lambda k: hunter.domain_for(search_name(c["name"]), k), con)
                src = "hunter" if dom else None
            except ProviderBusy:
                _cool(con, "hunter_domain")
            except Exception as e:  # noqa: BLE001
                log(f"  hunter domain-finder error for {c['name']}: {e}")
        if not dom:
            try:
                dom = _guess_domain(con, c["name"])
                src = "search_guess" if dom else None
            except ProviderBusy as e:
                log(f"  search busy: {e}")
                break
        dom = norm_domain(dom) if dom else None
        if dom:
            con.execute("UPDATE companies SET domain=?, domain_source=? WHERE id=?", (dom, src, c["id"]))
            stats[src] += 1
            log(f"  {c['name']} -> {dom} ({src})")
        else:
            stats["unresolved"].append(c["name"])
        con.commit()
    return stats


# ---------------- emails ----------------
def enrich_emails(con, industry, top=30, icps=None, include_flagged=False, accept=("verified", "catch_all"),
                  only=None, log=print):
    leads = select_leads(con, industry, top=None, icps=icps)
    todo = [l for l in leads if not l.get("email") and (include_flagged or not l.get("profile_flag"))][:top]
    providers = [p for p in emailp.PROVIDERS if p.available() and (not only or p.name in only)]
    if not providers:
        return {"error": "no email provider keys in .env (PROSPEO/HUNTER/APOLLO/TOMBA/BETTERCONTACT)"}
    exhausted = set()  # services whose every key is resting (each key rests on its own in keys.py)
    stats = {"tried": 0, "found": 0, "by_provider": {}, "statuses": {}, "exhausted": sorted(exhausted)}
    for l in todo:
        comp = con.execute("SELECT name, domain, domain_source FROM companies WHERE id=?", (l["company_id"],)).fetchone()
        query = {"name": l["name"], "company": search_name(comp["name"]), "domain": comp["domain"],
                 "linkedin_url": l["linkedin_url"],
                 # a search-guessed domain must not override the company name for providers that resolve it
                 "domain_trusted": bool(comp["domain"]) and comp["domain_source"] != "search_guess"}
        stats["tried"] += 1
        best = None
        for p in providers:
            if p.name in exhausted:
                continue
            done = con.execute("SELECT 1 FROM email_attempts WHERE person_id=? AND provider=?",
                               (l["id"], p.name)).fetchone()
            if done:
                continue
            used_key = []

            def call(k, p=p):
                used_key.append(k)
                return p.find(query, k) if k else p.find(query)
            try:
                res = keys.run(p.name, call, con, log)
            except ProviderBusy as e:
                log(f"  {p.name}: every key is out of credits / rate limited ({e}) - skipping it for now")
                exhausted.add(p.name)
                continue
            except (ProviderError, Exception) as e:  # noqa: BLE001
                log(f"  {p.name}: error {str(e)[:120]}")
                continue
            con.execute("INSERT OR REPLACE INTO email_attempts VALUES (?,?,?,?)",
                        (l["id"], p.name, (res or {}).get("status") or "none", db.now()))
            if res:
                _count(con, p.name)
                if used_key[-1]:
                    keys.count(used_key[-1], 1, con)
            con.commit()
            if not res or res["status"] == "invalid":
                continue
            if best is None or ["unknown", "catch_all", "verified"].index(res["status"]) > \
                    ["unknown", "catch_all", "verified"].index(best["status"]):
                best = res
            if res["status"] in accept:
                break
        if best:
            ex = best.get("extra") or {}
            flag = None
            if ex.get("country") and ex["country"].lower() not in target_countries(industry):
                flag = f"outside target geo ({ex['country']})"
            con.execute("""UPDATE people SET email=?, email_status=?, email_source=?, country=COALESCE(country, ?),
                           profile_flag=COALESCE(profile_flag, ?) WHERE id=?""",
                        (best["email"], best["status"], best["source"], ex.get("country"), flag, l["id"]))
            if ex.get("domain") and not comp["domain"]:
                con.execute("UPDATE companies SET domain=?, domain_source=? WHERE id=?",
                            (norm_domain(ex["domain"]), best["source"], l["company_id"]))
            con.commit()
            stats["found"] += 1
            stats["by_provider"][best["source"]] = stats["by_provider"].get(best["source"], 0) + 1
            stats["statuses"][best["status"]] = stats["statuses"].get(best["status"], 0) + 1
            log(f"  {l['name']} ({l['company']}): {best['email']} [{best['status']}, {best['source']}]")
        if len(exhausted) == len(providers):
            stats["stopped"] = "all email providers exhausted for now"
            break
    stats["exhausted"] = sorted(exhausted)
    return stats


# ---------------- posts ----------------
def posts_via_api(con, show, queries, max_posts=50, posted_after=None):
    items = lip.search_posts(queries, max_posts, posted_after)
    added = 0
    for it in items:
        if not it.get("url"):
            continue
        cur = con.execute("""INSERT OR IGNORE INTO post_candidates(show_id, url, author, title, snippet, status, at,
                             author_headline, author_url, posted_at) VALUES (?,?,?,?,?,'new',?,?,?,?)""",
                          (show["id"], it["url"], it["author"], (it["content"] or "")[:120],
                           (it["content"] or "")[:1500], db.now(), it["author_headline"], it["author_url"],
                           it["posted_at"]))
        added += cur.rowcount
    _count(con, "apify_posts", len(items))
    con.commit()
    return {"posts_returned": len(items), "new_candidates": added}


def balances():
    out = []
    for p in emailp.PROVIDERS:
        row = {"provider": p.name, "configured": p.available(), "free_tier": p.free_note}
        if p.available():
            try:
                row["remaining"] = p.balance()
            except Exception as e:  # noqa: BLE001
                row["remaining"] = f"error: {str(e)[:80]}"
        out.append(row)
    out.append({"provider": "apify", "configured": lip.apify_available(), "free_tier": "$5 platform credit/month"})
    out.append({"provider": "brightdata", "configured": lip.brightdata_available(), "free_tier": "see account"})
    return out


# ---------------- direct LinkedIn people search (Apify) ----------------
FUNCTION_HINTS = [  # infer LinkedIn functions from the persona's own titles/keywords when it has no linkedin_filters
    ("purchasing", ("purchas", "procure", "sourcing", "buyer", "category manager", "vendor", "commodit", "supply chain")),
    ("operations", ("plant head", "operations", "manufacturing head", "works manager", "production head")),
    ("sales", ("sales", "account manager", "key account", "export", "country manager", "commercial")),
    ("business_development", ("business development", "bdm", "partnership")),
    ("marketing", ("marketing", "brand", "event")),
]


def linkedin_filters_for(icp):
    f = dict(icp.get("linkedin_filters") or {})
    if not f.get("functions"):
        text = " ".join(icp.get("titles", []) + icp.get("title_keywords", []) + [icp.get("name", "")]).lower()
        f["functions"] = [fn for fn, hints in FUNCTION_HINTS if any(h in text for h in hints)]
    return f


DEFAULT_SENIORITY = ["senior", "strategic", "manager", "director", "vp", "cxo"]


FOREIGN_SLUG_WORDS = {"africa", "usa", "us", "uk", "europe", "inc", "gmbh", "america", "americas", "china", "japan",
                      "mena", "asia", "australia", "brasil", "brazil", "mexico", "germany", "canada", "llc", "careers",
                      "indonesia", "thailand", "vietnam", "malaysia", "singapore", "philippines", "korea", "italy",
                      "france", "spain", "poland", "czech", "turkey", "saudi", "uae", "dubai", "egypt", "nigeria",
                      "kenya", "argentina", "chile", "colombia", "netherlands", "sweden", "hungary", "romania", "atc"}


def company_page_score(company_name, url, title):
    """Strict match of a LinkedIn company page to our company (no legal-suffix stripping, so
    'Wheels India' != 'Wheels Inc'). Returns 0 when it should not be used."""
    if "linkedin.com/company/" not in (url or ""):
        return 0
    slug = url.split("linkedin.com/company/", 1)[1].split("/")[0].split("?")[0].lower()
    want = _flat(search_name(company_name))
    page = _flat(re.split(r"\s+[|\-–]\s+", title or "")[0])
    sflat = _flat(slug)
    if not want:
        return 0
    full = _flat(re.sub(r"\(.*?\)", "", company_name))
    generic = len(want) < 8  # short brands ("titan", "wheels", "lmw") need the full name to be sure
    if full in (sflat, page) or (len(full) >= 6 and (page.startswith(full) or sflat.startswith(full))):
        score = 12 - min(max(len(page), len(sflat)) - len(full), 30) / 10
    elif want in (sflat, page):
        score = 5 if generic else 10
    elif sflat.startswith(want) or page.startswith(want):
        score = (3 if generic else 6) - min(len(sflat) - len(want), 30) / 10
    else:
        return 0
    extra = set(slug.replace("_", "-").split("-")) - set(re.split(r"[^a-z0-9]+", company_name.lower()))
    if extra & FOREIGN_SLUG_WORDS:
        score -= 6
    # the page address itself must carry the company's distinctive word or initials - a matching page *title*
    # on an unrelated slug ("ernest-lobo-associates" titled "Mahindra & Mahindra") is not the official page
    words = [w for w in re.split(r"[^a-z0-9]+", search_name(company_name).lower()) if w]
    distinct = [w for w in words if len(w) >= 3 and w not in GENERIC_WORDS]
    initials = {"".join(w[0] for w in words if w not in ("and", "the", "of"))}
    initials |= {m.lower() for m in re.findall(r"\(([A-Za-z&]{2,6})\)", company_name)}
    if not (any(w in sflat for w in distinct[:2]) or any(len(i) >= 2 and sflat.startswith(i) for i in initials)):
        return 0
    return max(score, 0)


def resolve_company_linkedin(con, company_row):
    """Find the company's LinkedIn page URL via the search router (TinySearch first). Cached on the company."""
    if company_row["linkedin_url"]:
        return company_row["linkedin_url"]
    results, _ = router.search(con, f'"{search_name(company_row["name"])}" company page', site="linkedin.com/company",
                               num=10)
    best, best_score = None, 0
    for r in results:
        sc = company_page_score(company_row["name"], r.get("url"), r.get("title"))
        if sc > best_score:
            best, best_score = r["url"], sc
    if not best:
        return None
    slug = best.split("linkedin.com/company/", 1)[1].split("/")[0].split("?")[0]
    clean = f"https://www.linkedin.com/company/{slug}"
    con.execute("UPDATE companies SET linkedin_url=? WHERE id=?", (clean, company_row["id"]))
    con.commit()
    return clean


def _title_terms(icp, n):
    """Real job titles first, then short title words. search_keywords are web-search phrases
    ("sales head packaging machinery") that title filters never match."""
    terms = list(icp.get("titles") or []) + list(icp.get("title_keywords") or [])
    if not terms:
        terms = list(icp.get("search_keywords") or [])
    return list(dict.fromkeys(t.strip() for t in terms if t and len(t.strip()) >= 2))[:n]


def linkedin_people(con, industry, icp_code, max_items=150, companies_limit=40, log=print, via="auto",
                    dataset_id=None, seniority=None, only_companies=None):
    """Search LinkedIn people directly (no login) at the companies that pass this ICP's company rule.
    via=apify: harvestapi profile search (LinkedIn function + seniority filters, needs company page URLs).
    via=brightdata: Business Search index (current company name + title words + country), beta access.
    via=aiark: AI Ark people search by company name + title words + location (no company page needed).
    via=deepline: Deepline Native search (free allowance), one request per company by domain or name.
    only_companies: restrict to these company names (e.g. the ones Apify couldn't resolve to a LinkedIn page)."""
    from .people import company_facts, company_rule_ok, load_icps
    icp = next((i for i in load_icps(industry) if i["code"] == icp_code), None)
    if not icp:
        return {"error": f"unknown or disabled ICP {icp_code}"}
    if via == "auto":
        via = "deepline" if dlp.search_available() else "apify" if lip.apify_available() else \
            "aiark" if lip.aiark_search_available() else \
            "brightdata" if lip.brightdata_search_available() else None
    if not via:
        return {"error": "set APIFY_TOKEN (or BRIGHTDATA_API_KEY with Business Search access) in .env"}
    from .people import companies_for_icp, effective_min_shows
    targets, _m = companies_for_icp(con, industry, icp, effective_min_shows(industry))
    if only_companies:
        targets = [c for c in targets if c["name"] in set(only_companies)]
    targets = targets[:companies_limit]
    if not targets:
        return {"error": f"no companies satisfy ICP {icp_code}'s company rule yet"}
    unresolved = []
    try:
        if via == "brightdata":
            terms = _title_terms(icp, 8)
            cc = targeting.region_code(industry) if targeting.get(industry).get("countries") else \
                config.get("LINKEDIN_COUNTRY_CODE", "IN")
            log(f"  Bright Data Business Search: {len(targets)} companies, titles {terms}, country {cc}")
            profiles = lip.bd_people_search([search_name(c["name"]) for c in targets], terms, cc,
                                            limit=min(100, max_items), max_pages=max(1, -(-max_items // 100)))
            companies = targets
            _count(con, "brightdata_business_search")
        elif via == "deepline":
            terms = _title_terms(icp, 40)
            countries = targeting.get(industry).get("countries") or \
                [c.strip() for c in config.get("LINKEDIN_LOCATIONS", "India").split(",") if c.strip()]
            log(f"  Deepline people search (free): {len(targets)} companies, titles {terms[:6]}, "
                f"countries {countries}, max {max_items} people")
            profiles = dlp.people_search([{"name": c["name"], "domain": c["domain"]} for c in targets], terms,
                                         countries, max_items=max_items, log=log)
            companies = targets
            _count(con, "deepline_people_search", len(profiles))
        elif via == "aiark":
            terms = _title_terms(icp, 20)
            locations = targeting.linkedin_locations(industry)
            log(f"  AI Ark people search: {len(targets)} companies, titles {terms[:6]}, locations {locations}, "
                f"max {max_items} people (0.5 credits each)")
            profiles = lip.aiark_people_search([search_name(c["name"]) for c in targets], terms, locations,
                                               max_items=max_items)
            companies = targets
            _count(con, "aiark_people_search")
        else:
            url_to_company = {}
            for c in targets:
                try:
                    u = resolve_company_linkedin(con, c)
                except ProviderBusy as e:
                    log(f"  search busy while resolving LinkedIn pages: {e}")
                    break
                if u:
                    url_to_company[u] = c
                else:
                    unresolved.append(c["name"])
            if not url_to_company:
                return {"error": "could not resolve any LinkedIn company pages", "unresolved": unresolved}
            f = linkedin_filters_for(icp)
            fn_ids = [lip.FUNCTION_IDS[x] for x in f.get("functions", []) if x in lip.FUNCTION_IDS]
            sen_ids = [lip.SENIORITY_IDS[x] for x in (seniority or f.get("seniority", DEFAULT_SENIORITY))
                       if x in lip.SENIORITY_IDS]
            tgt = targeting.get(industry)
            locations = targeting.linkedin_locations(industry)
            headcount = targeting.headcount_codes(tgt.get("headcount"))
            log(f"  one Apify run: {len(url_to_company)} companies, functions {fn_ids}, locations {locations}, "
                f"max {max_items} profiles")
            profiles = lip.people_search(list(url_to_company), fn_ids, sen_ids, locations, max_items=max_items,
                                         dataset_id=dataset_id, exclude_locations=tgt.get("exclude_locations"),
                                         exclude_titles=tgt.get("exclude_titles"), headcount=headcount)
            companies = list(url_to_company.values())
            if not dataset_id:
                _count(con, "apify_people_search_runs")
    except (ProviderBusy, ProviderError) as e:
        return {"error": str(e), "unresolved": unresolved}
    added, unmatched = 0, 0
    for p in profiles:
        if not p.get("name") or not p.get("url"):
            continue
        comp = next((c for c in companies if c["name"] == p.get("for_company")), None) or \
            next((c for c in companies if (p.get("company") and strict_company_match(p["company"], c["name"]))
                     or (not p.get("company") and strict_company_match(p.get("headline"), c["name"]))), None)
        if not comp:
            unmatched += 1
            continue
        pid = db.upsert_person(con, comp["id"], p["name"], title=p.get("title") or p.get("headline"),
                               location=p.get("location"), linkedin_url=p["url"], icp=icp_code,
                               source=f"linkedin_search:{via}")
        if pid:
            con.execute("""UPDATE people SET country=COALESCE(country, ?), li_company=COALESCE(li_company, ?),
                           li_checked_at=COALESCE(li_checked_at, ?) WHERE id=?""",
                        (p.get("country") or "India", p.get("company") or comp["name"], db.now(), pid))
            db.add_evidence(con, "person", pid, "linkedin_profile", "inferred", p["url"],
                            f'LinkedIn search ({via}): {p.get("headline") or p.get("title") or ""} | {p.get("location") or ""}')
            added += 1
    con.commit()
    res = {"icp": icp_code, "via": via, "companies_searched": len(companies), "unresolved_companies": unresolved,
           "profiles_returned": len(profiles), "people_added_or_matched": added, "unmatched_profiles": unmatched}
    if via == "deepline":  # companies its index doesn't cover: the paid sources get a go at these
        found = {p.get("for_company") for p in profiles}
        res["companies_without_results"] = [c["name"] for c in companies if c["name"] not in found]
    return res


def company_posts(con, show, company_ids=None, limit_companies=15, limit_per_company=40, days_before=10,
                  days_after=30, log=print):
    """Bright Data: posts published by exhibitor company pages around a show. Posts that tag people
    ('thank you for visiting our booth') become post_candidates for Claude to classify."""
    from datetime import date
    if not show["start_date"]:
        return {"error": "show has no start_date"}
    start = date.fromisoformat(show["start_date"][:10]) - timedelta(days=days_before)
    end = date.fromisoformat((show["end_date"] or show["start_date"])[:10]) + timedelta(days=days_after)
    # Tested live 2026-11-18: Bright Data only reaches a company's *recent* posts (~last 2-3 months); a window
    # 22 months back returns "No records found". Old shows can't be covered; upcoming shows use recent posts.
    today = date.today()
    if end < today - timedelta(days=100):
        return {"error": f"{show['name']} ended {(today - end).days} days ago - Bright Data only reaches recent company "
                         "posts (about the last 3 months). Use it for recent or upcoming shows; for older shows use "
                         "`posts-api` (LinkedIn post search) instead."}
    upcoming = start > today
    rows = con.execute("""SELECT DISTINCT c.* FROM companies c JOIN evidence e ON e.entity='company' AND e.entity_id=c.id
                          WHERE e.show_id=? AND e.kind='exhibitor'""", (show["id"],)).fetchall()
    if company_ids:
        rows = [r for r in rows if r["id"] in company_ids]
    urls = {}
    for c in rows[:limit_companies]:
        try:
            u = resolve_company_linkedin(con, c)
        except ProviderBusy:
            break
        if u:
            urls[u] = c
    if not urls:
        return {"error": "no exhibitor LinkedIn pages resolved"}
    log(f"  Bright Data post discovery: {len(urls)} company pages, " + ("recent posts (show is upcoming)" if upcoming
                                                                          else f"{start} .. {end}"))
    try:
        posts = lip.bd_company_posts(list(urls), None if upcoming else start.isoformat(),
                                     None if upcoming else end.isoformat(), limit_per_company)
    except (ProviderBusy, ProviderError) as e:
        return {"error": str(e)}
    _count(con, "brightdata_posts", len(posts))
    added, with_tags = 0, 0
    for p in posts:
        tags = p.get("tagged_people") or []
        with_tags += bool(tags)
        tag_txt = "; ".join(f'{t.get("name") or t.get("title") or ""} <{t.get("link") or t.get("url") or ""}>'
                            if isinstance(t, dict) else str(t) for t in tags)
        snippet = (p.get("text") or "")[:1200] + (f"\nTAGGED PEOPLE: {tag_txt}" if tag_txt else "")
        cur = con.execute("""INSERT OR IGNORE INTO post_candidates(show_id, url, author, title, snippet, status, at,
                             author_headline, author_url, posted_at) VALUES (?,?,?,?,?,'new',?,?,?,?)""",
                          (show["id"], p.get("url"), p.get("author"), "company post" + (" (tags people)" if tags else ""),
                           snippet, db.now(), p.get("author_headline"), p.get("author_url"), p.get("posted_at")))
        added += cur.rowcount
    con.commit()
    return {"companies": len(urls), "posts": len(posts), "posts_tagging_people": with_tags, "new_candidates": added}
