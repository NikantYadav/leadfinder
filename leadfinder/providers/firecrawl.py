"""Firecrawl (API v2, docs read 2026-11-24) - used where it beats the free tools, kept inside the free plan.

Free plan: 1,000 credits per month, no rollover, no pay-as-you-go; 10 requests/min each for /scrape and /map,
2 at a time. Costs measured live on 2026-11-24 (balance read before/after each call):
  /map                          1 credit per call, however many links come back   <- the cheapest win
  /scrape                       1 credit per page, scroll/wait actions included; error pages (404) cost 1 too
  /scrape of a PDF              1 credit PER PDF PAGE, unless parsers=[] -> the raw file (base64) for a flat 1
Used for (best value first):
  1. map_site(show site, "exhibitor") -> the exhibitor list / directory / catalogue links on a show's site
     (pipeline evidence step, `./lf site-map`). One credit found ADIPEC's Swapcard directory and ProPak's PDF list.
  2. map_site(company site, "exhibitions events") -> a company's own events / "meet us at" pages (evidence part 2).
  3. scrape(url, actions=scroll) -> exhibitor lists that only load in a browser (read-list, before asking the user).
  4. last fallback in fetch.py (after direct, TinyFetch, OpenSERP); PDFs fetched raw for 1 credit.
Not used, on purpose: /search (2 credits per 10 results; TinySearch is free, 12k/day), the json format (+4 credits
a page; Claude reads the text for free), /extract and /crawl (1 credit per page crawled, burns the month).
Always proxy "basic": never the "enhanced" proxy (an anti-bot proxy; this tool does not evade bot checks).

Budget guard (works across processes, because headless Claude spends through `./lf` subprocesses):
  - automatic use (env LF_FIRECRAWL_AUTO=1, set by the pipeline for itself and its Claude steps) stops when the live
    balance would fall below FIRECRAWL_RESERVE (default 150), and at the run cap LF_FIRECRAWL_CAP counted in
    credit_usage under LF_FIRECRAWL_BUCKET;
  - manual use (the user's own read-list / fetch / site-map commands) may use the reserve, down to 0.
"""
import base64
import os
import re
import time
from urllib.parse import urlparse

import requests

from .. import config, db, keys
from .search import ProviderBusy

BASE = "https://api.firecrawl.dev/v2"
MIN_GAP = float(config.get("FIRECRAWL_MIN_GAP", "6.5"))     # 10 requests/min per key on the free plan
RESERVE = int(config.get("FIRECRAWL_RESERVE", "150"))       # automatic steps leave this much for the user
BALANCE_MAX_AGE = 300                                       # seconds a cached live balance is trusted
COST = {"map": 1, "scrape": 1}


class OutOfBudget(RuntimeError):
    """The Firecrawl budget (reserve or run cap) does not allow this call. Not a key problem: no key rotation."""


def available():
    return keys.available("firecrawl")


def _con(con):
    return con if con is not None else keys._con()


def is_auto():
    return os.environ.get("LF_FIRECRAWL_AUTO") == "1"


# ---------- budget ----------
def _state(con, name):
    row = _con(con).execute("SELECT cooldown_until, last_used FROM provider_state WHERE provider=?", (name,)).fetchone()
    return row


def _put_state(con, name, value=None, at=None):
    c = _con(con)
    c.execute("""INSERT INTO provider_state(provider, cooldown_until, last_used) VALUES (?,?,?)
                 ON CONFLICT(provider) DO UPDATE SET cooldown_until=excluded.cooldown_until, last_used=excluded.last_used""",
              (name, value, at or db.now()))
    c.commit()


def _read_live(key):
    from .. import credits
    d = credits._live("firecrawl", key)
    r = d.get("remaining")
    return int(r) if isinstance(r, (int, float)) else None


def balance(con=None, max_age=BALANCE_MAX_AGE, read_live=None):
    """Credits left over all keys (live, cached in provider_state for max_age seconds; local spends since the read
    are subtracted). None when no balance could be read."""
    read_live = read_live or _read_live
    row = _state(con, "firecrawl_balance")
    try:
        fresh = bool(row) and time.time() - float(row["last_used"]) < max_age   # last_used = epoch of the live read
    except (TypeError, ValueError):
        fresh = False
    if fresh and row["cooldown_until"] not in (None, ""):
        return int(row["cooldown_until"])
    total, got = 0, False
    for k in keys.all_keys("firecrawl"):
        n = read_live(k)
        if n is not None:
            total, got = total + n, True
    if not got:
        return None
    _put_state(con, "firecrawl_balance", str(total), f"{time.time():.0f}")
    return total


