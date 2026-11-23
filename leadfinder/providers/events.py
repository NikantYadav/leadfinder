"""Trade-show data from Apify actors: exhibitor catalogs, show discovery, and people seen at a show.

Exhibitor catalogs (tested live 2026-11-24 on the FREE Apify plan; none of these caps rows):
  rel8ble~*-exhibitors-scraper     one actor per fair organiser / platform, $0.0009-0.00095 per exhibitor, no start fee,
                                   seconds per run. interpack 60 rows $0.057 in 7 s; EuroBLECH (RX) 30 rows $0.027.
                                   Takes a fair NAME ("interpack", "euroblech") or the fair's site / directory URL,
                                   so a show without a usable list URL (or with a PDF one) can still be read.
  scrapersdelight~mapyourshow-exhibitor-scraper   any Map Your Show directory, ~$0.0018/row with profile details
                                   (website, country). IFT 2027: 30 rows $0.054.
  tidytools~trade-show-exhibitor-list-scraper   MYS / a2z / ExpoFP / Swapcard + AI fallback; $0.003/row, failures are
                                   free, so it is the probe for unknown hosts. Failed on Messe Duesseldorf and on the
                                   custom Indian sites (PackPlus, ProPak India).
  skython / getascraper            capped at 20 / 25 rows per run on the FREE plan: used only on a paid plan.
Other sources:
  zen-studio~10times-events-scraper   show discovery by topic + countries + dates. ~$0.0035/event. No website field.
  sherwood~trade-show-people-finder   people who posted/commented about a show on LinkedIn (no login: harvestapi),
                                   plus booth staff by role. $0.004/exhibitor, $0.01/person with evidence, plus the
                                   sub-actors it calls (min $0.30 each) - capped by maxSubActorSpendUsd. Reads newest
                                   posts first, so only recent shows yield people (interpack May 2026: 0; EuroBLECH
                                   Oct 2026: 4).
Not wired (need the user's logged-in cookie = account risk, the user's call): LinkedIn event attendees, Luma guests.

Runs are ASYNC (start -> poll -> read dataset), always on ONE token, with maxTotalChargeUsd as a hard cap. Callers
persist the returned run ref so a resumed pipeline adopts the run instead of paying for it twice.
"""
import re
import time
from urllib.parse import urlparse

import requests

from .. import keys
from .search import ProviderBusy, ProviderError

BASE = "https://api.apify.com/v2"
SKYTHON = "skython~exhibitor-list-scraper"
TIDY = "tidytools~trade-show-exhibitor-list-scraper"
MYS = "scrapersdelight~mapyourshow-exhibitor-scraper"
TENTIMES = "zen-studio~10times-events-scraper"
PEOPLE = "sherwood~trade-show-people-finder"
MIN_CAP_USD = 0.30  # Apify refuses a lower maxTotalChargeUsd; row limits keep the real spend at the expected cost
TERMINAL = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT")


def _r8(name):
    return f"rel8ble~{name}-exhibitors-scraper"


