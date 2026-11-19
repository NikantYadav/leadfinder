"""Excel export: output/leads_<industry>.xlsx (stable name, so the Tracker you edit survives re-runs).

Before every export the existing file's Tracker sheet is read back into the database, then a backup of the
old file is kept in output/backups/. Your status/notes edits are never lost.
"""
import json
import shutil
from datetime import date, datetime
from urllib.parse import quote_plus

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from . import config, db
from .people import company_facts, company_rule_ok, load_icps
from .scoring import score_person

STATUSES = ["new", "contacted", "replied", "interview booked", "interviewed", "not interested", "wrong person"]
TRACKER_COLS = ["lead_id", "name", "company", "icp", "status", "channel", "last_contacted", "interview_date",
                "pain_confirmed", "notes"]


def path_for(industry):
    return config.OUTPUT / f"leads_{industry['slug']}.xlsx"


def sync_tracker(con, industry):
    """Read the Tracker sheet of the existing workbook back into the DB. Returns rows synced."""
    p = path_for(industry)
    if not p.exists():
        return 0
    wb = load_workbook(p, read_only=True, data_only=True)
    if "Tracker" not in wb.sheetnames:
        return 0
    rows = list(wb["Tracker"].iter_rows(values_only=True))
    if not rows:
        return 0
    head = [str(h or "").strip() for h in rows[0]]
    # The file's last-modified time decides conflicts: a row edited in the web UI after the file was saved wins.
    file_time = datetime.utcfromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = 0
    for r in rows[1:]:
        rec = dict(zip(head, r))
        if not rec.get("lead_id"):
            continue
        cur = con.execute("SELECT updated_at FROM tracker WHERE person_id=?", (int(rec["lead_id"]),)).fetchone()
        if cur and cur["updated_at"] and cur["updated_at"] > file_time:
            continue
        vals = {k: (rec.get(k).isoformat()[:10] if isinstance(rec.get(k), (date, datetime)) else rec.get(k))
                for k in TRACKER_COLS[4:]}
        con.execute("""INSERT INTO tracker(person_id, status, channel, last_contacted, interview_date, pain_confirmed,
                       notes, updated_at) VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(person_id) DO UPDATE SET status=excluded.status, channel=excluded.channel,
                       last_contacted=excluded.last_contacted, interview_date=excluded.interview_date,
                       pain_confirmed=excluded.pain_confirmed, notes=excluded.notes, updated_at=excluded.updated_at""",
                    (int(rec["lead_id"]), *[vals[k] for k in TRACKER_COLS[4:]], file_time))
        n += 1
    con.commit()
    return n


def build_leads(con, industry, min_shows=None):
    from .people import effective_min_shows
    min_shows = min_shows or effective_min_shows(industry)
    icps = load_icps(industry)
    people = con.execute("""SELECT p.*, c.name AS company, c.domain FROM people p
                            JOIN companies c ON c.id=p.company_id
                            JOIN company_industries ci ON ci.company_id=c.id AND ci.industry_id=?""",
                         (industry["id"],)).fetchall()
    from .people import classify
    leads = []
    facts_cache = {}
    for p in people:
        if not p["icp_locked"]:
            icp = classify(con, icps, p["title"], p["company_id"], min_shows)
            if icp != p["icp"]:
                con.execute("UPDATE people SET icp=? WHERE id=?", (icp, p["id"]))
            p = dict(p, icp=icp)
        else:
            p = dict(p)
        if not p["icp"]:
            continue  # title doesn't fit any ICP
        pev = con.execute("SELECT * FROM evidence WHERE entity='person' AND entity_id=?", (p["id"],)).fetchall()
        cev = con.execute("SELECT * FROM evidence WHERE entity='company' AND entity_id=?", (p["company_id"],)).fetchall()
        if p["company_id"] not in facts_cache:
            facts_cache[p["company_id"]] = company_facts(con, p["company_id"])
        f = facts_cache[p["company_id"]]
        score, conf, why = score_person(p, pev, cev, f["exhibitor_window_count"], min_shows)
        ev = sorted(pev + cev, key=lambda e: {"confirmed": 0, "likely": 1, "inferred": 2}[e["confidence"]])
        att = [e for e in ev if e["kind"] != "linkedin_profile"]
        best = (att or ev or [None])[0]
        shows = _show_names(con, [e["show_id"] for e in pev + cev if e["show_id"]])
        leads.append({**p, "score": score, "confidence": conf, "why": why, "shows": shows,
                      "evidence_url": best["url"] if best else "", "evidence_kind": best["kind"] if best else "",
                      "show_ids": sorted({e["show_id"] for e in pev + cev if e["show_id"]})})
    con.commit()
    leads.sort(key=lambda x: -x["score"])
    return leads


