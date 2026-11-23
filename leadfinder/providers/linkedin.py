"""LinkedIn data via third-party APIs (no LinkedIn login), written against docs read on 2026-11-18.

Profiles (check location + current company before spending email credits):
  Apify  harvestapi~linkedin-profile-scraper  ($5/month free platform credit; ~$4 per 1k profiles;
         free-tier users capped at 200 profiles total for this actor)
  Bright Data dataset gd_l1viktl72bvl7bjuj0 (sync /datasets/v3/scrape, one URL per request).
         Note: since 2025-11-13 LinkedIn hides position/experience from these scrapers; current_company still works.
Posts (people posting about a show):
  Apify  harvestapi~linkedin-post-search  (~$2 per 1k posts)
"""
import requests

from .. import config, db, keys
from .search import ProviderBusy, ProviderError

APIFY = "https://api.apify.com/v2/actors/{actor}/run-sync-get-dataset-items"


def _slug(url):
    if not url or "linkedin.com/in/" not in url:
        return None
    return url.split("linkedin.com/in/", 1)[1].split("?")[0].split("/")[0].lower()


def apify_available():
    return keys.available("apify")


def brightdata_available():
    return keys.available("brightdata")


# Business Search (/search/people) is early access: without it every call is a 403. Remember that per key for a day
# (so runs don't keep asking), and show it on the credits page. Profile scraping and post discovery are unaffected.
BD_SEARCH_DENIED_HOURS = 24
BD_SEARCH_HOWTO = "email sales@brightdata.com to enable Business Search"


def _bd_denied_setting(key):
    return f"bd_search_denied:{key.state_id}"


def bd_search_denied(key):
    from datetime import datetime, timedelta, timezone
    at = db.get_setting(keys._con(), _bd_denied_setting(key))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=BD_SEARCH_DENIED_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return bool(at) and at > cutoff


def brightdata_search_available():
    return any(not bd_search_denied(k) for k in keys.usable("brightdata"))


LAST_APIFY_KEY = {}  # actor -> Key that ran it last (so the caller can count the run against that account)


PEOPLE_SEARCH_ACTOR = "harvestapi~linkedin-profile-search"
# There is NO run cap on this actor for free plans (tested 2026-11-23: an 11th run on a FREE account succeeded).
# It is pay-per-event: one search page (up to 25 profiles, "Short" mode) costs ~$0.10 of platform credit.
APIFY_SEARCH_PAGE_USD = 0.10
APIFY_MIN_CREDIT_USD = 0.25


def apify_actor_runs(key, actor=PEOPLE_SEARCH_ACTOR):
    """Runs of an actor made by this token's account, read from Apify (free call). None if unreadable."""
    try:
        r = requests.get(f"https://api.apify.com/v2/acts/{actor}/runs", params={"limit": 1}, timeout=20,
                         headers={"Authorization": f"Bearer {key.value}"})
        return (r.json().get("data") or {}).get("total") if r.ok else None
    except (requests.RequestException, ValueError):
        return None


def apify_credit_left(key):
    """USD of platform credit left this month on the token's account (free call). None if unreadable."""
    try:
        r = requests.get("https://api.apify.com/v2/users/me/limits", timeout=20,
                         headers={"Authorization": f"Bearer {key.value}"})
        d = (r.json().get("data") or {}) if r.ok else {}
        cap = d["limits"]["maxMonthlyUsageUsd"]
        # on the FREE plan the usage limit ($10) is above the free credit ($5): spend only the credit
        me = requests.get("https://api.apify.com/v2/users/me", timeout=20, headers={"Authorization": f"Bearer {key.value}"})
        plan = ((me.json().get("data") or {}).get("plan") or {}) if me.ok else {}
        if plan.get("id") == "FREE" and plan.get("monthlyUsageCreditsUsd") is not None:
            cap = min(cap, plan["monthlyUsageCreditsUsd"])
        return cap - d["current"]["monthlyUsageUsd"]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


def apify_has_credit(key):
    # unreadable -> try anyway; a 402 from Apify rotates to the next token
    left = apify_credit_left(key)
    return left is None or left >= APIFY_MIN_CREDIT_USD


def finished_run_dataset(since_iso, actor=PEOPLE_SEARCH_ACTOR):
    """Dataset of a run of this actor that started at/after since_iso and SUCCEEDED, on any of the tokens (free)."""
    for key in keys.all_keys("apify"):
        try:
            r = requests.get(f"https://api.apify.com/v2/acts/{actor}/runs", params={"desc": 1, "limit": 5},
                             timeout=20, headers={"Authorization": f"Bearer {key.value}"})
            items = (r.json().get("data") or {}).get("items") or [] if r.ok else []
        except (requests.RequestException, ValueError):
            continue
        for it in items:
            if (it.get("startedAt") or "")[:19] >= since_iso[:19] and it.get("status") == "SUCCEEDED":
                return it.get("defaultDatasetId")
    return None


