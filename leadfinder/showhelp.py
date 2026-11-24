"""Human help for exhibitor lists the tool cannot read on its own (search-box-only directories, browser-only pages).

When the evidence step finishes and a core show (one found by the research step) still has almost no exhibitors,
the run pauses (stage status 'help') and the web UI asks the user, per show, to:
  1. paste a link: a directory on a2z / ExpoFP / Map Your Show / Swapcard, a PDF fair guide, or any page that lists
     the exhibitors -> read_url() (Apify catalog readers, PDF/hall-stall parser, then Claude reading the page text);
  2. copy a prompt for a browser-capable Claude agent that works through the site WITH the human (the human does
     logins, captchas and search-box clicks) -> agent_prompt(); the agent imports with `./lf import-exhibitors` or
     prints JSON the user pastes back -> parse_rows() + import_rows();
  3. skip the show.
Decisions are kept per show (keyed by series + year too), so a re-run never asks about the same show twice.
"""
import csv
import io
import json
import re

from . import config, db

SCHEMA = """CREATE TABLE IF NOT EXISTS show_help (
  industry_id INTEGER, show_key TEXT, show_id INTEGER, status TEXT, note TEXT, at TEXT,
  PRIMARY KEY(industry_id, show_key))"""

THIN_LIST = 30            # same bar as pipeline.THIN_LIST: fewer exhibitors than this = list probably missed
DIRECTORY_HOSTS = ("a2zinfo", "a2z", "expofp", "mapyourshow", "swapcard")


def _ensure(con):
    con.execute(SCHEMA)


def show_key(show):
    base = re.sub(r"[^a-z0-9]+", " ", str(show.get("series") or show.get("name") or "").lower())
    base = re.sub(r"\b(19|20)\d\d\b", " ", base).strip()
    return f'{" ".join(base.split())}|{show.get("year") or ""}'


def decisions(con, ind):
    _ensure(con)
    return {r["show_key"]: dict(r) for r in con.execute("SELECT * FROM show_help WHERE industry_id=?", (ind["id"],))}


def decide(con, ind, show, status, note=None):
    """status: 'done' (a list was imported), 'skipped' (the user went on without it)."""
    _ensure(con)
    con.execute("""INSERT INTO show_help(industry_id, show_key, show_id, status, note, at) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(industry_id, show_key) DO UPDATE SET show_id=excluded.show_id, status=excluded.status,
                   note=COALESCE(excluded.note, note), at=excluded.at""",
                (ind["id"], show_key(show), show["id"], status, note, db.now()))
    con.commit()


def exhibitor_count(con, show_id):
    return con.execute("""SELECT COUNT(DISTINCT entity_id) FROM evidence WHERE show_id=? AND kind='exhibitor'
                          AND entity='company'""", (show_id,)).fetchone()[0]


def _evidence_detail(con, ind):
    row = con.execute("SELECT status, detail FROM pipeline_stages WHERE industry_id=? AND stage='evidence'",
                      (ind["id"],)).fetchone()
    d = json.loads(row["detail"]) if row and row["detail"] else {}
    return d.get("result") or d   # a paused stage keeps its result under "result"


def _short_reason(text):
    """'not read: no exhibitors found on <url>. Next step: ...' -> 'no exhibitors found on <url>'."""
    t = re.sub(r"^not read:\s*", "", str(text or "")).split(". Next step")[0].strip()
    return t[:300] or "the exhibitor list could not be found or read automatically"


def needing_help(con, ind, core_names=None):
    """Core shows still thin and not yet decided, each with why it couldn't be read.
    Core = the shows the evidence step found thin before it ran (the research step's shows); shows added later from
    a company's own "visit us at" page are one-offs and never asked about. Without that record: every thin show."""
    d = _evidence_detail(con, ind)
    core = set(core_names if core_names is not None else (d.get("thin_shows_before") or []))
    why = {**(d.get("firecrawl_lists") or {}), **(d.get("catalogs") or {})}
    found = d.get("list_links") or {}
    done = decisions(con, ind)
    out = []
    for s in con.execute("SELECT * FROM shows WHERE industry_id=? ORDER BY start_date", (ind["id"],)):
        s = dict(s)
        if core and s["name"] not in core:
            continue
        if show_key(s) in done:
            continue
        n = exhibitor_count(con, s["id"])
        if n >= THIN_LIST:
            continue
        out.append({"id": s["id"], "name": s["name"], "series": s["series"], "year": s["year"], "start_date": s["start_date"], "end_date": s["end_date"],
                    "city": s["city"], "country": s["country"], "url": s["url"],
                    "exhibitor_list_url": s["exhibitor_list_url"], "exhibitors": n,
                    "reason": _short_reason(why.get(s["name"])), "links": found.get(s["name"]) or []})
    return out