# Catalog readers. field: input taking [name or URL]; limit: rows per fair/show; names: fair names the actor knows
# (matched against the show's name); hosts: URL fragments of directories it reads (the URL is passed as is).
CATALOGS = [
    {"actor": _r8("messe-duesseldorf"), "field": "fairs", "limit": "maxResultsPerFair", "details": True,
     "price": 0.00095,
     "names": ["medica", "compamed", "interpack", "components", "k", "wire", "tube", "prowein", "euroshop", "eurocis",
               "aplusa", "glasstec", "rehacare", "valveworld", "boot", "caravan-salon", "beauty"],
     "hosts": ["/vis/v1/", "interpack.com", "medica-tradefair.com", "k-online.com", "wire-tradefair.com",
               "tube-tradefair.com", "prowein.com", "euroshop-tradefair.com", "aplusa-online.com", "glasstec-online.com",
               "rehacare.com", "valveworld.net", "boot.de", "caravan-salon.de", "eurocis.com"]},
    {"actor": _r8("deutsche-messe"), "field": "fairs", "limit": "maxResultsPerFair", "details": True,
     "names": ["hannover-messe", "ligna", "interschutz", "domotex", "surface-technology"],
     "hosts": ["hannovermesse.de", "ligna.de", "interschutz.org", "domotex.de"]},
    {"actor": _r8("messe-frankfurt"), "field": "fairs", "limit": "maxResultsPerFair", "details": True,
     "names": ["automechanika", "ambiente", "christmasworld", "creativeworld", "heimtextil", "techtextil", "texprocess",
               "light-building", "ish", "iffa", "prolight-sound", "eurobike", "sps", "formnext", "pcim", "intersec"],
     "hosts": ["messefrankfurt.com"]},
    {"actor": _r8("koelnmesse"), "field": "fairs", "limit": "maxResultsPerFair", "details": True,
     "names": ["anuga", "interzum", "ids", "ism", "anuga-foodtec", "didacta", "kind-jugend", "fsb", "orgatec",
               "aquanale"],
     "hosts": ["anuga.com", "interzum.com", "ids-cologne.de", "ism-cologne.com", "anugafoodtec.com", "orgatec.com",
               "koelnmesse"]},
    {"actor": _r8("nuernbergmesse"), "field": "fairs", "limit": "maxResultsPerFair", "details": True,
     "names": ["biofach", "embedded-world", "fachpack", "interzoo", "chillventa", "galabau", "enforce-tac", "it-sa",
               "euroguss", "fensterbau-frontale", "iwa", "powtech", "holz-handwerk", "kommunale"],
     "hosts": ["biofach.de", "embedded-world.de", "fachpack.de", "interzoo.com", "chillventa.de", "euroguss.de",
               "powtech.de", "nuernbergmesse.de"]},
    {"actor": _r8("messe-berlin"), "field": "fairs", "limit": "maxResultsPerFair", "details": True,
     "names": ["itb", "innotrans", "gruene-woche", "fruit-logistica", "ifa", "ila"], "hosts": []},
    {"actor": _r8("rx"), "field": "shows", "limit": "maxResultsPerShow",
     "names": ["wtm-london", "iftm", "ibtm-world", "euroblech", "aluminium", "fibo", "atm", "psi", "in-cosmetics",
               "london-book-fair", "infosecurity-europe"],
     "hosts": ["euroblech.com", "/exhibitor-directory"]},
    {"actor": _r8("event-cloud"), "field": "fairs", "limit": "maxResultsPerFair",
     "names": ["smm", "internorga", "windenergy", "logimat", "cmt", "idd", "iaa-mobility", "iaa-transportation",
               "agritechnica", "spielwarenmesse"],
     "hosts": ["smm-hamburg.com", "logimat-messe.de", "agritechnica.com"]},
    {"actor": _r8("gitex-gulfood"), "field": "shows", "limit": "maxResultsPerShow", "details": True,
     "names": ["gitex-global", "gitex-europe", "gitex-asia", "gitex-africa", "gulfood", "gulfood-manufacturing"],
     "hosts": ["exhibitors.gulfood.com", "exhibitors.gitex.com", "xporience"]},
    {"actor": _r8("ieg"), "field": "fairs", "limit": "maxResultsPerFair", "details": True,
     "names": ["ecomondo", "ttg", "riminiwellness", "enada", "oroarezzo", "venditalia"],
     "hosts": ["iegexpo.it", "ecomondo.com", "ttgexpo.it", "sigep", "vicenzaoro"]},
    {"actor": _r8("hktdc-fair"), "field": "fairs", "limit": "maxResultsPerFair", "names": [], "hosts": ["hktdc.com"]},
    {"actor": _r8("a2z"), "field": "shows", "limit": "maxResultsPerShow", "details": True, "names": [],
     "hosts": ["smallworldlabs.com", "a2zinc.net"]},
    {"actor": _r8("expofp"), "field": "shows", "limit": "maxResultsPerShow", "names": [],
     "hosts": ["expofp.com", "expofp.io"]},
    {"actor": MYS, "field": "shows", "limit": "maxItems", "price": 0.0018, "names": [], "hosts": ["mapyourshow.com"],
     "extra": {"includeDetails": True, "includeContactPersons": False}},
    {"actor": TIDY, "price": 0.003, "names": [], "hosts": ["swapcard.", "expofp.", "mapyourshow.", "a2zinc.", "personify"]},
]
PRICE_DEFAULT = 0.0009
SKYTHON_SPEC = {"actor": SKYTHON, "price": 0.0029, "start_fee": 0.01, "min_rows": 20}
SKYTHON_PATTERNS = ("/vis/v1/", "messe", "koelnmesse", "hannovermesse", "nuernbergmesse", "rxglobal", "reedexpo",
                    "informamarkets", "xporience", "ungerboeck", "goeshow", "easyfairs", "comexposium",
                    "/exhibitors-products", "/aussteller", "exhibitor-search", "ausstellerverzeichnis")