def _note_spend(con, n):
    """Keep the cached balance honest between live reads, and count the run bucket."""
    row = _state(con, "firecrawl_balance")
    if row and row["cooldown_until"] not in (None, ""):
        _put_state(con, "firecrawl_balance", str(max(0, int(row["cooldown_until"]) - n)), row["last_used"])
    bucket = os.environ.get("LF_FIRECRAWL_BUCKET")
    if bucket:
        c = _con(con)
        c.execute("""INSERT INTO credit_usage(provider, month, used) VALUES (?, 'run', ?)
                     ON CONFLICT(provider, month) DO UPDATE SET used=used+?""", (bucket, n, n))
        c.commit()


def run_spent(con=None, bucket=None):
    bucket = bucket or os.environ.get("LF_FIRECRAWL_BUCKET")
    if not bucket:
        return 0
    row = _con(con).execute("SELECT used FROM credit_usage WHERE provider=? AND month='run'", (bucket,)).fetchone()
    return row["used"] if row else 0


def check_budget(cost=1, con=None, auto=None, read_live=None):
    """Raise OutOfBudget when this call is not allowed. Returns the balance it judged by (None = unknown)."""
    auto = is_auto() if auto is None else auto
    if auto:
        cap = os.environ.get("LF_FIRECRAWL_CAP")
        if cap is not None and run_spent(con) + cost > int(cap):
            raise OutOfBudget(f"firecrawl: this run's budget of {cap} credits is used up")
    left = balance(con, read_live=read_live)
    floor = RESERVE if auto else 0
    if left is not None and left - cost < floor:
        raise OutOfBudget(f"firecrawl: {left} credits left this month"
                          + (f", kept for your own use (reserve {RESERVE})" if auto and left > 0 else ""))
    return left


def can_spend(cost=1, con=None, auto=None):
    try:
        check_budget(cost, con, auto)
        return True
    except OutOfBudget:
        return False


# ---------- calls ----------
def _pace(con, key):
    """At most one call per MIN_GAP seconds per key, across processes (provider_state)."""
    name = "firecrawl_pace:" + key.state_id
    row = _state(con, name)
    if row and row["cooldown_until"]:
        wait = float(row["cooldown_until"]) + MIN_GAP - time.time()
        if wait > 0:
            time.sleep(min(wait, MIN_GAP))
    _put_state(con, name, f"{time.time():.3f}")


def _post(path, body, cost, con=None, auto=None, timeout=180):
    if not available():
        raise RuntimeError("firecrawl: FIRECRAWL_API_KEY not set")
    check_budget(cost, con, auto)
    body = {**body}
    if path == "/scrape":
        body["proxy"] = "basic"   # never "auto"/"enhanced": no anti-bot proxies

    def call(key):
        _pace(con, key)
        r = requests.post(BASE + path, json=body, timeout=timeout, headers={"Authorization": f"Bearer {key.value}"})
        if r.status_code in (402, 429):
            raise ProviderBusy(f"firecrawl HTTP {r.status_code}" + (" (rate limited)" if r.status_code == 429 else
                                                                    " (out of credits)"))
        if not r.ok:
            raise RuntimeError(f"firecrawl: HTTP {r.status_code} {r.text[:150]}")
        d = r.json()
        data = d.get("data") if isinstance(d.get("data"), dict) else {}
        used = int((data.get("metadata") or {}).get("creditsUsed") or d.get("creditsUsed") or cost)
        keys.count(key, used, _con(con))
        _note_spend(con, used)
        return d
    return keys.run("firecrawl", call, con)


def site_root(url):
    """'https://expogage.com/events/petrotech' -> 'https://expogage.com' (map on a sub-path finds ~nothing)."""
    u = url if "://" in url else "https://" + url
    p = urlparse(u)
    return f"{p.scheme or 'https'}://{p.netloc}"


MAP_CACHE_DAYS = 30


