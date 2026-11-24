"""Exhibitors' customers: the companies an exhibitor names on its own website (clients / customers / case studies /
references / testimonials pages). These buyers are who walks a trade show, so they feed the buyer personas
(company rules 'visitor' / 'visitor_or_exhibitor'). Indirect: the evidence says the company BUYS from an exhibitor,
not that it attended - kind customer_of_exhibitor, confidence inferred, no show.

Cost: the exhibitor's homepage and client page are fetched directly (free). Only when the homepage shows no client
link is the site mapped with Firecrawl (1 credit, cached 30 days, within the run's Firecrawl budget); a client page the
direct fetch can't read goes through the normal fetch chain. One tool-less Sonnet call reads up to BATCH pages.
Each exhibitor's answer is cached (search_cache 'customers_page'), so a re-run never reads a site twice.
"""
import html as htmlmod
import json
import re
from urllib.parse import urljoin, urlparse

import requests

from . import db
from .normalize import _flat, norm_domain, strict_company_match
from .providers.fetch import UA, _html_to_text

CLIENT_RE = re.compile(r"client|customer|case[-_ ]?stud|success[-_ ]?stor|referen|testimonial|kunden|our[-_ ]?users|"
                       r"who[-_ ]?we[-_ ]?serve|trusted[-_ ]?by", re.I)
NOT_CLIENT_RE = re.compile(r"support|service|care|login|log-in|signin|sign-in|portal|feedback|complaint|career|job|"
                           r"privacy|policy|terms|cookie|download|\.pdf(\?|$)|^mailto:|^tel:|^javascript:|register|"
                           r"account|cart|checkout|reference[-_ ]?(guide|manual)", re.I)
BATCH = 6
PAGE_CHARS = 7000
CACHE = "customers_page"
JUNK_NAME = re.compile(r"^(logo|client|customer|image|img|banner|icon|slide|partner|brand|photo)s?[\s\-_]*\d*$", re.I)


def _get(url, timeout=25):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True)
    if not r.ok or "html" not in r.headers.get("content-type", "html"):
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.url, r.text


def _same_site(a, b):
    ha, hb = urlparse(a).netloc.lower().removeprefix("www."), urlparse(b).netloc.lower().removeprefix("www.")
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def client_links(base_url, html):
    """Links on a page that look like a clients / customers / case-studies page, best first."""
    out = {}
    for href, text in re.findall(r'(?is)<a\s[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', html or ""):
        url = urljoin(base_url, htmlmod.unescape(href.strip()))
        if not url.startswith("http") or not _same_site(url, base_url):
            continue
        label = re.sub(r"(?s)<[^>]+>", " ", text)
        label = re.sub(r"\s+", " ", htmlmod.unescape(label)).strip()[:80]
        path = urlparse(url).path
        if NOT_CLIENT_RE.search(path) or NOT_CLIENT_RE.search(label):
            continue
        score = (3 if CLIENT_RE.search(path) else 0) + (2 if CLIENT_RE.search(label) else 0)
        if score:
            out[url.rstrip("/")] = max(out.get(url.rstrip("/"), 0), score)
    return [u for u, _ in sorted(out.items(), key=lambda kv: (-kv[1], len(kv[0])))]


def rank_mapped(links):
    """Firecrawl map rows -> client-page URLs, best first."""
    urls = []
    for l in links:
        u = l.get("url") if isinstance(l, dict) else l
        path = urlparse(u or "").path
        if u and CLIENT_RE.search(path) and not NOT_CLIENT_RE.search(path):
            urls.append(u.rstrip("/"))
    return sorted(dict.fromkeys(urls), key=lambda u: (u.count("/"), len(u)))


