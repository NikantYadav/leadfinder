"""Company-name and domain normalisation, so that 'Bharat Forge Ltd', 'BHARAT FORGE LIMITED'
and 'Bharat Forge Pvt. Ltd.' collapse to one company."""
import re
from urllib.parse import urlparse

LEGAL_SUFFIXES = {
    "pvt", "private", "ltd", "limited", "llp", "llc", "inc", "incorporated", "corp", "corporation",
    "co", "company", "plc", "gmbh", "ag", "kg", "spa", "srl", "sa", "sas", "bv", "nv", "oy", "ab",
    "as", "pte", "pty", "kk", "sdn", "bhd", "tbk", "jsc", "ooo", "india",
}
# Legal suffixes are stripped only from the end of the name (they can be meaningful mid-name).
PREFIXES = ("m/s ", "m/s. ", "ms ", "the ")


def norm_company(name: str) -> str:
    if not name:
        return ""
    s = name.lower().strip()
    for p in PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
    s = s.replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)          # drop "(India)", "(P)" etc.
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    words = s.split()
    # strip legal suffixes from the end only, keep at least one word
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


BRAND_DROP = LEGAL_SUFFIXES | {"group", "company", "technologies", "solutions", "systems"}


def search_name(name: str) -> str:
    """Short brand form for search queries: 'Ace Micromatic Group' -> 'Ace Micromatic',
    'Bharat Electronics Ltd (BEL)' -> 'Bharat Electronics', 'S & T Engineers Pvt. Ltd' -> 'S & T Engineers'."""
    s = re.sub(r"\(.*?\)", " ", name or "")
    s = s.split("|")[0].split(" / ")[0]
    words = s.replace(",", " ").split()
    while len(words) > 1 and re.sub(r"[^a-z]", "", words[-1].lower()) in BRAND_DROP:
        if words[-1].lower() == "india" and len(" ".join(words[:-1])) < 6:
            break  # "Axles India" stays; "Airbus India" -> "Airbus"
        words.pop()
    return " ".join(words).strip(" .-&")


def norm_domain(url_or_domain: str) -> str:
    if not url_or_domain:
        return ""
    s = url_or_domain.strip().lower()
    if "://" not in s:
        s = "http://" + s
    host = urlparse(s).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    # reduce to the registrable domain: jobs.boeing.com -> boeing.com, x.tata.co.in -> tata.co.in
    labels = host.split(".")
    keep = 3 if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "net", "ac", "gov", "edu"} \
        and len(labels[-1]) == 2 else 2
    host = ".".join(labels[-keep:]) if len(labels) > keep else host
    # ignore domains that never identify a company
    if host in {"linkedin.com", "in.linkedin.com", "facebook.com", "google.com", "youtube.com",
                "twitter.com", "x.com", "instagram.com", "indiamart.com", "tradeindia.com"}:
        return ""
    return host


def norm_person(name: str) -> str:
    s = re.sub(r"[^a-z ]+", " ", (name or "").lower())
    s = re.sub(r"\b(mr|mrs|ms|dr|er|prof)\b", " ", s)
    return " ".join(s.split())


def same_company(a: str, b: str) -> bool:
    """Loose match used when checking a search-result snippet against a target company."""
    na, nb = norm_company(a), norm_company(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = sorted((na, nb), key=len)
    # prefix match only when the shorter name is specific enough ("bharat forge" ~ "bharat forge kalyani")
    return len(short.split()) >= 2 and long_.startswith(short + " ")


def _flat(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("&", "and"))


def strict_company_match(candidate, company_name):
    """True when `candidate` (a LinkedIn current-company string or headline) names our company, without legal-suffix
    stripping: 'Wheels India Limited' matches 'Wheels India', 'Wheels Inc' does not."""
    c = _flat(candidate)
    full = _flat(re.sub(r"\(.*?\)", "", company_name))
    want = _flat(search_name(company_name))
    if not c or not want:
        return False
    if full and (c.startswith(full) or full in c):
        return True
    if _core(candidate) and _core(candidate) == _core(company_name):   # 'LMW Limited' == 'LMW Ltd'
        return True
    brand = search_name(company_name)
    if brand.isupper() and len(brand) >= 2 and re.search(rf"\b{re.escape(brand.lower())}\b", (candidate or "").lower()):
        return True                                                    # acronyms: LMW, BHEL, TVS
    if len(_flat(brand)) >= 6 and _brand_as_employer(candidate, brand):
        return True                                                    # 'at Airbus', 'The Boeing Company'

    return len(want) >= 8 and (c.startswith(want) or want in c)


def _core(name):
    """Company name minus legal suffixes, but keeping 'India' (strict comparisons)."""
    s = re.sub(r"\(.*?\)", " ", (name or "").lower().replace("&", " and "))
    words = re.sub(r"[^a-z0-9 ]+", " ", s).split()
    while len(words) > 1 and words[-1] in (LEGAL_SUFFIXES - {"india"}):
        words.pop()
    return "".join(words)


_PREV_OK = {"at", "the", "of", "with", "for", "in", "@"}
_NEXT_OK = {"india", "group", "company", "limited", "ltd", "pvt", "private", "corporation", "corp", "co",
            "and", "industries"}


def _brand_as_employer(candidate, brand):
    """Brand appears as the employer itself, not inside another name: preceded by nothing/'at'/'the'/punctuation
    and followed by nothing/punctuation/India/Group/Company/Limited. Rejects 'Wheels, LLC', 'Wheels Inc', 'Hot Wheels'."""
    toks = re.findall(r"[a-z0-9]+|[|,·@()\-–:;/.!]", (candidate or "").lower().replace("&", " and "))
    b = re.findall(r"[a-z0-9]+", brand.lower().replace("&", " and "))
    n = len(b)
    for i in range(len(toks) - n + 1):
        if toks[i:i + n] != b:
            continue
        prev = toks[i - 1] if i else ""
        nxt = toks[i + n] if i + n < len(toks) else ""
        nxt2 = toks[i + n + 1] if i + n + 1 < len(toks) else ""
        prev_ok = prev == "" or prev in _PREV_OK or not prev.isalnum()
        if nxt == "," and nxt2 in {"llc", "inc", "ltd", "limited", "corp", "gmbh"}:
            continue  # "Wheels, LLC"
        next_ok = nxt == "" or nxt in _NEXT_OK or not nxt.isalnum()
        if prev_ok and next_ok:
            return True
    return False
