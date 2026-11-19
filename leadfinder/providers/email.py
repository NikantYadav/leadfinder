"""Email-finder waterfall across free tiers. Each provider was written against its docs as read on 2026-11-18:
  Prospeo   POST api.prospeo.io/enrich-person (X-KEY)          - charged only when an email is found
  Hunter    GET  api.hunter.io/v2/email-finder (api_key)        - free plan includes API
  Apollo    POST api.apollo.io/api/v1/people/match (x-api-key)  - free accounts need a work-email signup
  Tomba     GET  api.tomba.io/v1/email-finder | /v1/linkedin    - 25 searches / 30 days free
  BetterContact POST app.bettercontact.rocks/api/v2/async (X-API-Key) - async, 50 free credits once
Snov.io is NOT included: its free trial has no API access.

Every provider returns None (no result) or a dict:
  {email, status: verified|catch_all|unknown, score, source, extra: {country, city, title, company, domain}}
and raises ProviderBusy when out of credits / rate limited (the waterfall then moves on).
"""
import time

import requests

from .. import config, keys
from .search import ProviderBusy, ProviderError

STATUS_MAP = {
    "verified": "verified", "valid": "verified", "deliverable": "verified",
    "accept_all": "catch_all", "catch_all": "catch_all", "catch_all_safe": "catch_all",
    "catch_all_not_safe": "catch_all", "risky": "catch_all",
    "unknown": "unknown", "unverified": "unknown", "extrapolated": "unknown", "guessed": "unknown",
    "invalid": "invalid", "undeliverable": "invalid", "not_found": "invalid", "disposable": "invalid",
}


def norm_status(s):
    return STATUS_MAP.get(str(s or "").lower(), "unknown")


def _split(name):
    parts = (name or "").split()
    return (parts[0], " ".join(parts[1:])) if len(parts) > 1 else (name, "")


def _key(provider, key=None):
    """The key to use: the one the caller picked (rotation), else the first configured one."""
    if key is not None:
        return key
    ks = keys.all_keys(provider.name)
    if not ks:
        raise ProviderBusy(f"{provider.name}: no key set")
    return ks[0]


def _num(v):
    try:
        f = float(v)
        return int(f) if f.is_integer() else round(f, 2)
    except (TypeError, ValueError):
        return v


def _json(r):
    try:
        return r.json()
    except ValueError:
        return {}


class Prospeo:
    name, key_env, free_note = "prospeo", "PROSPEO_API_KEY", "~75-100 credits/month"

    def available(self):
        return keys.available(self.name)

    def find(self, p, key=None):
        data = {}
        if p.get("linkedin_url"):
            data["linkedin_url"] = p["linkedin_url"]
        data["full_name"] = p["name"]
        if p.get("domain"):
            data["company_website"] = p["domain"]
        if p.get("company"):
            data["company_name"] = p["company"]
        r = requests.post("https://api.prospeo.io/enrich-person", timeout=60,
                          headers={"X-KEY": _key(self, key).value, "Content-Type": "application/json"},
                          json={"only_verified_email": True, "data": data})
        d = _json(r)
        code = d.get("error_code") or ""
        if r.status_code == 429 or code in ("INSUFFICIENT_CREDITS", "RATE_LIMITED"):
            raise ProviderBusy(code or "429")
        if code in ("NO_MATCH", "INVALID_DATAPOINTS"):
            return None
        if not r.ok or d.get("error"):
            raise ProviderError(f"HTTP {r.status_code} {code or r.text[:150]}")
        person, company = d.get("person") or {}, d.get("company") or {}
        em = person.get("email") or {}
        email = em.get("email") if isinstance(em, dict) else em
        if not email or "*" in email:
            return None
        loc = person.get("location") or {}
        return {"email": email, "status": norm_status(em.get("status")), "score": None, "source": self.name,
                "extra": {"country": loc.get("country"), "city": loc.get("city"),
                          "title": person.get("current_job_title"), "company": company.get("name"),
                          "domain": company.get("domain")}}

    def balance(self, key=None):
        r = requests.get("https://api.prospeo.io/account-information", timeout=20,
                         headers={"X-KEY": _key(self, key).value})
        d = _json(r).get("response") or {}
        if not r.ok or "remaining_credits" not in d:
            return {"error": f"HTTP {r.status_code}"}
        return {"remaining": d["remaining_credits"], "unit": "credits",
                "note": f"renews {str(d.get('next_quota_renewal_date') or '')[:10]}".strip()}