def _show_names(con, ids):
    ids = sorted(set(ids))
    if not ids:
        return ""
    rows = con.execute(f"SELECT name FROM shows WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return ", ".join(r["name"] for r in rows)


def _sheet(wb, title, header, rows, widths=None):
    ws = wb.create_sheet(title)
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
    for r in rows:
        ws.append(r)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, h in enumerate(header, 1):
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(h, min(max(len(h) + 4, 12), 40))
    return ws


def _link(ws, col_idx):
    for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
        c = row[0]
        if isinstance(c.value, str) and c.value.startswith("http"):
            c.hyperlink = c.value
            c.font = Font(color="0563C1", underline="single")


def export(con, industry, min_shows=None):
    from .people import effective_min_shows
    min_shows = min_shows or effective_min_shows(industry)
    synced = sync_tracker(con, industry)
    p = path_for(industry)
    if p.exists():
        (config.OUTPUT / "backups").mkdir(exist_ok=True)
        shutil.copy2(p, config.OUTPUT / "backups" / f"{p.stem}_{datetime.now():%Y%m%d-%H%M%S}.xlsx")

    leads = build_leads(con, industry, min_shows)
    wb = Workbook()
    wb.remove(wb.active)

    # Leads
    head = ["lead_id", "icp", "score", "confidence", "name", "title", "company", "location", "country", "check",
            "linkedin", "email", "email_status", "email_source", "shows", "evidence_kind", "evidence_url", "why",
            "google_check"]
    rows = [[l["id"], l["icp"], l["score"], l["confidence"], l["name"], l["title"], l["company"], l["location"],
             l.get("country") or "", l.get("profile_flag") or ("ok" if l.get("li_checked_at") else ""),
             l["linkedin_url"] or "", l["email"] or "", l.get("email_status") or "", l.get("email_source") or "",
             l["shows"], l["evidence_kind"], l["evidence_url"], l["why"],
             "https://www.google.com/search?q=" + quote_plus(f'"{l["name"]}" "{l["company"]}"')]
            for l in leads]
    ws = _sheet(wb, "Leads", head, rows, {"title": 34, "why": 60, "evidence_url": 40, "linkedin": 36, "shows": 30,
                                          "company": 28, "email": 30, "check": 26})
    for name in ("linkedin", "evidence_url", "google_check"):
        _link(ws, head.index(name) + 1)

    # Companies
    comps = con.execute("""SELECT c.* FROM companies c JOIN company_industries ci ON ci.company_id=c.id
                           WHERE ci.industry_id=?""", (industry["id"],)).fetchall()
    icps = load_icps(industry)
    crow = []
    for c in comps:
        f = company_facts(con, c["id"])
        n_people = con.execute("SELECT COUNT(*) FROM people WHERE company_id=? AND icp IS NOT NULL", (c["id"],)).fetchone()[0]
        kinds = con.execute("SELECT DISTINCT kind FROM evidence WHERE entity='company' AND entity_id=?", (c["id"],)).fetchall()
        kw = " OR ".join(f'"{k}"' for i in icps for k in (i.get("search_keywords") or i["title_keywords"])[:2])
        fit = [i["code"] for i in icps if i.get("find_via", "company_list") == "company_list"
               and company_rule_ok(i.get("company_rule"), f, min_shows)]
        crow.append([c["id"], c["name"], ",".join(fit), c["domain"] or "", c["country"] or "", c["city"] or "",
                     f["exhibitor_window_count"], ", ".join(s["name"] for s in f["exhibitor_shows"]),
                     f["visitor_evidence"], ", ".join(sorted(k["kind"] for k in kinds)), n_people,
                     "https://www.google.com/search?q=" + quote_plus(f'site:linkedin.com/in "{c["name"]}" ({kw})'),
                     "https://www.linkedin.com/search/results/people/?keywords=" + quote_plus(c["name"]),
                     c["linkedin_url"] or ""])
    crow.sort(key=lambda r: (not r[2], -r[6], -r[8], r[1]))  # ICP-fit companies first
    ws = _sheet(wb, "Companies", ["company_id", "company", "icp_fit", "domain", "country", "city", "shows_in_12mo",
                                  "exhibitor_at", "visitor_signals", "evidence_kinds", "people_found",
                                  "find_people_google", "find_people_linkedin", "linkedin_page"], crow,
                {"company": 30, "exhibitor_at": 40, "find_people_google": 30, "find_people_linkedin": 30})
    _link(ws, 12)
    _link(ws, 13)
    _link(ws, 14)

    # Tracker (round-trips through the DB)
    trow = []
    for l in leads:
        t = con.execute("SELECT * FROM tracker WHERE person_id=?", (l["id"],)).fetchone()
        trow.append([l["id"], l["name"], l["company"], l["icp"], (t["status"] if t else None) or "new",
                     t["channel"] if t else None, t["last_contacted"] if t else None,
                     t["interview_date"] if t else None, t["pain_confirmed"] if t else None, t["notes"] if t else None])
    ws = _sheet(wb, "Tracker", TRACKER_COLS, trow, {"notes": 60, "company": 28})
    dv = DataValidation(type="list", formula1='"' + ",".join(STATUSES) + '"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"E2:E{max(len(trow) + 200, 2)}")
    dv2 = DataValidation(type="list", formula1='"yes,no,partly,?"', allow_blank=True)
    ws.add_data_validation(dv2)
    dv2.add(f"I2:I{max(len(trow) + 200, 2)}")

    # Evidence
    ev = con.execute("""SELECT e.*, COALESCE(c.name, p.name) AS who, s.name AS show FROM evidence e
                        LEFT JOIN companies c ON e.entity='company' AND c.id=e.entity_id
                        LEFT JOIN people p ON e.entity='person' AND p.id=e.entity_id
                        LEFT JOIN shows s ON s.id=e.show_id""").fetchall()
    ws = _sheet(wb, "Evidence", ["entity", "who", "show", "kind", "confidence", "url", "snippet"],
                [[e["entity"], e["who"], e["show"], e["kind"], e["confidence"], e["url"], e["snippet"]] for e in ev],
                {"url": 50, "snippet": 80, "who": 28})
    _link(ws, 6)

    # Shows
    sh = con.execute("SELECT * FROM shows WHERE industry_id=? ORDER BY start_date", (industry["id"],)).fetchall()
    _sheet(wb, "Shows", ["show_id", "name", "year", "start_date", "end_date", "city", "country", "url",
                         "exhibitor_list_url"],
           [[s["id"], s["name"], s["year"], s["start_date"], s["end_date"], s["city"], s["country"], s["url"],
             s["exhibitor_list_url"]] for s in sh], {"name": 34, "url": 40, "exhibitor_list_url": 40})

    wb.save(p)
    by_icp = {}
    for l in leads:
        by_icp[l["icp"]] = by_icp.get(l["icp"], 0) + 1
    return {"file": str(p), "leads": len(leads), "by_icp": by_icp, "companies": len(comps),
            "tracker_rows_synced_from_previous": synced}