ALIASES = {"a+a": "aplusa", "light + building": "light-building", "light+building": "light-building",
           "hannover messe": "hannover-messe", "gruene woche": "gruene-woche", "grune woche": "gruene-woche"}


def available():
    return keys.available("apify")


def paid_plan():
    """True when any configured Apify token is on a paid plan (skython reads only 20 exhibitors per run on FREE)."""
    for k in keys.all_keys("apify"):
        try:
            r = requests.get(f"{BASE}/users/me", headers=_h(k), timeout=20)
            if r.ok and ((r.json().get("data") or {}).get("plan") or {}).get("id") not in (None, "FREE"):
                return True
        except requests.RequestException:
            continue
    return False


def _tokens(text):
    t = (text or "").lower()
    for a, b in ALIASES.items():
        t = t.replace(a, b)
    t = re.sub(r"\b(19|20)\d\d\b", " ", t)
    return re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", t)


def _name_match(show_name, fair):
    """Whole-token match: 'interpack 2026' -> interpack, 'Hannover Messe' -> hannover-messe; 'K 2025' -> k only as
    a whole word, never inside another word."""
    toks = _tokens(show_name)
    want = fair.split("-")
    flat = " ".join(t.replace("-", " ") for t in toks).split()
    return fair in toks or any(flat[i:i + len(want)] == want for i in range(len(flat) - len(want) + 1))


def route(show, allow_capped=True):
    """Catalog readers to try for a show, best first: (spec, value) where value is the fair name or URL to pass.
    A fair-name match works even when the show has no list URL (or only a PDF); otherwise the list URL / site URL is
    matched by host; unknown hosts get the free-failing tidytools probe, then skython (paid plan only)."""
    if isinstance(show, str):
        show = {"name": "", "exhibitor_list_url": show}
    out = []
    for spec in CATALOGS:
        fair = next((n for n in spec["names"] if _name_match(show.get("name"), n)), None)
        if fair:
            out.append((spec, fair))
    for url in (show.get("exhibitor_list_url"), show.get("url")):
        u = (url or "").lower()
        if not u or u.split("?")[0].endswith(".pdf"):
            continue
        for spec in CATALOGS:
            if any(h in u for h in spec["hosts"]) and spec["actor"] not in [s["actor"] for s, _ in out]:
                out.append((spec, url))
    lst = show.get("exhibitor_list_url") or ""
    if lst and not lst.lower().split("?")[0].endswith(".pdf"):
        if TIDY not in [s["actor"] for s, _ in out]:
            out.append((CATALOGS[-1], lst))
        if allow_capped and any(p in lst.lower() for p in SKYTHON_PATTERNS):
            out.append((SKYTHON_SPEC, lst))
    return out


# ---------- async run plumbing ----------
def _key_by_state(state_id):
    for k in keys.all_keys("apify"):
        if k.state_id == state_id:
            return k
    raise ProviderError("the Apify token that started this run is no longer configured")


def _h(key):
    return {"Authorization": f"Bearer {key.value}"}


def _with_credit(min_usd):
    from .linkedin import apify_credit_left

    def ok(k):
        left = apify_credit_left(k)
        return left is None or left >= max(min_usd, MIN_CAP_USD)
    return ok


def start(actor, body, max_usd, log=None):
    """Start a run on the first token with at least max_usd credit left. Returns a ref {actor, run_id, key}."""
    def call(key):
        r = requests.post(f"{BASE}/acts/{actor}/runs", params={"maxTotalChargeUsd": max(MIN_CAP_USD, round(max_usd, 2))},
                          json=body, headers=_h(key), timeout=30)
        if r.status_code in (402, 403, 429):
            raise ProviderBusy(f"apify HTTP {r.status_code}: {r.text[:150]}")
        if not r.ok:
            raise ProviderError(f"apify HTTP {r.status_code}: {r.text[:200]}")
        keys.count(key, extra_bucket="runs:" + actor)
        return {"actor": actor, "run_id": r.json()["data"]["id"], "key": key.state_id}
    return keys.run("apify", call, log=log, only=_with_credit(max_usd))