class Hunter:
    name, key_env, free_note = "hunter", "HUNTER_API_KEY", "free plan monthly searches (see balance)"

    def available(self):
        return keys.available(self.name)

    def find(self, p, key=None):
        first, last = _split(p["name"])
        params = {"api_key": _key(self, key).value, "first_name": first, "last_name": last}
        if p.get("domain") and p.get("domain_trusted", True):
            params["domain"] = p["domain"]
        elif p.get("company"):
            params["company"] = p["company"]
        if p.get("linkedin_url"):
            params["linkedin_handle"] = p["linkedin_url"]
        r = requests.get("https://api.hunter.io/v2/email-finder", params=params, timeout=60)
        if r.status_code in (429, 403):  # 429 = usage limit reached, 403 = rate limit (per Hunter docs)
            raise ProviderBusy(f"HTTP {r.status_code}")
        if r.status_code in (404, 451):
            return None
        if not r.ok:
            raise ProviderError(f"HTTP {r.status_code} {r.text[:150]}")
        d = _json(r).get("data") or {}
        if not d.get("email"):
            return None
        status = (d.get("verification") or {}).get("status")
        if not status and d.get("accept_all"):
            status = "accept_all"
        return {"email": d["email"], "status": norm_status(status), "score": d.get("score"), "source": self.name,
                "extra": {"title": d.get("position"), "company": d.get("company"), "domain": d.get("domain")}}

    def balance(self, key=None):
        r = requests.get("https://api.hunter.io/v2/account", params={"api_key": _key(self, key).value}, timeout=20)
        d = _json(r).get("data") or {}
        req = d.get("requests") or {}
        b = req.get("credits") or req.get("searches") or {}
        if not r.ok or "available" not in b:
            return {"error": f"HTTP {r.status_code}"}
        return {"remaining": b["available"] - b.get("used", 0), "unit": "credits",
                "note": f"resets {d.get('reset_date') or ''}".strip()}

    def domain_for(self, company, key=None):
        """Free endpoint (does not consume credits)."""
        r = requests.get("https://api.hunter.io/v2/domain-finder", timeout=30,
                         params={"company": company, "api_key": _key(self, key).value})
        if r.status_code in (429, 403):
            raise ProviderBusy(f"HTTP {r.status_code}")
        rows = _json(r).get("data") or []
        return rows[0].get("domain") if rows else None


class Apollo:
    name, key_env, free_note = "apollo", "APOLLO_API_KEY", "free plan credits (work-email signup)"

    def available(self):
        return keys.available(self.name)

    def find(self, p, key=None):
        first, last = _split(p["name"])
        params = {"first_name": first, "last_name": last}
        if p.get("company"):
            params["organization_name"] = p["company"]
        if p.get("domain") and p.get("domain_trusted", True):
            params["domain"] = p["domain"]
        if p.get("linkedin_url"):
            params["linkedin_url"] = p["linkedin_url"]
        r = requests.post("https://api.apollo.io/api/v1/people/match", params=params, timeout=60,
                          headers={"x-api-key": _key(self, key).value, "Content-Type": "application/json"})
        if r.status_code == 429:
            raise ProviderBusy("429")
        if r.status_code in (402, 403):
            raise ProviderBusy(f"HTTP {r.status_code} (plan/credits)")
        if not r.ok:
            raise ProviderError(f"HTTP {r.status_code} {r.text[:150]}")
        person = _json(r).get("person") or {}
        if not person.get("email") or person.get("match_confidence") == "none":
            return None
        org = person.get("organization") or {}
        return {"email": person["email"], "status": norm_status(person.get("email_status")), "score": None,
                "source": self.name,
                "extra": {"country": person.get("country"), "city": person.get("city"), "title": person.get("title"),
                          "company": org.get("name"), "domain": org.get("primary_domain")}}

    def balance(self, key=None):
        # Apollo exposes no credit balance over the API; usage_stats gives the per-endpoint rate limit only.
        r = requests.post("https://api.apollo.io/api/v1/usage_stats/api_usage_stats", timeout=20,
                          headers={"x-api-key": _key(self, key).value, "Content-Type": "application/json"})
        d = _json(r)
        if not r.ok or not isinstance(d, dict):
            return {"error": f"HTTP {r.status_code}"}
        day = next((v.get("day") or {} for k, v in d.items() if "people" in k and "match" in k), {})
        if "left_over" not in day:
            return {"error": "no people/match limit in usage stats"}
        return {"remaining": day["left_over"], "unit": "matches left today",
                "note": f"rate limit of {day.get('limit')}/day, not credits; credits are only shown in Apollo"}


