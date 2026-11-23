"""What is left on every API key, for the Free credits page.

Each number is labelled with where it came from:
  live   - read from the provider's own account API just now
  local  - counted by this tool (the provider has no balance API, or the limit is daily)
Balances are fetched in parallel (~5 s). report() answers instantly from the last result and refreshes it in the
background once it is older than 60 s; the server warms it at start-up. Refresh waits for a fresh read.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from . import db, keys
from .providers import email as emailp
from .providers import linkedin as lip

CACHE_SECONDS = 60
_cache = {"at": 0, "data": None}
_build_lock = threading.Lock()  # one balance sweep at a time; a second caller waits for the one in flight
_bg = {"running": False}

SERVICES = [  # (service, label, what it is used for, free allowance text)
    ("tinyfish", "TinyFish", "web search + page fetch", "12,000 searches + 1,000 fetches per day"),
    ("serper", "Serper", "web search", "2,500 searches, one-time"),
    ("firecrawl", "Firecrawl", "page fetch", "1,000 pages per month"),
    ("apify", "Apify", "LinkedIn search, profile checks, trade-show exhibitor lists", "$5 platform credit per month (~$0.10 per search)"),
    ("ai_ark", "AI Ark", "LinkedIn search by company name", "100 trial credits (0.5 per person found)"),
    ("brightdata", "Bright Data", "LinkedIn profile checks, company posts, people search (backup)", "trial credit"),
    ("prospeo", "Prospeo", "emails", "~75-100 credits per month"),
    ("hunter", "Hunter", "emails + company websites", "free plan searches per month"),
    ("apollo", "Apollo", "emails", "free plan credits"),
    ("tomba", "Tomba", "emails", "25 searches per 30 days"),
    ("bettercontact", "BetterContact", "emails", "50 credits, one-time"),
]
EMAIL = {p.name: p for p in emailp.PROVIDERS}


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _live(service, key):
    """-> dict(remaining, unit, note) or dict(error). Never raises."""
    try:
        if service in EMAIL:
            return EMAIL[service].balance(key)
        if service == "serper":
            r = requests.get("https://google.serper.dev/account", headers={"X-API-KEY": key.value}, timeout=20)
            d = r.json() if r.ok else {}
            return {"remaining": d["balance"], "unit": "searches"} if "balance" in d else {"error": f"HTTP {r.status_code}"}
        if service == "firecrawl":
            r = requests.get("https://api.firecrawl.dev/v2/team/credit-usage", timeout=20,
                             headers={"Authorization": f"Bearer {key.value}"})
            d = (r.json() if r.ok else {}).get("data") or {}
            if "remainingCredits" not in d:
                return {"error": f"HTTP {r.status_code}"}
            return {"remaining": d["remainingCredits"], "unit": "credits",
                    "note": f"renews {str(d.get('billingPeriodEnd') or '')[:10]}"}
        if service == "apify":
            r = requests.get("https://api.apify.com/v2/users/me/limits", timeout=20,
                             headers={"Authorization": f"Bearer {key.value}"})
            d = (r.json() if r.ok else {}).get("data") or {}
            if not d:
                return {"error": f"HTTP {r.status_code}"}
            left = lip.apify_credit_left(key)  # free plan: the $5 credit, not the $10 usage limit
            if left is None:
                left = d["limits"]["maxMonthlyUsageUsd"] - d["current"]["monthlyUsageUsd"]
            pages = max(0, int(left / lip.APIFY_SEARCH_PAGE_USD))
            note = (f"people search: ~{pages} searches (~{pages * 25:,} profiles) left · "
                    f"renews {str((d.get('monthlyUsageCycle') or {}).get('endAt') or '')[:10]}")
            return {"remaining": f"${left:.2f}", "unit": "credit", "note": note}
        if service == "ai_ark":
            left = lip.aiark_credits(key)
            if left is None:
                return {"error": "could not read AI Ark balance"}
            return {"remaining": f"{left:g}", "unit": "credits",
                    "note": f"people search: ~{int(left / lip.AIARK_CREDITS_PER_RESULT)} people left"}
        if service == "brightdata":
            r = requests.get("https://api.brightdata.com/customer/balance", timeout=20,
                             headers={"Authorization": f"Bearer {key.value}"})
            denied = f"people search: not enabled ({lip.BD_SEARCH_HOWTO})" if lip.bd_search_denied(key) else None
            if r.status_code in (401, 403):
                return {"error": "balance hidden: this key has no billing permission (give it Admin/billing "
                                 "access in Bright Data > Settings > Users to see it here)"
                                 + (f"; {denied}" if denied else "")}
            d = r.json() if r.ok else {}
            out = {"remaining": f"${d['balance']}", "unit": "balance"} if "balance" in d else {"error": f"HTTP {r.status_code}"}
            if denied:
                out["note"] = denied
            return out
    except Exception as e:  # noqa: BLE001 - one bad provider must not break the page
        return {"error": str(e)[:120]}
    return None  # no balance API: local counts only


def _local(service, key, con):
    """Usage this tool recorded for the key, against daily limits where the provider has them."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    out = {"used_month": keys.used(key, month, con), "used_today": keys.used(key, _today(), con)}
    if service == "tinyfish":
        s, f = keys.used(key, "search:" + _today(), con), keys.used(key, "fetch:" + _today(), con)
        out.update(remaining=f"{12000 - s:,} searches · {1000 - f:,} fetches", unit="left today",
                   note="counted by this tool; resets 00:00 UTC")
    return out