def status(ref):
    key = _key_by_state(ref["key"])
    r = requests.get(f"{BASE}/actor-runs/{ref['run_id']}", headers=_h(key), timeout=30)
    if not r.ok:
        raise ProviderError(f"apify run {ref['run_id']}: HTTP {r.status_code}")
    return r.json()["data"]


def adopt(actor, run_id):
    """Ref for a run started elsewhere (UI, earlier session): finds the token that owns it."""
    for k in keys.all_keys("apify"):
        r = requests.get(f"{BASE}/actor-runs/{run_id}", headers=_h(k), timeout=30)
        if r.ok:
            return {"actor": actor, "run_id": run_id, "key": k.state_id}
    raise ProviderError(f"no configured Apify token can read run {run_id}")


def wait(ref, deadline=None, poll=15, log=None):
    """Poll until the run ends. Past the deadline the run is aborted; rows already paid for stay in its dataset."""
    last = None
    while True:
        d = status(ref)
        if d["status"] in TERMINAL:
            return d
        msg = d.get("statusMessage")
        if log and msg and msg != last:
            log(f"  apify {ref['actor'].split('~')[1]}: {msg[:120]}")
            last = msg
        if deadline and time.time() > deadline:
            key = _key_by_state(ref["key"])
            requests.post(f"{BASE}/actor-runs/{ref['run_id']}/abort", headers=_h(key), timeout=30)
            if log:
                log("  apify run aborted at the time limit; keeping the rows it already produced")
            time.sleep(3)
            return status(ref)
        time.sleep(poll)


def dataset(ref, run=None):
    run = run or status(ref)
    key = _key_by_state(ref["key"])
    out, offset = [], 0
    while True:
        r = requests.get(f"{BASE}/datasets/{run['defaultDatasetId']}/items",
                         params={"clean": 1, "offset": offset, "limit": 1000}, headers=_h(key), timeout=60)
        if not r.ok:
            raise ProviderError(f"apify dataset HTTP {r.status_code}")
        page = r.json()
        out += page
        if len(page) < 1000:
            return out
        offset += 1000


def cost(run):
    return float(run.get("usageTotalUsd") or 0)


def run_actor(actor, body, max_usd, deadline=None, log=None, ref=None, on_start=None):
    """start (or adopt `ref`) -> wait -> rows. Returns (rows, run, ref). on_start(ref) lets the caller persist the
    run before waiting, so a stop/resume adopts it instead of paying for a second run."""
    if not ref:
        ref = start(actor, body, max_usd, log=log)
        if on_start:
            on_start(ref)
    run = wait(ref, deadline, log=log)
    return dataset(ref, run), run, ref


# ---------- exhibitor catalogs ----------
def _norm_key(k):
    return re.sub(r"[^a-z]", "", k.lower())


def _pick(item, *names):
    flat = {_norm_key(k): v for k, v in item.items()}
    for n in names:
        v = flat.get(n)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            return ", ".join(v)
    return None


def parse_exhibitor(item):
    """One catalog row (skython / tidytools / similar) -> {name, website, country, city, booth, email, phone,
    profile_url, categories}. None for failure rows and rows without a company name."""
    if item.get("success") is False or item.get("type") in ("estimate", "error"):
        return None
    name = _pick(item, "companyname", "displayname", "name", "exhibitorname", "exhibitor", "company", "title")
    if not name:
        return None
    site = _pick(item, "companywebsite", "website", "web", "homepage", "companyurl")
    if site and not re.search(r"\.[a-z]{2,}", site, re.I):
        site = None
    return {"name": name, "website": site,
            "country": _pick(item, "companycountry", "country", "countryname"),
            "city": _pick(item, "companycity", "city"),
            "booth": _pick(item, "hallstands", "hallstand", "booth", "stand", "boothnumber", "boothcodes", "hall"),
            "email": _pick(item, "companyemail", "email"),
            "phone": _pick(item, "companyphone", "phone"),
            "profile_url": _pick(item, "exhibitorprofileurl", "profileurl", "detailurl", "exhibitorurl", "sourceurl"),
            "linkedin_url": _pick(item, "linkedin", "linkedinurl", "companylinkedin"),
            "categories": _pick(item, "zproductcategories", "productcategories", "categories", "category")}


def price(spec):
    return spec.get("price", PRICE_DEFAULT)


