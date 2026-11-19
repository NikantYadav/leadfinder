"""SQLite storage. One database for all industries, so companies/people are never duplicated across runs."""
import json
import sqlite3
from datetime import datetime, timezone

from . import config
from .normalize import norm_company, norm_domain, norm_person

SCHEMA = """
CREATE TABLE IF NOT EXISTS industries (
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE, name TEXT, geo TEXT, icp_json TEXT,
  icp_approved INTEGER DEFAULT 0, created_at TEXT);

CREATE TABLE IF NOT EXISTS shows (
  id INTEGER PRIMARY KEY, industry_id INTEGER, name TEXT, series TEXT, year INTEGER,
  start_date TEXT, end_date TEXT, city TEXT, country TEXT, url TEXT, exhibitor_list_url TEXT,
  source_url TEXT, notes TEXT,
  UNIQUE(industry_id, name, year));

CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY, name TEXT, norm_key TEXT UNIQUE, domain TEXT, country TEXT, city TEXT,
  sector TEXT, notes TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_companies_domain ON companies(domain);

CREATE TABLE IF NOT EXISTS company_industries (
  company_id INTEGER, industry_id INTEGER, PRIMARY KEY(company_id, industry_id));

CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY, company_id INTEGER, name TEXT, norm_name TEXT, title TEXT, location TEXT,
  linkedin_url TEXT, email TEXT, phone TEXT, icp TEXT, icp_locked INTEGER DEFAULT 0,
  source TEXT, created_at TEXT,
  UNIQUE(company_id, norm_name));
CREATE UNIQUE INDEX IF NOT EXISTS ux_people_li ON people(linkedin_url) WHERE linkedin_url IS NOT NULL;

-- Every claim about a company or person is backed by an evidence row with a URL.
-- kind: exhibitor | visitor_post | thanked_by_exhibitor | order | delegation | speaker | buyer_meet |
--       report_named | matchmaking | app_attendee | testimonial | profile_inferred | linkedin_profile
-- confidence: confirmed | likely | inferred
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY, entity TEXT, entity_id INTEGER, show_id INTEGER, kind TEXT,
  confidence TEXT, url TEXT, snippet TEXT, found_at TEXT,
  UNIQUE(entity, entity_id, show_id, kind, url));

CREATE TABLE IF NOT EXISTS search_cache (
  provider TEXT, query TEXT, params TEXT, response TEXT, at TEXT, PRIMARY KEY(query, params));

CREATE TABLE IF NOT EXISTS search_queue (
  id INTEGER PRIMARY KEY, purpose TEXT, query TEXT, params TEXT, context TEXT,
  status TEXT DEFAULT 'pending', provider TEXT, attempts INTEGER DEFAULT 0, last_error TEXT,
  created_at TEXT, done_at TEXT, UNIQUE(purpose, query, params));

CREATE TABLE IF NOT EXISTS post_candidates (
  id INTEGER PRIMARY KEY, show_id INTEGER, url TEXT, author TEXT, title TEXT, snippet TEXT,
  status TEXT DEFAULT 'new', at TEXT, UNIQUE(show_id, url));

CREATE TABLE IF NOT EXISTS provider_state (
  provider TEXT PRIMARY KEY, cooldown_until TEXT, last_used TEXT);

CREATE TABLE IF NOT EXISTS credit_usage (
  provider TEXT, month TEXT, used INTEGER DEFAULT 0, PRIMARY KEY(provider, month));

CREATE TABLE IF NOT EXISTS fetch_log (
  url TEXT PRIMARY KEY, path TEXT, method TEXT, status TEXT, chars INTEGER, at TEXT);

CREATE TABLE IF NOT EXISTS tracker (
  person_id INTEGER PRIMARY KEY, status TEXT, channel TEXT, last_contacted TEXT, interview_date TEXT,
  pain_confirmed TEXT, notes TEXT, updated_at TEXT);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


MIGRATIONS = [  # (table, column, type) added after the first release
    ("people", "email_status", "TEXT"), ("people", "email_source", "TEXT"), ("people", "country", "TEXT"),
    ("people", "li_company", "TEXT"), ("people", "li_checked_at", "TEXT"), ("people", "profile_flag", "TEXT"),
    ("companies", "domain_source", "TEXT"), ("companies", "linkedin_url", "TEXT"),
    ("industries", "persona_text", "TEXT"), ("industries", "options_json", "TEXT"),
    ("post_candidates", "author_headline", "TEXT"), ("post_candidates", "author_url", "TEXT"),
    ("post_candidates", "posted_at", "TEXT"),
]


def connect():
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS pipeline_stages (
                   industry_id INTEGER, stage TEXT, status TEXT, detail TEXT, started_at TEXT, ended_at TEXT,
                   PRIMARY KEY(industry_id, stage))""")
    con.execute("""CREATE TABLE IF NOT EXISTS email_attempts (
                   person_id INTEGER, provider TEXT, result TEXT, at TEXT, PRIMARY KEY(person_id, provider))""")
    con.commit()
    return con