def _background_refresh():
    try:
        _build(refresh=False)
    except Exception:  # noqa: BLE001 - the next click retries; never kill the thread loudly
        pass
    finally:
        _bg["running"] = False


def warm():
    """Start a background balance sweep unless one is running (called at server start and when data goes stale)."""
    if _bg["running"]:
        return
    _bg["running"] = True
    threading.Thread(target=_background_refresh, daemon=True).start()


def report(refresh=False):
    """Instant when there is a previous result (stale ones also start a background refresh and say so)."""
    if not refresh and _cache["data"]:
        if time.time() - _cache["at"] >= CACHE_SECONDS:
            warm()
        return {**_cache["data"], "refreshing": _bg["running"]}
    return {**_build(refresh), "refreshing": False}


def _build(refresh=False):
    with _build_lock:
        if not refresh and _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]  # another caller just finished the sweep while we waited
        return _sweep(refresh)


def _sweep(refresh):
    con = db.connect()
    jobs = [(svc, k) for svc, *_ in SERVICES for k in keys.all_keys(svc)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        live = {(s, k.state_id): v for (s, k), v in zip(jobs, ex.map(lambda j: _live(*j), jobs))}
    services = []
    for svc, label, use, allowance in SERVICES:
        rows = []
        for k in keys.all_keys(svc):
            row = {"key": k.fp, "index": k.index, "resting_until": keys.cooling_until(k, con)}
            loc = _local(svc, k, con)
            row.update({"used_month": loc["used_month"], "used_today": loc["used_today"]})
            lv = live.get((svc, k.state_id))
            if lv and "error" not in lv:
                row.update(lv, source="live")
            elif "remaining" in loc:
                row.update(remaining=loc["remaining"], unit=loc["unit"], note=loc["note"], source="local")
                if lv:
                    row["error"] = lv["error"]
            else:
                row.update(source="none", error=(lv or {}).get("error"))
            rows.append(row)
        services.append({"service": svc, "label": label, "use": use, "free_allowance": allowance,
                         "env": keys.SPECS[svc][0], "keys": rows})
    from .providers import deepline as dlp
    if dlp.available():  # signed in through the deepline CLI, not a .env key
        left = dlp.free_left(max_age=0 if refresh else 300)
        row = {"key": "CLI", "index": 1, "resting_until": None, "used_month": 0, "used_today": 0}
        if left is None:
            row.update(source="none", error="could not read the allowance (signed in? run: deepline auth status)")
        else:
            row.update(remaining=f"{left:,}", unit="free results", note="people search, free allowance",
                       source="live")
        services.insert(next((i for i, s in enumerate(services) if s["service"] == "apify"), 0),
                        {"service": "deepline", "label": "Deepline", "use": "LinkedIn people search (first choice)",
                         "free_allowance": "5,000 people-search results", "env": "deepline CLI", "keys": [row]})
    openserp = False
    try:
        from . import config
        openserp = requests.get(config.OPENSERP_URL + "/health", timeout=2).ok
    except requests.RequestException:
        pass
    data = {"services": services, "openserp": openserp, "checked_at": db.now()}
    _cache.update(at=time.time(), data=data)
    return data