def _apify(actor, body, timeout=310, only=None):
    """Synchronous run (no polling), so a busy token simply moves the run to the next token."""
    def call(key):
        r = requests.post(APIFY.format(actor=actor), json=body, timeout=timeout,
                          headers={"Authorization": f"Bearer {key.value}"})
        if r.status_code in (402, 403, 429):
            raise ProviderBusy(f"apify HTTP {r.status_code}: {r.text[:150]}")
        if r.status_code == 408:
            raise ProviderError("apify run exceeded 300 s sync limit - use a smaller batch")
        if not r.ok:
            raise ProviderError(f"apify HTTP {r.status_code}: {r.text[:200]}")
        LAST_APIFY_KEY[actor] = key
        keys.count(key, extra_bucket="runs:" + actor)
        return r.json()
    return keys.run("apify", call, only=only)


def profiles_apify(urls):
    items = _apify("harvestapi~linkedin-profile-scraper",
                   {"profileScraperMode": "Profile details no email ($4 per 1k)", "queries": urls})
    out = {}
    for it in items or []:
        loc = (it.get("location") or {}).get("parsed") or {}
        cur = (it.get("currentPosition") or [{}])
        cur = cur[0] if cur else {}
        slug = (it.get("publicIdentifier") or _slug(it.get("linkedinUrl")) or "").lower()
        out[slug] = {"name": " ".join(x for x in (it.get("firstName"), it.get("lastName")) if x),
                     "headline": it.get("headline"), "company": cur.get("companyName"),
                     "title": cur.get("position") or cur.get("title"),
                     "country": loc.get("country"), "country_code": loc.get("countryCode"), "city": loc.get("city"),
                     "url": it.get("linkedinUrl"), "source": "apify"}
    return out


def profile_brightdata(url):
    return keys.run("brightdata", lambda k: _profile_brightdata(url, k))