# ---------- rows the user pastes (JSON from the agent, CSV, or one name per line) ----------
def parse_rows(text):
    """-> list of dicts with at least 'name'. Accepts a JSON array / {"exhibitors": [...]} / JSON lines, a CSV with a
    header row, or plain lines (one company per line). Fenced code blocks are unwrapped."""
    t = (text or "").strip()
    m = re.search(r"```(?:json|csv)?\s*\n(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    if not t:
        return []
    if t[0] in "[{":
        try:
            data = json.loads(t)
        except ValueError:
            data = None
            try:
                data = [json.loads(line) for line in t.splitlines() if line.strip()]
            except ValueError:
                pass
        if isinstance(data, dict):
            data = data.get("exhibitors") or data.get("rows") or data.get("companies") or [data]
        if isinstance(data, list):
            rows = [{"name": x} if isinstance(x, str) else x for x in data]
            return [r for r in rows if isinstance(r, dict) and str(r.get("name") or r.get("company") or "").strip()]
    lines = [l for l in t.splitlines() if l.strip()]
    if len(lines) > 1 and ("," in lines[0] or "\t" in lines[0]) and re.search(r"\b(name|company|exhibitor)\b",
                                                                             lines[0], re.I):
        dialect = "excel-tab" if "\t" in lines[0] else "excel"
        rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
                for r in csv.DictReader(io.StringIO("\n".join(lines)), dialect=dialect)]
        for r in rows:
            r.setdefault("name", r.get("company") or r.get("company name") or r.get("exhibitor") or "")
        return [r for r in rows if r["name"]]
    names = [re.sub(r"^\s*(\d+[.)]|[-*•])\s*", "", l).strip() for l in lines]
    return [{"name": n} for n in names if 1 < len(n) <= 150]


def import_rows(con, ind, show, rows, source_url=None):
    """Rows -> companies + confirmed exhibitor evidence for this show. Every evidence row needs a URL: the row's own
    profile_url, else the page the human used (source_url), else the show's list / site URL."""
    from .showdata import ingest_exhibitors
    src = (source_url or "").strip() or show.get("exhibitor_list_url") or show.get("url") or show.get("source_url")
    if not src:
        return {"added": 0, "error": "no source URL: give the page the list came from"}
    before = exhibitor_count(con, show["id"])
    ingest_exhibitors(con, ind, show["id"], rows, src)
    after = exhibitor_count(con, show["id"])
    added = after - before
    if added > 0:
        decide(con, ind, show, "done", f"{added} exhibitors imported")
    return {"rows": len(rows), "added": added, "exhibitors_now": after, "source_url": src}


# ---------- a pasted link ----------
def _claude_extract(text, show_name):
    """Company names from a fetched page's text, by a tool-less Claude call. [] when it fails."""
    from .pipeline import _claude_text
    prompt = (f"Below is the text of a web page or PDF that should list the exhibitors of the trade show "
              f"'{show_name}'. Return ONLY a JSON array of objects {{\"name\", \"country\"?, \"city\"?, \"booth\"?, "
              "\"website\"?}} - one per exhibiting company that is actually listed in the text. Do not add companies "
              "that are not in the text. Leave out sponsors' slogans, menu items, organisers and navigation. If the "
              "text lists no exhibitors, return [].\n\nTEXT:\n" + text[:60000])
    out = _claude_text(prompt, "sonnet", "low", timeout=300)
    if not out:
        return []
    m = re.search(r"\[.*\]", out, re.S)
    try:
        rows = json.loads(m.group(0)) if m else []
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and str(r.get("name") or "").strip()]


def _rows_from_text(text, show, use_claude):
    from .parsers import parse_hall_stall
    rows = [{"name": r["name"], "booth": f'Hall {r["hall"]} Stall {r["stall"]}'} for r in parse_hall_stall(text)]
    via = "hall/stall parser"
    if len(rows) < 5 and use_claude and text.strip():
        found = _claude_extract(text, show["name"])
        if len(found) > len(rows):   # never trade a few real parser rows for an empty Claude answer
            rows, via = found, "Claude reading the page"
    return rows, via