# ---------- industries ----------
def slugify(s):
    import re
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def get_industry(con, slug_or_id):
    row = con.execute("SELECT * FROM industries WHERE slug=? OR id=?", (str(slug_or_id), slug_or_id)).fetchone()
    if not row:
        raise SystemExit(f"Unknown industry '{slug_or_id}'. Create it with: lf init-industry --name ...")
    return row


# ---------- companies ----------
def upsert_company(con, name, domain=None, country=None, city=None, sector=None, industry_id=None):
    """Find an existing company by domain, then by normalised name; create it otherwise."""
    dom = norm_domain(domain) if domain else ""
    key = norm_company(name)
    if not key and not dom:
        return None
    row = None
    if dom:
        row = con.execute("SELECT * FROM companies WHERE domain=?", (dom,)).fetchone()
    if row is None and key:
        row = con.execute("SELECT * FROM companies WHERE norm_key=?", (key,)).fetchone()
    if row is None:
        cur = con.execute(
            "INSERT INTO companies(name, norm_key, domain, country, city, sector, created_at) VALUES (?,?,?,?,?,?,?)",
            (name.strip(), key or dom, dom or None, country, city, sector, now()))
        cid = cur.lastrowid
    else:
        cid = row["id"]
        # fill blanks only; never overwrite what is already known
        con.execute("""UPDATE companies SET domain=COALESCE(domain, ?), country=COALESCE(country, ?),
                       city=COALESCE(city, ?), sector=COALESCE(sector, ?) WHERE id=?""",
                    (dom or None, country, city, sector, cid))
    if industry_id:
        con.execute("INSERT OR IGNORE INTO company_industries VALUES (?,?)", (cid, industry_id))
    return cid


# ---------- people ----------
def upsert_person(con, company_id, name, title=None, location=None, linkedin_url=None, email=None,
                  phone=None, icp=None, source=None):
    nn = norm_person(name)
    if not nn:
        return None
    li = _clean_li(linkedin_url)
    row = None
    if li:
        row = con.execute("SELECT * FROM people WHERE linkedin_url=?", (li,)).fetchone()
    if row is None:
        row = con.execute("SELECT * FROM people WHERE company_id IS ? AND norm_name=?", (company_id, nn)).fetchone()
    if row is None:
        cur = con.execute("""INSERT INTO people(company_id, name, norm_name, title, location, linkedin_url, email,
                             phone, icp, icp_locked, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (company_id, name.strip(), nn, title, location, li, email, phone, icp,
                           1 if icp else 0, source, now()))
        return cur.lastrowid
    pid = row["id"]
    con.execute("""UPDATE people SET title=COALESCE(title, ?), location=COALESCE(location, ?),
                   linkedin_url=COALESCE(linkedin_url, ?), email=COALESCE(email, ?), phone=COALESCE(phone, ?),
                   company_id=COALESCE(company_id, ?) WHERE id=?""",
                (title, location, li, email, phone, company_id, pid))
    if icp:
        con.execute("UPDATE people SET icp=?, icp_locked=1 WHERE id=?", (icp, pid))
    return pid


def _clean_li(url):
    if not url or "linkedin.com/in/" not in url:
        return None
    slug = url.split("linkedin.com/in/", 1)[1].split("?")[0].split("#")[0].strip("/")
    return f"https://www.linkedin.com/in/{slug}" if slug else None


def add_evidence(con, entity, entity_id, kind, confidence, url, snippet=None, show_id=None):
    if entity_id is None:
        return
    if confidence not in ("confirmed", "likely", "inferred"):
        raise ValueError(f"confidence must be confirmed|likely|inferred, got {confidence!r}")
    if not url:
        raise ValueError("every evidence row needs a source URL")
    con.execute("""INSERT OR IGNORE INTO evidence(entity, entity_id, show_id, kind, confidence, url, snippet, found_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (entity, entity_id, show_id, kind, confidence, url, (snippet or "")[:500], now()))


DEFAULT_SETTINGS = {}


def get_setting(con, key):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] else DEFAULT_SETTINGS.get(key, "")


def set_setting(con, key, value):
    con.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
    con.commit()


def to_json(rows):
    return json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False)
