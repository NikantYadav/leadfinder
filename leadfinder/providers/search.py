"""Search router: tries free providers in order, rests any provider that hits a captcha / rate limit,
caches every response so no query is ever paid for twice.

Provider order (each is skipped when not configured or cooling down):
  1. openserp   - self-hosted (docker), free, no key; Google captchas when queried fast -> paced.
  2. serper     - SERPER_API_KEY   (2,500 free queries one-time at serper.dev)

Every provider returns a list of {"title", "url", "snippet"}.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import requests

from .. import config, keys
from ..db import now


class ProviderBusy(Exception):
    """Captcha / rate-limit / quota: try another provider, retry this one later."""


class ProviderError(Exception):
    pass


# ---------------- individual providers ----------------
class OpenSERP:
    name, service = "openserp", None
    monthly_free = None  # unlimited, but paced

    def available(self):
        try:
            return requests.get(config.OPENSERP_URL + "/health", timeout=3).ok
        except requests.RequestException:
            return False

    def search(self, query, site=None, num=10, region=None):
        params = {"text": query, "limit": max(num, 10)}
        if site:
            params["site"] = site
        if region:
            params["region"] = region
        try:
            r = requests.get(f"{config.OPENSERP_URL}/google/search", params=params, timeout=150)
        except requests.RequestException as e:
            raise ProviderError(str(e))
        data = _json(r)
        err = data.get("error") if isinstance(data, dict) else None
        if r.status_code == 429 or err in ("captcha_detected", "blocked", "rate_limited"):
            raise ProviderBusy(err or "429")
        if not r.ok:
            raise ProviderError(f"HTTP {r.status_code}: {str(data)[:200]}")
        results = [{"title": x.get("title") or "", "url": x.get("url") or "", "snippet": x.get("snippet") or ""}
                   for x in data.get("results") or [] if x.get("type", "organic") == "organic"]
        if site and results and not _honours_site(results, site):
            # Google sometimes silently drops the site: filter when it is throttling us.
            raise ProviderBusy("site filter ignored (soft throttle)")
        return results


class Serper:
    name = service = "serper"
    monthly_free = 2500  # one-time grant

    def available(self):
        return keys.configured(self.service)

    def search(self, query, site=None, num=10, region=None, key=None):
        key = key or keys.all_keys(self.service)[0]
        # Free accounts reject num > 10 ("Query pattern not allowed"), so fetch 10 per page (1 credit each).
        q = f"site:{site} {query}" if site else query
        out = []
        self.last_cost = 0
        for page in range(1, (min(num, 30) + 9) // 10 + 1):
            body = {"q": q, "num": 10, "page": page}
            if region:
                body["gl"] = region.lower()
            r = requests.post("https://google.serper.dev/search", json=body, timeout=60,
                              headers={"X-API-KEY": key.value, "Content-Type": "application/json"})
            if r.status_code in (402, 429):
                if out:
                    break
                raise ProviderBusy(f"HTTP {r.status_code}")
            if not r.ok:
                raise ProviderError(f"HTTP {r.status_code}: {r.text[:200]}")
            self.last_cost += 1
            items = r.json().get("organic", [])
            out += [{"title": x.get("title", ""), "url": x.get("link", ""), "snippet": x.get("snippet", "")}
                    for x in items]
            if len(items) < 9:  # no further page worth paying for
                break
        return out


class TinySearch:
    """TinyFish TinySearch (docs read 2026-11-18): GET https://api.search.tinyfish.ai, header X-API-Key.
    Free up to 12,000 requests/day (resets 00:00 UTC), 30 requests/min; ~10 results per page, page 0..10.
    site: operators are still accepted in the query."""
    name, service = "tinysearch", "tinyfish"
    monthly_free = None
    daily_free = 12000

    def available(self):
        return keys.configured(self.service)

    _last_call = {}  # key -> monotonic time of its last request (limit is 30/min per key)

    def _get(self, params, key):
        """One paced request: keep under the per-key rate limit instead of bursting into a 429, and wait out a short
        429 (Retry-After) once or twice before declaring the key busy for 30 minutes."""
        for attempt in range(3):
            wait = config.TINYSEARCH_MIN_GAP - (time.monotonic() - self._last_call.get(key.value, 0))
            if wait > 0:
                time.sleep(wait)
            r = requests.get("https://api.search.tinyfish.ai", params=params, timeout=60,
                             headers={"X-API-Key": key.value})
            self._last_call[key.value] = time.monotonic()
            if r.status_code != 429 or attempt == 2:
                return r
            try:
                delay = float((getattr(r, "headers", None) or {}).get("Retry-After", ""))
            except ValueError:
                delay = 20
            if delay > 90:  # a long wait means a quota, not a burst: let the key rest
                return r
            time.sleep(max(delay, 5))
        return r

    def search(self, query, site=None, num=10, region=None, key=None):
        key = key or keys.all_keys(self.service)[0]
        out = []
        self.last_cost = 0
        for page in range(0, (min(num, 30) + 9) // 10):
            # domain filtering via include_domains (site: in the query is deprecated per the docs);
            # path-level filtering (/in/ vs /posts/) is done by our parsers
            params = {"query": query, "page": page}
            if site:
                params["include_domains"] = site.split("/")[0]
            if region:
                params["location"] = region.upper()
            r = self._get(params, key)
            if r.status_code in (402, 429, 503):
                if out:
                    break
                raise ProviderBusy(f"HTTP {r.status_code}" + (" (rate limited)" if r.status_code != 402 else ""))
            if not r.ok:
                raise ProviderError(f"HTTP {r.status_code}: {r.text[:200]}")
            self.last_cost += 1
            items = _json(r).get("results") or []
            out += [{"title": x.get("title") or "", "url": x.get("url") or "", "snippet": x.get("snippet") or ""}
                    for x in items]
            if len(items) < 9:
                break
        if site and out and not _honours_site(out, site):
            raise ProviderError("site filter ignored")
        return out


# Order: TinySearch (free 12k/day) -> OpenSERP (free, captcha-prone) -> Serper (2,500 one-time)
PROVIDERS = [TinySearch(), OpenSERP(), Serper()]


# ---------------- router ----------------
def _json(r):
    try:
        return r.json()
    except ValueError:
        return {}


def _honours_site(results, site):
    dom = site.split("/")[0]
    hits = sum(1 for x in results if dom in x["url"] or "google.com/goto" in x["url"])
    return hits >= max(1, len(results) // 2)


def _cooling(con, provider):
    row = con.execute("SELECT cooldown_until, last_used FROM provider_state WHERE provider=?", (provider,)).fetchone()
    if row and row["cooldown_until"] and row["cooldown_until"] > now():
        return row["cooldown_until"]
    return None


def _set_state(con, provider, cooldown=False):
    until = None
    if cooldown:
        until = (datetime.now(timezone.utc) + timedelta(minutes=config.COOLDOWN_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute("""INSERT INTO provider_state(provider, cooldown_until, last_used) VALUES (?,?,?)
                   ON CONFLICT(provider) DO UPDATE SET cooldown_until=excluded.cooldown_until,
                   last_used=excluded.last_used""", (provider, until, now()))
    con.commit()


def _pace_openserp(con):
    row = con.execute("SELECT last_used FROM provider_state WHERE provider='openserp'").fetchone()
    if row and row["last_used"]:
        last = datetime.strptime(row["last_used"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        wait = config.OPENSERP_MIN_GAP - (datetime.now(timezone.utc) - last).total_seconds()
        if wait > 0:
            time.sleep(wait)


def _count_credit(con, provider, n=1):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    con.execute("""INSERT INTO credit_usage(provider, month, used) VALUES (?,?,?)
                   ON CONFLICT(provider, month) DO UPDATE SET used=used+?""", (provider, month, n, n))


def search(con, query, site=None, num=10, region="IN", use_cache=True, only=None):
    """Returns (results, provider_name). Raises ProviderBusy if every provider is unavailable right now."""
    params = json.dumps({"site": site, "num": num, "region": region}, sort_keys=True)
    if use_cache:
        row = con.execute("SELECT provider, response FROM search_cache WHERE query=? AND params=?",
                          (query, params)).fetchone()
        if row:
            return json.loads(row["response"]), f"cache:{row['provider']}"
    reasons = []
    for p in PROVIDERS:
        if only and p.name not in only:
            continue
        if not p.available():
            reasons.append(f"{p.name}: not configured/running")
            continue
        until = _cooling(con, p.name)
        if until:
            reasons.append(f"{p.name}: cooling down until {until}")
            continue
        if p.name == "openserp":
            _pace_openserp(con)
        used_key = []

        def call(k=None, p=p):
            used_key.append(k)
            return p.search(query, site=site, num=num, region=region, **({"key": k} if k else {}))
        try:
            # keyed services rotate through their keys (each key rests on its own); OpenSERP has no key
            results = keys.run(getattr(p, "service", None), call, con)
        except ProviderBusy as e:
            if not getattr(p, "service", None):
                _set_state(con, p.name, cooldown=True)
            reasons.append(f"{p.name}: busy ({e})")
            continue
        except ProviderError as e:
            _set_state(con, p.name)
            reasons.append(f"{p.name}: error ({e})")
            continue
        _set_state(con, p.name)
        cost = getattr(p, "last_cost", 1) or 1
        _count_credit(con, p.name, cost)
        if used_key and used_key[-1]:
            keys.count(used_key[-1], cost, con, extra_bucket="search:" + datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        if results:  # never cache an empty answer: it may be a silent throttle, retry it with another provider
            con.execute("INSERT OR REPLACE INTO search_cache VALUES (?,?,?,?,?)",
                        (p.name, query, params, json.dumps(results), now()))
        con.commit()
        return results, p.name
    raise ProviderBusy("; ".join(reasons) or "no search providers configured")


def google_link(query, site=None):
    from urllib.parse import quote_plus
    q = f"site:{site} {query}" if site else query
    return "https://www.google.com/search?q=" + quote_plus(q)


def status(con):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    out = []
    for p in PROVIDERS:
        used = con.execute("SELECT used FROM credit_usage WHERE provider=? AND month=?", (p.name, month)).fetchone()
        out.append({"provider": p.name, "configured": p.available(), "cooling_until": _cooling(con, p.name),
                    "used_this_month": used["used"] if used else 0, "free_allowance": p.monthly_free})
    return out
