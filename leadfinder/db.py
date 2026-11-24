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

-- A person's target group (ICP code) is per search: the same person can fit group "C" in one search and nothing in
-- another. people.icp/icp_locked are legacy (one value shared by every search) and are no longer read.
-- locked=1: set by a search that looked for exactly this group (LinkedIn search per group, manual add); the export
-- doesn't re-derive it from the title.
CREATE TABLE IF NOT EXISTS person_groups (
  person_id INTEGER, industry_id INTEGER, icp TEXT, locked INTEGER DEFAULT 0, PRIMARY KEY(person_id, industry_id));
CREATE INDEX IF NOT EXISTS ix_person_groups_ind ON person_groups(industry_id, icp);

-- Every claim about a company or person is backed by an evidence row with a URL.
-- kind: exhibitor | visitor_post | thanked_by_exhibitor | order | delegation | speaker | buyer_meet |
--       report_named | matchmaking | app_attendee | testimonial | profile_inferred | linkedin_profile |
--       visit_comment (commented about meeting at the show) | post_engaged (liked/commented on a show post: NOT
--       attendance) | customer_of_exhibitor (company named as a customer on an exhibitor's website)
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
    ("shows", "origin", "TEXT"),  # NULL/research = a core show of the search; evidence = a company's other show
]


def connect():
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    had_groups = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='person_groups'").fetchone()
    con.executescript(SCHEMA)
    if not had_groups:
        _migrate_person_groups(con)
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


def _migrate_person_groups(con):
    """One-off: copy the legacy shared people.icp into person_groups, for each search the person's company belongs
    to whose personas have that group code. A best guess (the shared value was overwritten by whichever search
    exported last); each search's next export re-derives its unlocked groups from titles."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(people)")}
    if "icp" not in cols:
        return
    # a lock (group set by a per-group LinkedIn search) doesn't say which search set it: keep it only when one
    # search is possible, otherwise let each search re-derive the group from the title
    con.execute("""WITH c AS (
                     SELECT p.id AS pid, ci.industry_id AS iid, p.icp AS icp, COALESCE(p.icp_locked, 0) AS lk
                     FROM people p JOIN company_industries ci ON ci.company_id=p.company_id
                     JOIN industries i ON i.id=ci.industry_id
                     WHERE p.icp IS NOT NULL AND i.icp_json IS NOT NULL AND json_valid(i.icp_json) AND EXISTS (
                       SELECT 1 FROM json_each(i.icp_json, '$.icps') j WHERE json_extract(j.value, '$.code')=p.icp))
                   INSERT OR IGNORE INTO person_groups(person_id, industry_id, icp, locked)
                   SELECT pid, iid, icp, CASE WHEN (SELECT COUNT(*) FROM c c2 WHERE c2.pid=c.pid)=1 THEN lk ELSE 0 END
                   FROM c""")


# SQL: append after "JOIN company_industries ci ON ci.company_id=p.company_id" to keep only people in a target group
# of that search.
IN_GROUP = "JOIN person_groups g ON g.person_id=p.id AND g.industry_id=ci.industry_id AND g.icp IS NOT NULL"


def set_group(con, person_id, industry_id, icp, locked=False):
    """Record a person's group in one search. A locked group is only replaced by another locked one."""
    if not person_id or not industry_id:
        return
    if locked:
        con.execute("""INSERT INTO person_groups(person_id, industry_id, icp, locked) VALUES (?,?,?,1)
                       ON CONFLICT(person_id, industry_id) DO UPDATE SET icp=excluded.icp, locked=1""",
                    (person_id, industry_id, icp))
    else:
        con.execute("""INSERT INTO person_groups(person_id, industry_id, icp, locked) VALUES (?,?,?,0)
                       ON CONFLICT(person_id, industry_id) DO UPDATE SET icp=excluded.icp WHERE locked=0""",
                    (person_id, industry_id, icp))


def listed_counts(con, industry_id):
    """The counts the results list shows for a search: people in one of its groups and not hidden by a flag
    (moved, left, outside the region), with emails among them; plus `people_all` including the hidden ones.
    Company-less people seen at a show are included, as in the list (build_leads records their groups too)."""
    r = con.execute("""SELECT COUNT(*) AS all_, SUM(COALESCE(p.profile_flag, '')='') AS shown,
                              SUM(COALESCE(p.profile_flag, '')='' AND p.email IS NOT NULL) AS emails
                       FROM person_groups g JOIN people p ON p.id=g.person_id
                       WHERE g.industry_id=? AND g.icp IS NOT NULL""", (industry_id,)).fetchone()
    return {"people": r[1] or 0, "emails": r[2] or 0, "people_all": r[0] or 0}


