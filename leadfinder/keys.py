"""API key pools: several keys per service, rotated when one runs out or is rate limited.

In .env, put several keys in one variable separated by commas, or use numbered variables:
    SERPER_API_KEY=key_one,key_two
    SERPER_API_KEY_2=key_three
Tomba has a second value paired by position: TOMBA_API_KEY + TOMBA_API_SECRET
(the n-th key goes with the n-th secret).

Each key gets its own state, stored under "key:<service>:<hash>" in provider_state / credit_usage. The UI shows
only the last 4 characters. Full keys never leave this process: not in the UI, not in logs.
"""
import hashlib
import re
import threading
from datetime import datetime, timedelta, timezone

from . import config, db

# service -> (main env var, optional paired env var)
SPECS = {
    "prospeo": ("PROSPEO_API_KEY", None),
    "hunter": ("HUNTER_API_KEY", None),
    "apollo": ("APOLLO_API_KEY", None),
    "tomba": ("TOMBA_API_KEY", "TOMBA_API_SECRET"),
    "bettercontact": ("BETTERCONTACT_API_KEY", None),
    "serper": ("SERPER_API_KEY", None),
    "tinyfish": ("TINYFISH_API_KEY", None),
    "firecrawl": ("FIRECRAWL_API_KEY", None),
    "apify": ("APIFY_TOKEN", None),
    "brightdata": ("BRIGHTDATA_API_KEY", None),
    "ai_ark": ("AI_ARK_API_KEY", None),
}
QUOTA_HOURS = 24          # cooldown when a key is out of credits
RATE_LIMIT_HINTS = ("429", "rate", "RATE_LIMITED", "per minute", "too many")


class Key:
    def __init__(self, service, index, value, secret=None):
        self.service, self.index, self.value, self.secret = service, index, value, secret
        self.fp = "…" + value[-4:] if len(value) >= 8 else "…" + value[-2:]
        # state is keyed by a hash of the whole key, so two keys ending in the same 4 characters never collide
        self.state_id = f"key:{service}:{hashlib.sha256(value.encode()).hexdigest()[:12]}"

    def __repr__(self):  # never print the key itself
        return f"<{self.service} key {self.index} {self.fp}>"


def _split(v):
    return [x.strip().strip('"').strip("'") for x in re.split(r"[,\s]+", v or "") if x.strip()]


def _values(var):
    out = _split(config.get(var, ""))
    for n in range(2, 21):
        out += _split(config.get(f"{var}_{n}", ""))
    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def all_keys(service):
    main, pair = SPECS[service]
    vals = _values(main)
    if not pair:
        return [Key(service, i + 1, v) for i, v in enumerate(vals)]
    secrets = _values(pair)
    # a single secret/cx is shared by every key; otherwise pair by position and drop keys without a partner
    return [Key(service, i + 1, v, secrets[0] if len(secrets) == 1 else secrets[i])
            for i, v in enumerate(vals) if secrets and (len(secrets) == 1 or i < len(secrets))]


def configured(service):
    return bool(all_keys(service))


_local = threading.local()


def _con(con=None):
    if con is not None:
        return con
    cached = getattr(_local, "con", None)
    if cached is None or getattr(_local, "path", None) != config.DB_PATH:
        _local.con, _local.path = db.connect(), config.DB_PATH
    return _local.con


def cooling_until(key, con=None):
    row = _con(con).execute("SELECT cooldown_until FROM provider_state WHERE provider=?", (key.state_id,)).fetchone()
    return row["cooldown_until"] if row and row["cooldown_until"] and row["cooldown_until"] > db.now() else None


def usable(service, con=None):
    c = _con(con)
    return [k for k in all_keys(service) if not cooling_until(k, c)]


def available(service):
    return bool(usable(service))


def is_rate_limit(err):
    s = str(err)
    return any(h.lower() in s.lower() for h in RATE_LIMIT_HINTS)


def cool(key, err, con=None):
    c = _con(con)
    if is_rate_limit(err):
        until = datetime.now(timezone.utc) + timedelta(minutes=config.COOLDOWN_MINUTES)
    else:
        until = datetime.now(timezone.utc) + timedelta(hours=QUOTA_HOURS)
    c.execute("""INSERT INTO provider_state(provider, cooldown_until, last_used) VALUES (?,?,?)
                 ON CONFLICT(provider) DO UPDATE SET cooldown_until=excluded.cooldown_until""",
              (key.state_id, until.strftime("%Y-%m-%dT%H:%M:%SZ"), db.now()))
    c.commit()


def count(key, n=1, con=None, extra_bucket=None):
    """Record use of a key for this month and for today (daily limits, e.g. TinyFish, need the day bucket)."""
    c = _con(con)
    now = datetime.now(timezone.utc)
    for bucket in (now.strftime("%Y-%m"), now.strftime("%Y-%m-%d"), *([extra_bucket] if extra_bucket else [])):
        c.execute("""INSERT INTO credit_usage(provider, month, used) VALUES (?,?,?)
                     ON CONFLICT(provider, month) DO UPDATE SET used=used+?""", (key.state_id, bucket, n, n))
    c.commit()


def used(key, bucket, con=None):
    row = _con(con).execute("SELECT used FROM credit_usage WHERE provider=? AND month=?",
                            (key.state_id, bucket)).fetchone()
    return row["used"] if row else 0


def run(service, fn, con=None, log=None, only=None):
    """Call fn(key) with each usable key in turn. A key that raises ProviderBusy (out of credits / rate limited)
    is rested and the same request is retried with the next key. Raises ProviderBusy when every key is busy.
    only: optional predicate to restrict which keys may be used (e.g. Apify tokens with credit left)."""
    from .providers.search import ProviderBusy
    if not service or service not in SPECS:  # a provider without API keys (OpenSERP, test fakes)
        return fn(None)
    keys = [k for k in usable(service, con) if not only or only(k)]
    if not keys:
        n = len(all_keys(service))
        raise ProviderBusy(f"{service}: no key set" if not n else f"{service}: all {n} key(s) resting")
    last = None
    for k in keys:
        try:
            return fn(k)
        except ProviderBusy as e:
            last = e
            cool(k, e, con)
            if log:
                log(f"  {service} key {k.fp} busy ({str(e)[:80]}), trying the next key")
    raise ProviderBusy(f"{service}: all keys busy ({last})")
