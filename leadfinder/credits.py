"""What is left on every API key, for the Free credits page.

Each number is labelled with where it came from:
  live   - read from the provider's own account API just now
  local  - counted by this tool (the provider has no balance API, or the limit is daily)
Balances are fetched in parallel and cached for 60 s (Refresh bypasses the cache).
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from . import db, keys
from .providers import email as emailp
from .providers import linkedin as lip

CACHE_SECONDS = 60
_cache = {"at": 0, "data": None}

SERVICES = [  # (service, label, what it is used for, free allowance text)
    ("tinyfish", "TinyFish", "web search + page fetch", "12,000 searches + 1,000 fetches per day"),
    ("serper", "Serper", "web search", "2,500 searches, one-time"),
    ("google_cse", "Google CSE", "web search", "100 searches per day"),
    ("firecrawl", "Firecrawl", "page fetch", "1,000 pages per month"),
    ("apify", "Apify", "LinkedIn search + profile checks", "$5 platform credit per month; 10 people-search runs"),
    ("brightdata", "Bright Data", "LinkedIn search (backup)", "trial credit"),
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
            left = d["limits"]["maxMonthlyUsageUsd"] - d["current"]["monthlyUsageUsd"]
            runs = lip.apify_actor_runs(key)
            note = f"renews {str((d.get('monthlyUsageCycle') or {}).get('endAt') or '')[:10]}"
            if runs is not None:
                note = f"people search: {max(0, lip.APIFY_FREE_RUNS - runs)} of {lip.APIFY_FREE_RUNS} free runs left · " + note
            return {"remaining": f"${left:.2f}", "unit": "credit", "note": note,
                    "runs_left": None if runs is None else max(0, lip.APIFY_FREE_RUNS - runs)}
        if service == "brightdata":
            r = requests.get("https://api.brightdata.com/customer/balance", timeout=20,
                             headers={"Authorization": f"Bearer {key.value}"})
            if r.status_code in (401, 403):
                return {"error": "balance hidden: this key has no billing permission (give it Admin/billing "
                                 "access in Bright Data > Settings > Users to see it here)"}
            d = r.json() if r.ok else {}
            return {"remaining": f"${d['balance']}", "unit": "balance"} if "balance" in d else {"error": f"HTTP {r.status_code}"}
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
    elif service == "google_cse":
        out.update(remaining=max(0, 100 - out["used_today"]), unit="searches left today",
                   note="counted by this tool")
    return out


def report(refresh=False):
    if not refresh and _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
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
    openserp = False
    try:
        from . import config
        openserp = requests.get(config.OPENSERP_URL + "/health", timeout=2).ok
    except requests.RequestException:
        pass
    data = {"services": services, "openserp": openserp, "checked_at": db.now()}
    _cache.update(at=time.time(), data=data)
    return data