def page_text(html):
    """Logo alt texts and image file names first (client pages are mostly logos), then the page's text."""
    names = []
    for tag in re.findall(r"(?is)<img\s[^>]*>", html or ""):
        for attr in ("alt", "title"):
            m = re.search(rf'(?i)\b{attr}=["\']([^"\']{{2,80}})["\']', tag)
            if m:
                names.append(htmlmod.unescape(m.group(1)).strip())
        m = re.search(r'(?i)\bsrc=["\']([^"\']+)["\']', tag)
        if m:
            base = re.sub(r"\.(png|jpe?g|gif|svg|webp)(\?.*)?$", "", m.group(1).split("/")[-1], flags=re.I)
            base = re.sub(r"[-_]+", " ", re.sub(r"[-_]?\d{2,}x\d{2,}$", "", base)).strip()
            if 2 <= len(base) <= 60:
                names.append(base)
    names = list(dict.fromkeys(n for n in names if n))
    return ("IMAGES (alt text / file names): " + " | ".join(names) + "\n\nTEXT:\n" + _html_to_text(html or ""))[:PAGE_CHARS]


def find_client_page(con, domain, log=print, allow_map=True):
    """-> (url, text) of the exhibitor's best client page, or (None, reason)."""
    home = f"https://{domain}"
    try:
        home, html = _get(home)
    except Exception as e:  # noqa: BLE001
        html, err = "", str(e)[:80]
    else:
        err = None
    links = client_links(home, html)
    if not links and allow_map:
        from .providers import firecrawl as fc
        if fc.available() and fc.can_spend(1, con):
            try:
                links = rank_mapped(fc.map_site(home, con=con))
            except Exception as e:  # noqa: BLE001
                log(f"    {domain}: site map failed ({str(e)[:80]})")
    for url in links[:2]:
        try:
            _, page = _get(url)
            text = page_text(page)
        except Exception:  # noqa: BLE001 - blocked or JS-only: the normal fetch chain (renders, may use Firecrawl)
            from .providers import fetch as fetchmod
            got = fetchmod.fetch(con, url)
            if got.get("error"):
                continue
            from pathlib import Path
            text = Path(got["path"]).read_text(errors="replace")[:PAGE_CHARS]
        if len(text) > 80:   # logo-only pages are short: alt texts and file names are the content
            return url, text
    return None, ("homepage not readable: " + err) if err and not links else "no clients / customers page found"


def extract(pages, model="sonnet", effort="low"):
    """pages: [{id, exhibitor, url, text}] -> {id: [names]} by one tool-less Claude call. {} when it fails."""
    from .pipeline import _claude_text
    parts = "\n\n".join(f"=== SECTION {p['id']} | exhibitor: {p['exhibitor']} | page: {p['url']}\n{p['text']}"
                        for p in pages)
    prompt = ("Each section below is the text of one company's (the exhibitor's) clients / customers / case-studies "
              "page. For each section list the companies the page names as the exhibitor's CUSTOMERS or CLIENTS: "
              "logo alt texts and image file names count, so do companies in case-study titles and testimonials. "
              "Do NOT list: the exhibitor itself or its group companies, its partners, distributors, dealers, "
              "suppliers or the technology brands it uses or sells, certification bodies, awards, associations, "
              "media, investors, the website builder, or generic words ('logo 3', 'client'). Write each name as on "
              "the page, without legal suffixes only if the page drops them. If a section names no customers, give "
              "an empty list. Return ONLY a JSON object {\"<section id>\": [\"Company\", ...]}.\n\n" + parts)
    out = _claude_text(prompt, model, effort, timeout=400)
    m = re.search(r"\{.*\}", out or "", re.S)
    try:
        got = json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}
    return {str(k): [str(n).strip() for n in v if isinstance(n, str)] for k, v in got.items() if isinstance(v, list)}


def clean_names(names, exhibitor, text):
    """Drop junk, the exhibitor itself, and any name that is not actually on the page (no invented customers)."""
    flat_text = _flat(text)
    out = []
    for n in names:
        n = re.sub(r"\s+", " ", n).strip(" .,-|")
        if not (2 <= len(n) <= 80) or JUNK_NAME.match(n) or not re.search(r"[A-Za-z]", n):
            continue
        if strict_company_match(n, exhibitor) or strict_company_match(exhibitor, n):
            continue
        if _flat(n) and _flat(n) not in flat_text:
            continue
        out.append(n)
    return list(dict.fromkeys(out))


