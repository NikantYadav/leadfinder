"""Deepline Native people search (`search_contact`) through the `deepline` CLI. Tested live 2026-11-23.

Free within the workspace's included allowance (5,000 results at signup); `free_only` is always sent, so a search
never spends Deepline credits. Returns names, titles, companies, locations and LinkedIn URLs (no emails).
One company per request (domain when known, else name), 15 requests/minute on the standard plan.

Setup on a new machine:  pnpm add -g deepline  (or npm i -g deepline)  then  deepline auth register
"""
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .search import ProviderBusy, ProviderError

TOOL = "search_contact"
GAP_SECONDS = 4.2  # 15 requests a minute
_allowance = {"at": 0.0, "left": None}


def available():
    return shutil.which("deepline") is not None


def free_left(max_age=300):
    """Free search results left in the allowance (cached; one `describe` call). None if unreadable."""
    if not available():
        return None
    if _allowance["left"] is not None and time.time() - _allowance["at"] < max_age:
        return _allowance["left"]
    try:
        r = subprocess.run(["deepline", "describe", TOOL], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"Cost:\s*Free\s*[—-]\s*([\d,]+)\s+results remaining", r.stdout or "")
    left = int(m.group(1).replace(",", "")) if m else (0 if "Cost:" in (r.stdout or "") else None)
    if left is not None:
        _allowance.update(at=time.time(), left=left)
    return left


def search_available():
    left = free_left()
    return bool(left)


def _spent(n):
    if _allowance["left"] is not None:
        _allowance["left"] = max(0, _allowance["left"] - n)


def _run(payload, timeout=120):
    with tempfile.TemporaryDirectory(prefix="lf-deepline-") as d:
        out, rec = Path(d) / "r.json", Path(d) / "rc.json"
        try:
            p = subprocess.run(["deepline", "run", TOOL, "--input", json.dumps(payload), "--json-out", str(out),
                                "--receipt-out", str(rec)], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ProviderError("deepline: search timed out")
        receipt = json.loads(rec.read_text()) if rec.exists() else {}
        if receipt.get("status") != "completed" or not out.exists():
            msg = str((receipt.get("error") or {}).get("message") or p.stderr[-300:] or "failed")
            if any(w in msg.lower() for w in ("rate limit", "429", "too many")):
                raise ProviderBusy(f"deepline rate limited: {msg[:150]}")
            if any(w in msg.lower() for w in ("no api key", "not connected", "unauthor", "auth")):
                raise ProviderBusy(f"deepline not signed in (run: deepline auth register): {msg[:150]}")
            raise ProviderError(f"deepline: {msg[:200]}")
        return (json.loads(out.read_text()).get("output") or {}).get("persons") or []


def _queries(c):
    """What to try for one company, best first: its domain, its cleaned name, the name without 'India'
    ('BOBST INDIA PVT. LTD.' -> 'BOBST INDIA' -> 'BOBST'; the index only knows the brand)."""
    from ..normalize import search_name
    out = []
    if c.get("domain") and len(c["domain"]) >= 4:
        out.append({"domain": c["domain"]})
    name = search_name(c.get("name") or "").strip()
    for n in (name, re.sub(r"(?i)\s+india\b.*$", "", name).strip()):
        if len(n) >= 3 and {"company_name": n} not in out:
            out.append({"company_name": n})
    return out


def people_search(companies, title_terms, countries, max_items=50, per_company=None, log=print):
    """companies: dicts with name (+ optional domain). Current title matches any term, contact lives in one of the
    countries. Returns profile dicts like linkedin.people_search(); never spends paid credits.
    per_company defaults to an even share of max_items (at least 3), so one big company can't take every slot."""
    if not available():
        raise ProviderBusy("deepline CLI not installed")
    left = free_left()
    if left is not None and left <= 0:
        raise ProviderBusy("deepline: free people-search allowance used up")
    titles = [t for t in dict.fromkeys(t.strip() for t in title_terms if t and len(t.strip()) >= 2)][:100]
    per_company = per_company or max(3, -(-max_items // max(1, len(companies))))
    out, calls = [], 0
    for c in companies:
        if len(out) >= max_items or (left is not None and len(out) >= left):
            break
        for q in _queries(c):
            payload = {**q, "page_size": min(per_company, max_items - len(out)), "free_only": True}
            if titles:
                payload["title_lists"] = [{"name": "icp titles", "titles": titles}]
            if countries:
                payload["location_countries"] = list(countries)[:50]
            if calls:
                time.sleep(GAP_SECONDS)
            calls += 1
            try:
                persons = _run(payload)
            except ProviderError as e:
                log(f"  deepline: {c.get('name')}: {e}")
                persons = []
            if not persons:
                continue
            _spent(len(persons))
            for p in persons:
                name = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
                out.append({"name": name, "headline": p.get("title"), "url": p.get("linkedin_url"),
                            "company": p.get("company_name"), "title": p.get("title"),
                            "location": p.get("location"), "country": p.get("country"),
                            "for_company": c.get("name")})  # searched by this company's domain/name
            break
    return out