def exhibitor_body(spec, value, limit):
    a = spec["actor"]
    if a == SKYTHON:
        return {"start_url": value, "result_limit": max(spec["min_rows"], limit)}
    if a == TIDY:
        return {"urls": [value], "maxExhibitors": limit, "maxExhibitorsPerDirectory": limit,
                "includeDetails": True, "aiFallback": True}
    body = {spec["field"]: [value], spec["limit"]: limit, **spec.get("extra", {})}
    if spec.get("details"):
        body["includeDetails"] = True
    return body


def max_charge(spec, limit):
    rows = max(spec.get("min_rows", 0), limit)
    return spec.get("start_fee", 0) + rows * price(spec) + 0.02


# ---------- show discovery (10times) ----------
def discover_body(query, countries=None, start_date=None, end_date=None, max_items=40):
    body = {"query": query, "eventType": "tradeshow", "maxItems": max_items}
    if countries:
        body["countries"] = countries
    if start_date:
        body["startDate"] = start_date
    if end_date:
        body["endDate"] = end_date
    return body


def parse_event(item):
    if not item.get("name") or not item.get("startDate"):
        return None
    loc = item.get("location") or {}
    year = int(item["startDate"][:4])
    return {"name": item["name"], "year": year, "start_date": item["startDate"], "end_date": item.get("endDate"),
            "city": loc.get("cityName"), "country": loc.get("countryName"),
            "categories": ", ".join(c.get("name") for c in item.get("categories") or [] if c.get("name")),
            "source_url": item.get("url")}


# ---------- people seen at a show (sherwood) ----------
def people_body(show, target_companies=None, max_companies=40, max_posts=80, role_based=False, sub_usd=1.5):
    body = {"eventName": f'{show["name"]}' + ("" if str(show.get("year") or "") in show["name"] else
                                             f' {show.get("year") or ""}').rstrip(),
            "maxCompanies": max(20, max_companies), "maxPosts": max_posts, "maxEngagementsPerPost": 15,
            "includeRoleBased": role_based, "maxPeoplePerCompany": 3, "maxPeople": 300,
            "maxSubActorSpendUsd": sub_usd}
    for src, dst in (("start_date", "startDate"), ("end_date", "endDate"), ("url", "eventUrl"),
                     ("exhibitor_list_url", "exhibitorListUrl")):
        if show.get(src):
            body[dst] = show[src]
    if target_companies:
        body["targetCompanies"] = target_companies
    return body


def _ev_url(p):
    for e in p.get("evidence") or []:
        if e.get("url"):
            return e["url"], (e.get("excerpt") or "")[:300]
    return None, None


def parse_people(items):
    """sherwood output -> (exhibitors, people). Exhibitor staff stay attached to their company; non-staff people
    (visitors, buyers, speakers) come from 'attendees' items. Only people with a source URL are kept."""
    comps, people = [], []
    for it in items:
        t = it.get("type")
        if t == "company" and it.get("name"):
            ev = next((w for w in it.get("webEvidence") or [] if w.get("url")), None)
            comps.append({"name": it["name"], "website": it.get("website"), "country": it.get("country"),
                          "linkedin_url": it.get("linkedinUrl"),
                          "url": (ev or {}).get("url"), "snippet": (ev or {}).get("excerpt"),
                          "confirmed_by": it.get("exhibitorConfirmedBy")})
            for p in it.get("people") or []:
                people.append(_person(p, staff_of=it))
        elif t in ("attendees", "attendee"):
            for p in it.get("people") or it.get("attendees") or it.get("items") or ([it] if it.get("personName") else []):
                people.append(_person(p))
    return comps, [p for p in people if p and p["url"]]


def _person(p, staff_of=None):
    name = p.get("personName") or p.get("name")
    if not name:
        return None
    url, snippet = _ev_url(p)
    score = float(p.get("attendanceScore") or 0)
    head = p.get("headline") or ""
    at = re.search(r"\b(?:at|@|bei|chez)\s+([A-Z0-9][^|,;\n]{1,60})", head)  # "Sales Manager at EAGLE LASERS"
    return {"name": name, "title": head or (p.get("roleBasis") or {}).get("title"),
            "company": (staff_of or {}).get("name") or p.get("companyName") or (at.group(1).strip() if at else None),
            "company_domain": (staff_of or {}).get("website"),
            "linkedin_url": p.get("personLinkedinUrl") or p.get("linkedinUrl"),
            "staff": bool(staff_of) or bool(p.get("isExhibitorStaff")),
            "score": score, "level": p.get("attendanceLevel"), "source": p.get("source"),
            "url": url, "snippet": snippet}