def exhibitors_to_read(con, ind, limit):
    """This search's exhibitor companies with a website, most shows first (core shows only)."""
    from .people import company_facts
    rows = con.execute("""SELECT DISTINCT c.* FROM companies c
                          JOIN company_industries ci ON ci.company_id=c.id AND ci.industry_id=?
                          JOIN evidence e ON e.entity='company' AND e.entity_id=c.id AND e.kind='exhibitor'
                          JOIN shows s ON s.id=e.show_id AND s.industry_id=? AND s.origin IS NOT 'evidence'
                          WHERE c.domain IS NOT NULL AND c.domain != ''""", (ind["id"], ind["id"])).fetchall()
    rows = sorted(rows, key=lambda c: (-company_facts(con, c["id"])["exhibitor_window_count"], c["id"]))
    return rows[:limit] if limit else rows


def _cached(con, domain):
    row = con.execute("SELECT response FROM search_cache WHERE provider=? AND query=?", (CACHE, domain)).fetchone()
    return json.loads(row["response"]) if row else None


def _cache(con, domain, value):
    con.execute("INSERT OR REPLACE INTO search_cache VALUES (?,?,?,?,?)", (CACHE, domain, "", json.dumps(value), db.now()))
    con.commit()


def ingest(con, ind, exhibitor, url, names, shows_text):
    n = 0
    for name in names:
        cid = db.upsert_company(con, name, industry_id=ind["id"])
        if cid is None or cid == exhibitor["id"]:
            continue
        db.add_evidence(con, "company", cid, "customer_of_exhibitor", "inferred", url,
                        f"named as a customer on {exhibitor['name']}'s website ({exhibitor['name']} exhibits at "
                        f"{shows_text})")
        n += 1
    con.commit()
    return n


def _shows_text(con, company_id, ind_id):
    rows = con.execute("""SELECT DISTINCT s.name FROM evidence e JOIN shows s ON s.id=e.show_id
                          WHERE e.entity='company' AND e.entity_id=? AND e.kind='exhibitor' AND s.industry_id=?
                          LIMIT 3""", (company_id, ind_id)).fetchall()
    return ", ".join(r["name"] for r in rows) or "a show"


def run(con, ind, limit=20, log=print, deadline=None, allow_map=True, model="sonnet"):
    """Read up to `limit` new exhibitors' client pages. Returns counts."""
    import time
    stats = {"exhibitors_checked": 0, "client_pages_found": 0, "customers_added": 0, "from_cache": 0}
    pending = []
    for c in exhibitors_to_read(con, ind, None):
        dom = norm_domain(c["domain"])
        got = _cached(con, dom)
        if got is not None:   # read before: re-attach its customers (free), never re-read the site
            if got.get("names"):
                stats["customers_added"] += ingest(con, ind, c, got["url"], got["names"], _shows_text(con, c["id"], ind["id"]))
            stats["from_cache"] += 1
            continue
        if stats["exhibitors_checked"] >= limit or (deadline and time.time() > deadline):
            continue
        stats["exhibitors_checked"] += 1
        url, text = find_client_page(con, dom, log, allow_map)
        if not url:
            _cache(con, dom, {"url": None, "names": [], "note": text})
            continue
        stats["client_pages_found"] += 1
        pending.append({"id": str(len(pending) + 1), "company": c, "domain": dom, "exhibitor": c["name"], "url": url,
                        "text": text})
    for i in range(0, len(pending), BATCH):
        batch = pending[i:i + BATCH]
        log(f"  reading the customers named on {len(batch)} exhibitor page(s): "
            + ", ".join(p["exhibitor"] for p in batch))
        found = extract(batch, model)
        for p in batch:
            if not found and p["id"] not in found:
                continue   # the reader failed: not cached, so a re-run tries again
            names = clean_names(found.get(p["id"], []), p["exhibitor"], p["text"])
            _cache(con, p["domain"], {"url": p["url"], "names": names})
            stats["customers_added"] += ingest(con, ind, p["company"], p["url"], names,
                                               _shows_text(con, p["company"]["id"], ind["id"]))
    return stats
