"""Per-search targeting set in the web form (stored in industries.options_json["target"]).

These are hard constraints: they are given to Claude when it writes the personas AND enforced in Python afterwards,
so the personas, LinkedIn filters, search region and profile checks all follow them. Empty fields mean "let the
description decide". Searches made before this existed fall back to their Region text and the .env defaults.
"""
import json
import re

from . import config

COUNTRY_CODES = {
    "india": "IN", "united states": "US", "usa": "US", "us": "US", "united kingdom": "GB", "uk": "GB",
    "germany": "DE", "france": "FR", "italy": "IT", "spain": "ES", "netherlands": "NL", "belgium": "BE",
    "united arab emirates": "AE", "uae": "AE", "saudi arabia": "SA", "qatar": "QA", "oman": "OM", "kuwait": "KW",
    "bahrain": "BH", "singapore": "SG", "malaysia": "MY", "indonesia": "ID", "thailand": "TH", "vietnam": "VN",
    "philippines": "PH", "bangladesh": "BD", "sri lanka": "LK", "nepal": "NP", "china": "CN", "japan": "JP",
    "south korea": "KR", "korea": "KR", "australia": "AU", "new zealand": "NZ", "canada": "CA", "mexico": "MX",
    "brazil": "BR", "south africa": "ZA", "nigeria": "NG", "kenya": "KE", "egypt": "EG", "turkey": "TR",
    "poland": "PL", "sweden": "SE", "switzerland": "CH", "austria": "AT", "czech republic": "CZ", "ireland": "IE",
    "israel": "IL", "russia": "RU",
}
# Everything below is free text from the user. Where a phrase maps onto a LinkedIn filter we use the filter;
# every phrase (mapped or not) also goes to Claude as a hard constraint, so nothing the user types is dropped.
SENIORITY_MAP = [  # (LinkedIn seniority id name, words that mean it)
    ("owner", ("owner", "founder", "proprietor", "partner", "promoter")),
    ("cxo", ("cxo", "ceo", "cfo", "coo", "cto", "cpo", "chief", "managing director", "md", "president")),
    ("vp", ("vp", "vice president", "svp", "evp", "avp")),
    ("director", ("director", "head", "gm", "general manager")),
    ("manager", ("manager", "lead", "incharge", "in-charge", "supervisor")),
    ("senior", ("senior", "sr", "specialist", "executive", "engineer", "officer")),
    ("strategic", ("strategic",)),
]
FUNCTION_MAP = [  # (LinkedIn function name, words that mean it)
    ("purchasing", ("purchas", "procure", "sourcing", "buyer", "supply chain", "vendor", "commodit")),
    ("sales", ("sales", "account", "export", "commercial", "channel", "distribution")),
    ("business_development", ("business development", "bd", "partnership", "alliances")),
    ("operations", ("operation", "plant", "production", "manufacturing", "works", "maintenance", "quality")),
    ("marketing", ("marketing", "brand", "event", "communication")),
]
HEADCOUNT_BANDS = [(1, 10, "B"), (11, 50, "C"), (51, 200, "D"), (201, 500, "E"), (501, 1000, "F"),
                   (1001, 5000, "G"), (5001, 10000, "H"), (10001, 10 ** 9, "I")]
LIST_FIELDS = ("countries", "cities", "exclude_locations", "titles", "exclude_titles", "sectors", "seniority",
               "functions", "headcount")

# which pipeline step must re-run when a field changes
RERUN_FROM = {"countries": "persona", "sectors": "persona", "titles": "persona", "exclude_titles": "persona",
              "seniority": "persona", "functions": "persona", "company_rule": "persona",
              "cities": "linkedin", "exclude_locations": "linkedin", "headcount": "linkedin"}


def _list(v):
    if isinstance(v, str):
        v = re.split(r"[,\n;]+", v)
    return [str(x).strip() for x in (v or []) if str(x).strip()][:40]


def clean(raw):
    """Validate what the browser sent."""
    raw = raw or {}
    t = {k: _list(raw.get(k)) for k in LIST_FIELDS}
    t["company_rule"] = str(raw.get("company_rule") or "").strip()[:300]
    return {k: v for k, v in t.items() if v}


def get(ind):
    try:
        opts = json.loads(ind["options_json"] or "{}")
    except (TypeError, ValueError, KeyError, IndexError):
        opts = {}
    t = dict(opts.get("target") or {})
    if not t.get("countries"):  # older searches: derive from the Region text
        geo = (ind["geo"] if "geo" in ind.keys() else "") or ""
        t["countries"] = [c for c in _list(geo.replace(" + ", ",").replace(" and ", ","))
                          if c.lower() in COUNTRY_CODES]
    return t