def map_site(url, search=None, limit=300, con=None, auto=None, root=True, refresh=False):
    """-> [{url, title, description}] (title/description may be missing). 1 credit; answers are cached for
    MAP_CACHE_DAYS in search_cache, so re-runs and repeated `./lf site-map` calls are free."""
    import json
    body = {"url": site_root(url) if root else url, "limit": limit, "includeSubdomains": True}
    if search:
        body["search"] = search
    c = _con(con)
    q, params = "map:" + body["url"].lower(), json.dumps({"search": search or "", "limit": limit})
    if not refresh:
        row = c.execute("SELECT response, at FROM search_cache WHERE provider='firecrawl_map' AND query=? AND params=?",
                        (q, params)).fetchone()
        if row and row["at"] >= time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - MAP_CACHE_DAYS * 86400)):
            return json.loads(row["response"])
    d = _post("/map", body, COST["map"], con, auto, timeout=120)
    links = [l if isinstance(l, dict) else {"url": l} for l in (d.get("links") or [])]
    c.execute("INSERT OR REPLACE INTO search_cache VALUES (?,?,?,?,?)", ("firecrawl_map", q, params, json.dumps(links), db.now()))
    c.commit()
    return links


def scroll_actions(n=6, wait_ms=1200):
    return [{"type": "wait", "milliseconds": 2500}] + \
        [a for _ in range(n) for a in ({"type": "scroll", "direction": "down"}, {"type": "wait", "milliseconds": wait_ms})]


def scrape(url, actions=None, links=False, only_main=False, wait_for=3000, con=None, auto=None):
    """-> {markdown, links, status}. 1 credit (actions included)."""
    body = {"url": url, "formats": ["markdown"] + (["links"] if links else []), "onlyMainContent": only_main,
            "timeout": 120000}
    if actions:
        body["actions"] = actions
    elif wait_for:
        body["waitFor"] = wait_for
    d = (_post("/scrape", body, COST["scrape"], con, auto, timeout=240).get("data") or {})
    return {"markdown": d.get("markdown") or "", "links": d.get("links") or [],
            "status": (d.get("metadata") or {}).get("statusCode")}


def scrape_pdf(url, con=None, auto=None):
    """The raw PDF for a flat 1 credit (parsers=[]); the caller turns it into text with pdfplumber. -> bytes."""
    d = (_post("/scrape", {"url": url, "formats": ["markdown"], "parsers": []}, COST["scrape"], con, auto,
               timeout=240).get("data") or {})
    status = (d.get("metadata") or {}).get("statusCode")
    raw = d.get("markdown") or d.get("rawBase64") or ""
    if status and status >= 400:
        raise RuntimeError(f"firecrawl: the PDF URL answered HTTP {status}")
    return decode_pdf(raw)


def decode_pdf(raw):
    if isinstance(raw, bytes):
        data = raw
    else:
        s = raw.strip()
        if s.startswith("%PDF"):
            return s.encode("latin-1", "ignore")
        s = re.sub(r"^data:[^,]*,", "", s)
        try:
            data = base64.b64decode(s + "=" * (-len(s) % 4), validate=False)
        except ValueError as e:
            raise RuntimeError(f"firecrawl: PDF answer is not base64 ({e})")
    if not data.startswith(b"%PDF"):
        raise RuntimeError("firecrawl: the answer is not a PDF file")
    return data


# ---------- exhibitor-list links on a show's site ----------
LIST_WORDS = ("exhibitor-list", "exhibitorlist", "exhibitor list", "exhibitors list", "list of exhibitors",
              "exhibitor directory", "exhibitor-directory", "exhibitors directory", "directory", "catalogue", "catalog",
              "fair guide", "show guide", "show-daily", "show daily", "aussteller", "exposant", "who's exhibiting",
              "whos-exhibiting", "exhibitor search", "exhibitor-search", "exhibitors")
BAD_WORDS = ("book-a-stand", "book a stand", "bookyourstand", "why-exhibit", "why exhibit", "manual", "survey", "login",
             "register", "registration", "sponsor", "rules", "regulation", "become", "apply", "services", "faq",
             "badge", "visa", "hotel", "travel", "contact", "terms", "privacy", "brochure", "floor-plan", "floorplan",
             "testimonial", "news", "blog", "press", "media-kit", "careers", "/form/")
DIRECTORY_HOSTS = ("swapcard", "mapyourshow", "a2zinfo", "expofp", "vis.messe", "exhibitor.", "exhibitors.",
                   "catalog.", "catalogue.", "directory.")
YEAR = re.compile(r"(?<!\d)(20[1-3]\d)(?!\d)")