def _profile_brightdata(url, key):
    r = requests.post("https://api.brightdata.com/datasets/v3/scrape", timeout=90,
                      params={"dataset_id": "gd_l1viktl72bvl7bjuj0", "format": "json"},
                      headers=_bd_headers(key), json=[{"url": url}])
    if r.status_code in (402, 403, 429):
        raise ProviderBusy(f"brightdata HTTP {r.status_code}: {r.text[:150]}")
    if r.status_code == 202:
        raise ProviderError("brightdata: still collecting after 1 min (async snapshot not handled)")
    if not r.ok:
        raise ProviderError(f"brightdata HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    it = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
    cur = it.get("current_company") or {}
    return {"name": it.get("name"), "headline": it.get("position"),
            "company": cur.get("name") or it.get("current_company_name"), "title": cur.get("title"),
            "country": None, "country_code": it.get("country_code"), "city": it.get("city"),
            "url": it.get("url"), "source": "brightdata"}


def check_profiles(urls, batch=25, on_batch=None):
    """Return {slug: profile} using Apify (batched) or Bright Data (one by one)."""
    out = {}
    if apify_available():
        for i in range(0, len(urls), batch):
            got = profiles_apify(urls[i:i + batch])
            out.update(got)
            if on_batch:
                on_batch(got)  # saved per batch, so a Stop never throws away profiles already paid for
        return out
    if brightdata_available():
        for u in urls:
            p = profile_brightdata(u)
            out[_slug(u)] = p
            if on_batch:
                on_batch({_slug(u): p})
        return out
    raise ProviderBusy("no LinkedIn profile provider configured (APIFY_TOKEN or BRIGHTDATA_API_KEY)")


def search_posts(queries, max_posts=50, posted_after=None):
    if not apify_available():
        raise ProviderBusy("APIFY_TOKEN not set")
    body = {"searchQueries": queries, "maxPosts": max_posts, "profileScraperMode": "short",
            "scrapeReactions": False, "scrapeComments": False}
    if posted_after:
        body["postedLimitDate"] = posted_after
    items = _apify("harvestapi~linkedin-post-search", body)
    return [{"author": (it.get("author") or {}).get("name"),
             "author_headline": (it.get("author") or {}).get("info"),
             "author_url": (it.get("author") or {}).get("linkedinUrl"),
             "content": it.get("content"), "url": it.get("linkedinUrl"),
             "posted_at": (it.get("postedAt") or {}).get("date")} for it in items or []]


# LinkedIn people search (harvestapi~linkedin-profile-search, input names from the actor's OpenAPI spec, 2026-11-18).
# Short mode: $0.10 per search page of 25 profiles. Free Apify plans: 10 runs of this actor in total -> batch.
FUNCTION_IDS = {"purchasing": "21", "sales": "25", "business_development": "4", "operations": "18", "marketing": "15"}
# companyHeadcount codes from the actor's input schema (read 2026-11-19)
HEADCOUNT_CODES = {"1-10": "B", "11-50": "C", "51-200": "D", "201-500": "E", "501-1000": "F", "1001-5000": "G",
                   "5001-10000": "H", "10001+": "I"}
SENIORITY_IDS = {"senior": "120", "strategic": "130", "manager": "210", "director": "220", "vp": "300", "cxo": "310",
                 "owner": "320"}


def dataset_items(dataset_id):
    """Re-read a finished Apify run's results (free) - e.g. to re-ingest after a parser fix. The dataset belongs to
    whichever account ran it, so try each token until one can read it."""
    last = None
    for key in keys.all_keys("apify"):
        r = requests.get(f"https://api.apify.com/v2/datasets/{dataset_id}/items", params={"clean": "true"},
                         timeout=120, headers={"Authorization": f"Bearer {key.value}"})
        if r.ok:
            return r.json()
        last = f"apify dataset HTTP {r.status_code}: {r.text[:150]}"
    raise ProviderError(last or "APIFY_TOKEN not set")


def people_search(company_urls, function_ids=None, seniority_ids=None, locations=None, job_titles=None,
                  max_items=100, dataset_id=None, exclude_locations=None, exclude_titles=None, headcount=None):
    """NOTE: free Apify plans return at most 25 profiles per run; each search page costs ~$0.10 of credit."""
    if not apify_available():
        raise ProviderBusy("APIFY_TOKEN not set")
    body = {"profileScraperMode": "Short", "currentCompanies": company_urls, "maxItems": max_items,
            "takePages": max(1, -(-max_items // 25))}
    if function_ids:
        body["functionIds"] = function_ids
    if seniority_ids:
        body["seniorityLevelIds"] = seniority_ids
    if locations:
        body["locations"] = locations
    if job_titles:
        body["currentJobTitles"] = job_titles
    if exclude_locations:
        body["excludeLocations"] = exclude_locations
    if exclude_titles:
        body["excludeCurrentJobTitles"] = exclude_titles
    if headcount:
        body["companyHeadcount"] = headcount
    # only tokens whose account still has platform credit
    items = dataset_items(dataset_id) if dataset_id else _apify(PEOPLE_SEARCH_ACTOR, body, only=apify_has_credit)
    out = []
    for it in items or []:
        loc = it.get("location")
        loc_text = loc.get("linkedinText") if isinstance(loc, dict) else loc
        parsed = (loc.get("parsed") or {}) if isinstance(loc, dict) else {}
        # Short mode returns `currentPositions` [{companyName, title, current}]; Full mode `currentPosition`
        cur = it.get("currentPositions") or it.get("currentPosition") or []
        cur = next((c for c in cur if c.get("current", True)), cur[0]) if cur else {}
        name = it.get("name") or " ".join(x for x in (it.get("firstName"), it.get("lastName")) if x)
        out.append({"name": name, "headline": it.get("headline"), "url": it.get("linkedinUrl"),
                    "company": cur.get("companyName"), "title": cur.get("position") or cur.get("title"),
                    "location": loc_text, "country": parsed.get("country")})
    return out


# ---------------- Bright Data: Business Search (people index) + company-post discovery ----------------
# Docs read 2026-11-18. Business Search is in beta: the account must be on its allowlist (business-email signup);
# 403 = not allowlisted. Index: 700M+ people/companies, refreshed monthly.
BD = "https://api.brightdata.com"


def _bd_headers(key):
    return {"Authorization": f"Bearer {key.value}", "Content-Type": "application/json"}


def bd_people_search(company_names, title_terms, country_code="IN", limit=100, max_pages=3):
    """POST /search/people, mode=ludicrous (structured): current company IN names AND title matches any term AND country."""
    if not brightdata_available():
        raise ProviderBusy("BRIGHTDATA_API_KEY not set")
    if not brightdata_search_available():
        raise ProviderBusy(f"Bright Data people search: not enabled ({BD_SEARCH_HOWTO})")
    return keys.run("brightdata", lambda k: _bd_people_search(k, company_names, title_terms, country_code, limit,
                                                              max_pages), only=lambda k: not bd_search_denied(k))


def _bd_people_search(key, company_names, title_terms, country_code, limit, max_pages):
    query = {"and": [
        {"or": [{"text": {"current_company_name": {"value": n, "mode": "all"}}} for n in company_names]},
        {"or": [{"text": {"current_title": t}} for t in title_terms]},
    ]}
    if country_code:
        query["and"].append({"equals": {"country_code": country_code}})
    out = []
    for page in range(max_pages):
        body = {"source": "linkedin_people", "mode": "ludicrous", "query": query, "offset": page * limit,
                "limit": limit, "view": "summary"}
        r = requests.post(f"{BD}/search/people", headers=_bd_headers(key), json=body, timeout=90)
        if r.status_code == 403:
            # not "busy": resting the key would also stop profile checks and post discovery, which do work
            db.set_setting(keys._con(), _bd_denied_setting(key), db.now())
            raise ProviderError(f"Bright Data people search: not enabled ({BD_SEARCH_HOWTO})")
        if r.status_code in (402, 429):
            raise ProviderBusy(f"brightdata HTTP {r.status_code}")
        if not r.ok:
            raise ProviderError(f"brightdata search HTTP {r.status_code}: {r.text[:200]}")
        if page == 0 and bd_search_denied(key):
            db.set_setting(keys._con(), _bd_denied_setting(key), "")  # access was granted since
        docs = r.json().get("documents") or []
        for d in docs:
            pid = d.get("linkedin_person_id") or d.get("linkedin_id") or d.get("id")
            url = d.get("url") or d.get("linkedin_url") or (f"https://www.linkedin.com/in/{pid}" if pid else None)
            out.append({"name": d.get("name") or " ".join(x for x in (d.get("first_name"), d.get("last_name")) if x),
                        "headline": d.get("headline") or d.get("position"), "url": url,
                        "company": d.get("current_company_name"), "title": d.get("current_title"),
                        "location": d.get("location") or d.get("city"), "country": d.get("country_code")})
        if len(docs) < limit:
            break
    return out


POSTS_DATASET = "gd_lyy3tktm25m4avu764"


def bd_company_posts(company_urls, start_date=None, end_date=None, limit_per_input=50, wait_seconds=900):
    """Discover posts published by company pages in a date window (async trigger -> progress -> snapshot).
    Each post carries tagged_people / tagged_companies: exhibitors thanking booth visitors tag them."""
    if not brightdata_available():
        raise ProviderBusy("BRIGHTDATA_API_KEY not set")
    # the whole trigger -> progress -> snapshot flow stays on one key: a snapshot is only visible to its own account
    return keys.run("brightdata", lambda k: _bd_company_posts(k, company_urls, start_date, end_date, limit_per_input,
                                                              wait_seconds))


def _bd_company_posts(key, company_urls, start_date, end_date, limit_per_input, wait_seconds):
    import time
    inputs = []
    for u in company_urls:
        row = {"url": u}
        if start_date:
            row["start_date"] = start_date
        if end_date:
            row["end_date"] = end_date
        inputs.append(row)
    r = requests.post(f"{BD}/datasets/v3/trigger", headers=_bd_headers(key), timeout=60,
                      params={"dataset_id": POSTS_DATASET, "include_errors": "true", "type": "discover_new",
                              "discover_by": "company_url"},
                      json={"input": inputs, "limit_per_input": limit_per_input})
    if r.status_code in (402, 403, 429):
        raise ProviderBusy(f"brightdata trigger HTTP {r.status_code}: {r.text[:150]}")
    if not r.ok:
        raise ProviderError(f"brightdata trigger HTTP {r.status_code}: {r.text[:200]}")
    sid = r.json().get("snapshot_id")
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(10)
        st = requests.get(f"{BD}/datasets/v3/progress/{sid}", headers=_bd_headers(key), timeout=60).json().get("status")
        if st == "ready":
            break
        if st in ("failed", "canceled"):
            raise ProviderError(f"brightdata snapshot {sid} {st}")
    else:
        raise ProviderError(f"brightdata snapshot {sid} not ready after {wait_seconds}s - re-run later")
    data = requests.get(f"{BD}/datasets/v3/snapshot/{sid}", params={"format": "json"}, headers=_bd_headers(key),
                        timeout=120).json()
    posts = []
    for p in data if isinstance(data, list) else []:
        if p.get("error"):
            continue
        posts.append({"url": p.get("url"), "author": p.get("user_name") or p.get("user_id"),
                      "author_url": p.get("use_url"), "author_headline": p.get("user_title"),
                      "account_type": p.get("account_type"), "text": p.get("post_text") or p.get("original_post_text"),
                      "posted_at": p.get("date_posted"), "tagged_people": p.get("tagged_people") or [],
                      "tagged_companies": p.get("tagged_companies") or []})
    return posts


# ---------------- AI Ark: people search by company NAME (no LinkedIn company page needed) ----------------
# Docs read 2026-11-23 (docs.ai-ark.com). POST /v1/people, header X-TOKEN. 0.5 credits per returned person;
# balance read is free. Free trial: 100 credits; the API key reported 4 requests/minute (x-ratelimit-limit-minute).
AIARK = "https://api.ai-ark.com/api/developer-portal/v1"
AIARK_CREDITS_PER_RESULT = 0.5
AIARK_PAGE_GAP_SECONDS = 16  # 4 requests a minute


def _aiark_headers(key):
    return {"X-TOKEN": key.value, "Content-Type": "application/json"}


_AIARK_BALANCE = {}  # key state_id -> (read at, credits): the 4/minute limit counts balance reads too


def aiark_credits(key, max_age=300):
    """Credits left on the key's account (free call, cached). None if unreadable."""
    import time
    hit = _AIARK_BALANCE.get(key.state_id)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    try:
        r = requests.get(f"{AIARK}/payments/credits", headers=_aiark_headers(key), timeout=20)
        val = float(r.json()["total"]) if r.ok else None
    except (requests.RequestException, ValueError, KeyError, TypeError):
        val = None
    if val is not None:
        _AIARK_BALANCE[key.state_id] = (time.time(), val)
    return val


def _aiark_spent(key, n):
    import time
    hit = _AIARK_BALANCE.get(key.state_id)
    if hit:
        _AIARK_BALANCE[key.state_id] = (time.time(), max(0.0, hit[1] - n * AIARK_CREDITS_PER_RESULT))


def aiark_has_credit(key):
    left = aiark_credits(key)
    return left is None or left >= AIARK_CREDITS_PER_RESULT * 5


def aiark_search_available():
    return any(aiark_has_credit(k) for k in keys.usable("ai_ark"))


def aiark_people_search(company_names, title_terms, locations, max_items=50):
    """Current company name (SMART match) AND current title matches any term AND location. Returns the same profile
    dicts as people_search(). Spends 0.5 credits per person returned, never more than the key has."""
    if not aiark_search_available():
        raise ProviderBusy("AI Ark: no key with credits left")
    return keys.run("ai_ark", lambda k: _aiark_people_search(k, company_names, title_terms, locations, max_items),
                    only=aiark_has_credit)


def _aiark_people_search(key, company_names, title_terms, locations, max_items):
    import time
    contact = {}
    if title_terms:
        contact["experience"] = {"current": {"title": {"any": {"include": {"mode": "SMART",
                                                                           "content": list(title_terms)[:300]}}}}}
    if locations:
        contact["location"] = {"any": {"include": list(locations)[:300]}}
    body = {"account": {"name": {"any": {"include": {"mode": "SMART", "content": list(company_names)[:300]}}}},
            "contact": contact}
    left = aiark_credits(key)
    if left is not None:
        max_items = min(max_items, int(left / AIARK_CREDITS_PER_RESULT))
    out, page = [], 0
    while len(out) < max_items:
        size = min(100, max_items - len(out))
        if page:
            time.sleep(AIARK_PAGE_GAP_SECONDS)
        r = requests.post(f"{AIARK}/people", headers=_aiark_headers(key), json={**body, "page": page, "size": size},
                          timeout=90)
        if r.status_code == 404:  # "data not found"
            break
        if r.status_code in (401, 402, 403, 429):
            raise ProviderBusy(f"ai_ark HTTP {r.status_code}: {r.text[:150]}")
        if not r.ok:
            raise ProviderError(f"ai_ark HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
        for it in d.get("content") or []:
            prof, loc = it.get("profile") or {}, it.get("location") or {}
            out.append({"name": prof.get("full_name") or " ".join(x for x in (prof.get("first_name"),
                                                                                 prof.get("last_name")) if x),
                        "headline": prof.get("headline"), "url": (it.get("link") or {}).get("linkedin"),
                        "company": ((it.get("company") or {}).get("summary") or {}).get("name"),
                        "title": prof.get("title"), "location": loc.get("default"), "country": loc.get("country")})
        keys.count(key, n=len(d.get("content") or []))
        _aiark_spent(key, len(d.get("content") or []))
        if d.get("last", True) or not d.get("content"):
            break
        page += 1
    return out