class Tomba:
    name, key_env, free_note = "tomba", "TOMBA_API_KEY", "25 searches / 30 days"

    def available(self):
        return keys.available(self.name)

    def _h(self, key=None):
        k = _key(self, key)
        return {"X-Tomba-Key": k.value, "X-Tomba-Secret": k.secret}

    def find(self, p, key=None):
        if p.get("domain") or p.get("company"):
            first, last = _split(p["name"])
            params = {"first_name": first, "last_name": last}
            use_domain = p.get("domain") and (p.get("domain_trusted", True) or not p.get("company"))
            params["domain" if use_domain else "company"] = p["domain"] if use_domain else p["company"][:75]
            r = requests.get("https://api.tomba.io/v1/email-finder", params=params, headers=self._h(key), timeout=60)
        elif p.get("linkedin_url"):
            r = requests.get("https://api.tomba.io/v1/linkedin", params={"url": p["linkedin_url"]},
                             headers=self._h(key), timeout=60)
        else:
            return None
        if r.status_code in (402, 429):
            raise ProviderBusy(f"HTTP {r.status_code}")
        if r.status_code in (400, 404, 422):
            return None
        if not r.ok:
            raise ProviderError(f"HTTP {r.status_code} {r.text[:150]}")
        d = _json(r).get("data") or {}
        if not d.get("email"):
            return None
        return {"email": d["email"], "status": norm_status((d.get("verification") or {}).get("status")),
                "score": d.get("score"), "source": self.name,
                "extra": {"country": d.get("country"), "title": d.get("position"), "company": d.get("company"),
                          "domain": d.get("website_url")}}

    def balance(self, key=None):
        r = requests.get("https://api.tomba.io/v1/me", headers=self._h(key), timeout=20)
        dom = ((_json(r).get("data") or {}).get("requests") or {}).get("domains") or {}
        if "available" in dom and "used" in dom:
            return {"remaining": dom["available"] - dom["used"], "unit": "searches"}
        return {"error": f"HTTP {r.status_code}"}


class BetterContact:
    name, key_env, free_note = "bettercontact", "BETTERCONTACT_API_KEY", "50 credits once (itself a waterfall)"
    base = "https://app.bettercontact.rocks/api/v2"

    def available(self):
        return keys.available(self.name)

    def find(self, p, key=None, poll_seconds=180):
        if not p.get("domain"):
            return None  # its schema keys on first/last name + company_domain
        first, last = _split(p["name"])
        row = {"first_name": first, "last_name": last, "company_domain": p["domain"]}
        if p.get("linkedin_url"):
            row["linkedin_url"] = p["linkedin_url"]
        h = {"X-API-Key": _key(self, key).value, "Content-Type": "application/json"}  # same key for the poll
        r = requests.post(f"{self.base}/async", headers=h, timeout=60,
                          json={"enrich_email_address": True, "enrich_phone_number": False, "data": [row]})
        if r.status_code in (402, 429):
            raise ProviderBusy(f"HTTP {r.status_code}")
        if not r.ok:
            raise ProviderError(f"HTTP {r.status_code} {r.text[:150]}")
        rid = _json(r).get("id")
        deadline = time.time() + poll_seconds
        while time.time() < deadline:
            time.sleep(5)
            g = _json(requests.get(f"{self.base}/async/{rid}", headers=h, timeout=60))
            st = g.get("status")
            if st == "on_hold":
                raise ProviderBusy("on_hold (out of credits)")
            if st == "terminated":
                rows = g.get("data") or []
                if not rows or not rows[0].get("contact_email_address"):
                    return None
                return {"email": rows[0]["contact_email_address"],
                        "status": norm_status(rows[0].get("contact_email_address_status")), "score": None,
                        "source": self.name, "extra": {}}
        raise ProviderError(f"bettercontact: request {rid} not finished in {poll_seconds}s")

    def balance(self, key=None):
        r = requests.get(f"{self.base}/account", headers={"X-API-Key": _key(self, key).value}, timeout=20)
        left = _json(r).get("credits_left")
        if left is None:
            return {"error": f"HTTP {r.status_code}"}
        return {"remaining": _num(left), "unit": "credits", "note": "one-time grant"}


# Waterfall order: biggest renewing free allowance + "charged only on success" first; BetterContact last
# (one-time credits, slow async).
PROVIDERS = [Prospeo(), Hunter(), Apollo(), Tomba(), BetterContact()]