def show_year(show):
    for v in (show.get("year"), str(show.get("start_date") or "")[:4]):
        if v and str(v).isdigit():
            return int(v)
    m = YEAR.search(str(show.get("name") or ""))
    return int(m.group(1)) if m else None


def current_edition(show, today=None):
    """A show running roughly now (started < 60 days ago or starts within 12 months): the site's undated list pages
    may be its list. For an older edition they are a newer edition's list - not proof for this show. Fair sites
    switch to the next edition within weeks of closing, hence the short backward window."""
    import datetime as dt
    today = today or dt.date.today()
    try:
        start = dt.date.fromisoformat(str(show.get("start_date") or "")[:10])
    except ValueError:
        y = show_year(show)
        return y is not None and y == today.year
    return -60 <= (start - today).days <= 365


def rank_list_links(links, show, today=None):
    """Exhibitor-list candidates for this show's edition, best first, plus links of other editions (not imported:
    a wrong year would invent 'N shows in 12 months'). -> (ranked [{url, title, score, why, confirmed}], other_editions)
    confirmed=False: an undated link on a site whose list pages already carry a LATER year (the site has rolled over,
    e.g. ADIPEC 2026 just ended and its pages say 2027) - shown to the user and Claude, never imported automatically."""
    want = show_year(show)
    cur = current_edition(show, today)
    later = bool(want) and any(int(y) > want for l in links
                               for y in YEAR.findall(f'{urlparse(l.get("url") or "").path} {l.get("title") or ""}')
                               if any(w in f'{l.get("url") or ""} {l.get("title") or ""}'.lower() for w in LIST_WORDS))
    ranked, other = [], []
    seen = set()
    for l in links:
        url = (l.get("url") or "").strip()
        key = url.rstrip("/").lower()
        if not url or key in seen:
            continue
        seen.add(key)
        title = (l.get("title") or "").strip()
        text = f"{url.lower()} {title.lower()}"
        host = urlparse(url).netloc.lower()
        path = urlparse(url).path.lower()
        is_pdf = path.endswith(".pdf")
        hits = [w for w in LIST_WORDS if w in text]
        directory = any(h in host for h in DIRECTORY_HOSTS)
        if not hits and not directory:
            continue
        if any(b in path or b in title.lower() for b in BAD_WORDS) and not directory:
            continue
        score = 3 * len(hits) + (6 if directory else 0) + (3 if is_pdf else 0)
        if any(w in text for w in ("exhibitor-list", "exhibitor list", "exhibitorlist", "list of exhibitors",
                                   "exhibitor directory", "exhibitor-directory")):
            score += 6
        if "product" in text:
            score -= 4   # product search pages, not the company list
        years = {int(y) for y in YEAR.findall(f"{path} {title}")}
        if want and years and want not in years:
            other.append({"url": url, "title": title, "years": sorted(years)})
            continue
        confirmed = True
        if want and years and want in years:
            score += 5
            why = f"{want} list"
        elif not cur:
            other.append({"url": url, "title": title, "years": []})
            continue
        elif later:
            confirmed, why = False, f"undated, and the site already shows a later edition than {want}: check the year"
        else:
            why = "undated page of the current edition"
        ranked.append({"url": url, "title": title, "score": score, "why": why, "pdf": is_pdf, "directory": directory,
                       "confirmed": confirmed})
    ranked.sort(key=lambda r: (not r["confirmed"], -r["score"]))
    return ranked, other


# ---------- a company's own events / exhibitions pages ----------
EVENT_WORDS = ("exhibition", "exhibitions", "trade-fair", "trade fair", "tradeshow", "trade-show", "trade show",
               "events", "event", "meet-us", "meet us", "visit-us", "visit us", "fairs", "expo", "messe", "booth",
               "stall", "news")


def rank_event_links(links, limit=8):
    out = []
    for l in links:
        url, title = l.get("url") or "", l.get("title") or ""
        text = f"{url.lower()} {title.lower()}"
        hits = [w for w in EVENT_WORDS if w in text]
        if not hits or any(b in text for b in ("career", "job", "privacy", "login", "cart")):
            continue
        score = len(hits) + (3 if any(w in text for w in ("exhibition", "trade fair", "trade-fair", "meet us", "visit us", "/fairs", "fairs-", "messe",
                                                          "trade show", "tradeshow")) else 0)
        out.append({"url": url, "title": title, "score": score})
    return sorted(out, key=lambda r: -r["score"])[:limit]