def country_code(name):
    return COUNTRY_CODES.get((name or "").strip().lower()) or (name.upper() if len(name or "") == 2 else None)


def region_code(ind):
    """Search-engine region (Serper gl / TinySearch location) for this search: its first country."""
    for c in get(ind).get("countries") or []:
        cc = country_code(c)
        if cc:
            return cc
    return "IN"


def target_countries(ind=None):
    """Lower-case country names + codes a profile may be in without being flagged 'outside target geo'."""
    countries = get(ind).get("countries") if ind is not None else None
    if not countries:
        raw = config.get("TARGET_COUNTRIES", "India,IN")
        return {c.strip().lower() for c in raw.split(",") if c.strip()}
    out = set()
    for c in countries:
        out.add(c.lower())
        cc = country_code(c)
        if cc:
            out.add(cc.lower())
            out |= {n for n, code in COUNTRY_CODES.items() if code == cc}
    return out


def linkedin_locations(ind):
    t = get(ind)
    locs = t.get("cities") or t.get("countries")
    if locs:
        return locs
    return [c.strip() for c in config.get("LINKEDIN_LOCATIONS", "India").split(",") if c.strip()]


def hubs(ind, icp_doc=None):
    t = get(ind)
    return t.get("cities") or (icp_doc or {}).get("hubs") or t.get("countries") or ["India"]


def prompt_block(ind):
    """The constraints, written for Claude."""
    t = get(ind)
    lines = []
    if t.get("countries"):
        lines.append(f"- Countries (people must be based here; shows/companies should be in or sell into these): "
                     f"{', '.join(t['countries'])}")
    if t.get("cities"):
        lines.append(f"- Focus cities / industrial hubs (put exactly these in `hubs`): {', '.join(t['cities'])}")
    if t.get("exclude_locations"):
        lines.append(f"- Exclude these locations: {', '.join(t['exclude_locations'])}")
    if t.get("sectors"):
        lines.append(f"- Company sectors (put exactly these in `sectors`): {', '.join(t['sectors'])}")
    if t.get("titles"):
        lines.append(f"- Job titles to target (include all of them in `titles`): {', '.join(t['titles'])}")
    if t.get("exclude_titles"):
        lines.append(f"- Titles/words to exclude (add to every ICP's `exclude_keywords`): {', '.join(t['exclude_titles'])}")
    if t.get("seniority"):
        lines.append(f"- Seniority, in the user's words: {', '.join(t['seniority'])}")
    if t.get("functions"):
        lines.append(f"- Department / function, in the user's words: {', '.join(t['functions'])}")
    if t.get("company_rule"):
        rule = rule_from_text(t["company_rule"])
        lines.append(f"- Trade-show requirement, in the user's words: \"{t['company_rule']}\""
                     + (f" (company_rule: {rule})" if rule else " - choose company_rule/find_via to match it"))
    if t.get("headcount"):
        lines.append(f"- Company size (employees): {', '.join(t['headcount'])}")
    if not lines:
        return ""
    return "HARD CONSTRAINTS the user set in the form (these override anything the description implies):\n" + "\n".join(lines)


def apply(icp_doc, ind):
    """Enforce the form's constraints on Claude's personas. Returns the changed doc."""
    t = get(ind)
    doc = dict(icp_doc)
    if t.get("countries"):
        doc["geo"] = ", ".join(t["countries"])
    if t.get("cities"):
        doc["hubs"] = t["cities"]
    if t.get("sectors"):
        doc["sectors"] = t["sectors"]
    for icp in doc.get("icps", []):
        f = dict(icp.get("linkedin_filters") or {})
        sen, fn = map_terms(t.get("seniority"), SENIORITY_MAP), map_terms(t.get("functions"), FUNCTION_MAP)
        if sen:
            f["seniority"] = sen
        if fn:
            f["functions"] = fn
        icp["linkedin_filters"] = f
        rule = rule_from_text(t.get("company_rule"))
        if rule:
            icp["company_rule"] = rule
            if rule == "any":
                icp["find_via"] = "industry_search"
        if t.get("titles"):
            icp["titles"] = list(dict.fromkeys((icp.get("titles") or []) + t["titles"]))
            kws = [x.lower() for x in t["titles"]]
            icp["title_keywords"] = list(dict.fromkeys((icp.get("title_keywords") or []) + kws))
        if t.get("exclude_titles"):
            ex = [x.lower() for x in t["exclude_titles"]]
            icp["exclude_keywords"] = list(dict.fromkeys((icp.get("exclude_keywords") or []) + ex))
    return doc