def groups_for(con, industry_id):
    """{person_id: (icp, locked)} for one search."""
    return {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT person_id, icp, locked FROM person_groups WHERE industry_id=?", (industry_id,))}


# ---------- industries ----------
def slugify(s):
    import re
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def delete_industry(con, ind):
    """Remove a search and everything that belongs only to it. Companies/people are shared between searches, so
    those are deleted only when no other search still uses their company. Returns counts of what was removed."""
    iid = ind["id"]
    cids = [r[0] for r in con.execute("SELECT company_id FROM company_industries WHERE industry_id=?", (iid,))]
    show_ids = [r[0] for r in con.execute("SELECT id FROM shows WHERE industry_id=?", (iid,))]
    n = {"people": 0, "companies": 0, "shows": len(show_ids)}

    def ids(sql, vals):
        return [r[0] for r in con.execute(sql, vals)]

    def chunks(xs, k=500):
        for i in range(0, len(xs), k):
            yield xs[i:i + k]

    con.execute("DELETE FROM company_industries WHERE industry_id=?", (iid,))
    con.execute("DELETE FROM person_groups WHERE industry_id=?", (iid,))
    con.execute("CREATE TABLE IF NOT EXISTS show_help (industry_id INTEGER, show_key TEXT, show_id INTEGER, "
                "status TEXT, note TEXT, at TEXT, PRIMARY KEY(industry_id, show_key))")
    con.execute("DELETE FROM show_help WHERE industry_id=?", (iid,))
    orphans = [c for ch in chunks(cids) for c in ids(
        f"SELECT id FROM companies WHERE id IN ({','.join('?' * len(ch))}) AND id NOT IN "
        "(SELECT company_id FROM company_industries)", ch)]
    for ch in chunks(orphans):
        q = ",".join("?" * len(ch))
        pids = ids(f"SELECT id FROM people WHERE company_id IN ({q})", ch)
        for pc in chunks(pids):
            pq = ",".join("?" * len(pc))
            con.execute(f"DELETE FROM tracker WHERE person_id IN ({pq})", pc)
            con.execute(f"DELETE FROM email_attempts WHERE person_id IN ({pq})", pc)
            con.execute(f"DELETE FROM evidence WHERE entity='person' AND entity_id IN ({pq})", pc)
            con.execute(f"DELETE FROM person_groups WHERE person_id IN ({pq})", pc)
        n["people"] += con.execute(f"DELETE FROM people WHERE company_id IN ({q})", ch).rowcount
        con.execute(f"DELETE FROM evidence WHERE entity='company' AND entity_id IN ({q})", ch)
        n["companies"] += con.execute(f"DELETE FROM companies WHERE id IN ({q})", ch).rowcount
    for ch in chunks(show_ids):
        q = ",".join("?" * len(ch))
        con.execute(f"DELETE FROM evidence WHERE show_id IN ({q})", ch)
        con.execute(f"DELETE FROM post_candidates WHERE show_id IN ({q})", ch)
    con.execute("DELETE FROM search_queue WHERE json_extract(context, '$.industry_id')=?", (iid,))
    con.execute("DELETE FROM shows WHERE industry_id=?", (iid,))
    con.execute("DELETE FROM pipeline_stages WHERE industry_id=?", (iid,))
    con.execute("DELETE FROM industries WHERE id=?", (iid,))
    return n


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
                  phone=None, icp=None, source=None, industry_id=None):
    """icp (a group code) is recorded, locked, for the search `industry_id`; it needs that search."""
    if icp and not industry_id:
        raise ValueError("upsert_person: a group (icp) needs the search it belongs to (industry_id)")
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
                             phone, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                          (company_id, name.strip(), nn, title, location, li, email, phone, source, now()))
        if icp:
            set_group(con, cur.lastrowid, industry_id, icp, locked=True)
        return cur.lastrowid
    pid = row["id"]
    if company_id and row["company_id"] is None:
        # found by LinkedIn URL without a company, but the same name already exists at that company: that record is
        # the person at the company (giving this one the company too would clash on company+name)
        other = con.execute("SELECT * FROM people WHERE company_id=? AND norm_name=? AND id!=?",
                            (company_id, nn, pid)).fetchone()
        if other:
            row, pid = other, other["id"]
            li = li if not other["linkedin_url"] else None
            if li and con.execute("SELECT 1 FROM people WHERE linkedin_url=?", (li,)).fetchone():
                li = None   # the URL stays on the company-less record it was found on
    con.execute("""UPDATE people SET title=COALESCE(title, ?), location=COALESCE(location, ?),
                   linkedin_url=COALESCE(linkedin_url, ?), email=COALESCE(email, ?), phone=COALESCE(phone, ?),
                   company_id=COALESCE(company_id, ?) WHERE id=?""",
                (title, location, li, email, phone, company_id, pid))
    if icp:
        set_group(con, pid, industry_id, icp, locked=True)
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