def read_url(con, ind, show, url, budget=0.5, log=print, use_claude=True, apify=True, render=True):
    """Read the exhibitors from a link (pasted by the user, or found on the show's site by Firecrawl /map). Order:
    Apify catalog readers (directory platforms, known fair sites), then fetch + hall/stall parser (fair-guide PDFs)
    and Claude reading the text, then - for lists that only load in a browser - a Firecrawl render that scrolls the
    page (1 Firecrawl credit) read the same way."""
    import time
    from . import showdata
    from .providers import events as ev
    from .providers import firecrawl as fc
    from .providers.fetch import fetch
    url = url.strip()
    tried = []
    is_pdf = url.lower().split("?")[0].endswith(".pdf")
    if apify and not is_pdf and ev.available():
        s2 = {**show, "exhibitor_list_url": url}
        res = showdata.harvest_exhibitors(con, ind, s2, budget, deadline=time.time() + 15 * 60, log=log,
                                          allow_capped=False)
        if res.get("added"):
            con.execute("UPDATE shows SET exhibitor_list_url=? WHERE id=?", (url, show["id"]))
            con.commit()
            decide(con, ind, show, "done", f'{res["added"]} exhibitors via {res.get("actor")}')
            return {"added": res["added"], "via": "apify:" + str(res.get("actor")).split("~")[0],
                    "cost": res.get("cost", 0), "exhibitors_now": exhibitor_count(con, show["id"])}
        tried.append(f'catalog readers: {res.get("error") or "no rows"}')
    f = fetch(con, url)
    text = open(f["path"]).read() if not f.get("error") else ""
    if f.get("error"):
        tried.append("fetch: page could not be loaded")
    rows, via = _rows_from_text(text, show, use_claude)
    if len(rows) < 5 and render and not is_pdf and fc.available()\
            and not any(re.search(r"HTTP (404|410)\b", t) for t in f.get("tried", [])):   # error pages cost a credit too
        try:   # the list may load only in a browser (scripts, "load more" on scroll)
            page = fc.scrape(url, actions=fc.scroll_actions(), links=True)
            if page["status"] and page["status"] >= 400:
                raise RuntimeError(f"HTTP {page['status']}")
            rtext = page["markdown"] + ("\n\nLINKS:\n" + "\n".join(page["links"][:2000]) if page["links"] else "")
            rows2, via2 = _rows_from_text(rtext, show, use_claude)
            if len(rows2) > len(rows):
                rows, via = rows2, "Firecrawl browser render, " + via2
            else:
                tried.append(f"Firecrawl browser render: no exhibitors in {len(page['markdown'])} characters")
        except Exception as e:  # noqa: BLE001 - out of budget, HTTP error: fall through to the human options
            tried.append(f"Firecrawl browser render: {str(e)[:160]}")
    if not rows:
        if text:
            tried.append(f"{via}: no exhibitors in the page text ({f['chars']} characters)")
        return {"added": 0, "tried": tried,
                "next": "use the agent prompt: the list probably loads only after a search or a click"
                if text else "use the agent prompt: this page needs a browser"}
    res = import_rows(con, ind, show, rows, url)
    if res["added"]:
        con.execute("UPDATE shows SET exhibitor_list_url=COALESCE(exhibitor_list_url, ?) WHERE id=?", (url, show["id"]))
        con.commit()
    return {**res, "via": via, "tried": tried}


# ---------- an uploaded file (PDF fair guide, CSV, Excel, JSON) ----------
UPLOAD_TYPES = (".pdf", ".csv", ".tsv", ".txt", ".json", ".xlsx", ".xlsm")


def _excel_rows(path):
    """First sheet with a header row naming the company column (name / company / exhibitor); otherwise the first
    non-empty column is taken as company names."""
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        grid = [[("" if c is None else str(c).strip()) for c in r] for r in ws.iter_rows(values_only=True)]
        grid = [r for r in grid if any(r)]
        if not grid:
            continue
        for hi, row in enumerate(grid[:15]):
            low = [c.lower() for c in row]
            col = next((i for i, c in enumerate(low) if re.search(r"\b(company|exhibitor|name)\b", c)), None)
            if col is not None:
                heads = [c or f"col{i}" for i, c in enumerate(low)]
                rows = []
                for r in grid[hi + 1:]:
                    d = {heads[i]: v for i, v in enumerate(r) if i < len(heads) and v}
                    d["name"] = r[col] if col < len(r) else ""
                    if d["name"]:
                        rows.append(d)
                return rows
        first = next(i for i in range(max(len(r) for r in grid)) if any(i < len(r) and r[i] for r in grid))
        return [{"name": r[first]} for r in grid if first < len(r) and r[first]]
    return []