def rerun_from(old, new):
    """Earliest pipeline step affected by a change in targeting (None = nothing changed)."""
    order = ["persona", "research", "linkedin"]
    norm = lambda v: sorted(x.lower() for x in v) if isinstance(v, list) else (v or "")  # noqa: E731
    hit = [RERUN_FROM[k] for k in set(old) | set(new)
           if k in RERUN_FROM and norm(old.get(k) or []) != norm(new.get(k) or [])]
    return min(hit, key=order.index) if hit else None


def refresh_geo_flags(con, ind):
    """Re-derive the 'outside target geo' part of every flag for this search from the stored country, so changing
    the countries un-hides (or hides) people already found. No API calls."""
    tc = target_countries(ind)
    from .db import IN_GROUP
    rows = con.execute(f"""SELECT p.id, p.country, p.profile_flag FROM people p
                           JOIN company_industries ci ON ci.company_id=p.company_id {IN_GROUP}
                           WHERE ci.industry_id=?""", (ind["id"],)).fetchall()
    changed = 0
    for r in rows:
        parts = [x for x in (r["profile_flag"] or "").split("; ") if x and not x.startswith("outside target geo")]
        c = (r["country"] or "").strip()
        if c and c.lower() not in tc:
            parts.insert(0, f"outside target geo ({c})")
        flag = "; ".join(parts) or None
        if flag != r["profile_flag"]:
            con.execute("UPDATE people SET profile_flag=? WHERE id=?", (flag, r["id"]))
            changed += 1
    con.commit()
    return changed


def map_terms(terms, table):
    """Free-text phrases -> the LinkedIn filter values they clearly mean (unmatched phrases are left to Claude)."""
    out = []
    for term in terms or []:
        low = f" {term.lower()} "
        for value, words in table:
            if any(re.search(rf"(?<![a-z]){re.escape(w)}", low) for w in words) and value not in out:
                out.append(value)
    return out


_NUM_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def shows_wanted(text):
    """'exhibits at 5+ shows', 'at least four trade fairs a year', '3 or more exhibitions' -> 5 / 4 / 3; else None."""
    low = (text or "").lower()
    num = r"(\d{1,2}|" + "|".join(_NUM_WORDS) + r")"
    m = re.search(num + r"\s*(?:\+|or more|and above|plus)?\s*(?:\w+\s+){0,2}?(?:shows?|fairs?|exhibitions?|expos?|events?)",
                  low) or re.search(r"(?:at least|minimum of|min\.?)\s*" + num, low)
    if not m:
        return None
    v = m.group(1)
    n = int(v) if v.isdigit() else _NUM_WORDS[v]
    return n if 1 <= n <= 20 else None


def rule_from_text(text):
    """Map the user's trade-show wording onto a company rule when it's unambiguous, else None (Claude decides)."""
    low = (text or "").lower()
    if not low:
        return None
    exhibit, visit = re.search(r"exhibit|stall|booth", low), re.search(r"visit|buy|attend|source", low)
    if not (exhibit or visit) and re.search(r"\b(no|none|any|not needed|doesn'?t matter|without)\b", low):
        return "any"
    exhibit, visit = re.search(r"exhibit|stall|booth", low), re.search(r"visit|buy|attend|source", low)
    if exhibit and visit:
        return "visitor_or_exhibitor"
    if exhibit:
        n = shows_wanted(low)
        return "exhibitor_3plus" if (n and n >= 2) or re.search(r"\b(multiple|several|many|repeat|regular)\b", low) \
            else "exhibitor"
    if visit:
        return "visitor"
    return None


def headcount_codes(terms):
    """'50-500', '200+', 'under 50', 'SME' style text -> LinkedIn companyHeadcount codes overlapping the range."""
    codes = []
    for t in terms or []:
        low = t.lower().replace(",", "")
        nums = [int(n) for n in re.findall(r"\d+", low.replace("k", "000"))]
        if not nums:
            lo, hi = {"small": (1, 50), "sme": (11, 500), "mid": (201, 1000), "medium": (51, 1000),
                      "large": (1001, 10 ** 9), "enterprise": (5001, 10 ** 9)}.get(next(
                          (w for w in ("small", "sme", "mid", "medium", "large", "enterprise") if w in low), ""), (None, None))
            if lo is None:
                continue
        elif "+" in low or "over" in low or "above" in low or "more" in low:
            lo, hi = nums[0], 10 ** 9
        elif "under" in low or "below" in low or "less" in low or "<" in low:
            lo, hi = 1, nums[0]
        else:
            lo, hi = nums[0], nums[1] if len(nums) > 1 else nums[0]
        codes += [c for a, b, c in HEADCOUNT_BANDS if a <= hi and b >= lo and c not in codes]
    return codes