def file_rows(path, show_name="", use_claude=True):
    """-> (rows, how). Raises ValueError for an unsupported or unreadable file."""
    ext = "." + str(path).rsplit(".", 1)[-1].lower() if "." in str(path) else ""
    if ext == ".xls":
        raise ValueError("old .xls files can't be read: save it as .xlsx or CSV and upload again")
    if ext not in UPLOAD_TYPES:
        raise ValueError(f"unsupported file type {ext or '(none)'}: upload a PDF, CSV or Excel (.xlsx) file")
    if ext in (".xlsx", ".xlsm"):
        return _excel_rows(path), "Excel"
    if ext == ".pdf":
        from .parsers import parse_hall_stall
        from .providers.fetch import _pdf_to_text
        with open(path, "rb") as f:
            text = _pdf_to_text(f.read())
        rows = [{"name": r["name"], "booth": f'Hall {r["hall"]} Stall {r["stall"]}'} for r in parse_hall_stall(text)]
        if len(rows) < 5 and use_claude:
            found = _claude_extract(text, show_name)
            if len(found) > len(rows):
                return found, "PDF read by Claude"
        return rows, "PDF (hall/stall layout)"
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return parse_rows(f.read()), ext[1:].upper()


def read_file(con, ind, show, path, source_url=None, use_claude=True):
    try:
        rows, how = file_rows(path, show["name"], use_claude)
    except ValueError as e:
        return {"added": 0, "error": str(e)}
    if not rows:
        return {"added": 0, "error": f"no companies found in the file ({how})"}
    return {**import_rows(con, ind, show, rows, source_url), "via": how}


# ---------- prompt for a browser agent working with the human ----------
def agent_prompt(con, ind, show):
    root = config.ROOT
    slug = ind["slug"]
    out_file = f"data/work/{slug}/exh_show_{show['id']}.json"
    reason = next((h["reason"] for h in needing_help(con, ind, core_names=[]) if h["id"] == show["id"]), None)
    seen, lines = set(), []
    for k, v in (("Show website", show.get("url")), ("Exhibitor list URL tried", show.get("exhibitor_list_url")),
                 ("Where the show was found", show.get("source_url"))):
        if v and v not in seen:
            seen.add(v)
            lines.append(f"  - {k}: {v}")
    urls = "\n".join(lines)
    when = " to ".join(x for x in (show.get("start_date"), show.get("end_date")) if x) or "unknown dates"
    where = ", ".join(x for x in (show.get("city"), show.get("country")) if x) or "unknown place"
    return f"""You are helping me collect the EXHIBITOR LIST of one trade show. You can use a web browser (for example
Claude in Chrome). I am sitting at the computer and will do the steps you can't: logging in, solving captchas or "are
you a human" checks, typing into search boxes, clicking "load more" when you ask me to. Tell me exactly what to click
or type, wait for me, then carry on.

THE SHOW
  Name: {show['name']}
  Dates: {when}
  Place: {where}
{urls}
  Why my lead-finder tool could not read it: {reason or "the list loads only through a search box or in a browser"}

WHAT I NEED
Every exhibiting company listed for this edition of the show (not past editions, not sponsors-only pages, not
speakers). For each company, whatever the directory shows of: name, country, city, booth/stand, website, and the URL
of the company's own exhibitor-profile page in the directory.

HOW TO GO ABOUT IT
1. Open the show website and find the exhibitor list / exhibitor directory / "who's exhibiting" / floor plan.
2. Check where the directory is hosted. If its address is on a2zinfo.net, expofp.com, mapyourshow.com or swapcard.com
   (also inside an iframe), STOP and tell me that URL: my tool reads those platforms by itself, so you don't need to
   copy anything.
3. If the list only appears after a search: try an empty search, "*", a "show all" / A-Z option, or each letter A-Z.
   Page through every results page (ask me to click if needed).
4. If there is a downloadable PDF or Excel exhibitor list / fair guide, tell me its URL instead - my tool reads PDFs.
5. Only write down companies you actually see on the page. Never guess or add companies from memory.
6. If a bot check appears, ask me to solve it. Do not try to get around it, and do not use other people's accounts.

HOW TO HAND IT BACK
Make a JSON array, one object per company, with these keys (leave out what you don't have):
  [{{"name": "...", "country": "...", "city": "...", "booth": "...", "website": "...", "profile_url": "..."}}]
Also note the URL of the page you read the list from (the SOURCE URL).

- If you can run commands on my computer: save the array to {root}/{out_file}, then run
    cd {root} && ./lf import-exhibitors --industry {slug} --show {show['id']} --file {out_file} --url "<SOURCE URL>"
  and tell me how many it added.
- Otherwise print the SOURCE URL on one line and the array in ONE ```json code block, and I will paste it into my tool.
  (A CSV file with a header row - name, country, city, booth, website, profile_url - also works: I can upload it.)
- If you found a downloadable PDF / Excel exhibitor list, just give me its link: I can download and upload it myself.
"""
