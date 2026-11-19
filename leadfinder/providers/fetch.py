"""Fetch a page (HTML or PDF) and save readable text to data/raw/, so Claude can read it and extract rows.

Chain (mode=auto): direct download -> TinyFetch (TINYFISH_API_KEY, 1,000 free URLs/day) ->
OpenSERP /extract (local headless render) -> Firecrawl (FIRECRAWL_API_KEY,
1,000 free pages/month). If all fail (bot check, login wall), use `lf capture` - a visible browser you drive.
Downloaded content is untrusted data: it is only stored as text, never executed.
"""
import hashlib
import io
import re

import requests

from datetime import datetime, timezone

from .. import config, keys
from .search import ProviderBusy
from ..db import now

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
BLOCK_MARKERS = ("just a moment...", "enable javascript and cookies", "attention required", "access denied",
                 "verify you are human", "cf-challenge")
MIN_USEFUL_CHARS = 400


def _path_for(url, ext="md"):
    h = hashlib.sha1(url.encode()).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", url.lower().split("://", 1)[-1])[:60].strip("-")
    return config.RAW / f"{slug}-{h}.{ext}"


def _looks_blocked(text):
    t = (text or "")[:3000].lower()
    return any(m in t for m in BLOCK_MARKERS)


def _html_to_text(html):
    html = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", html)
    # keep link targets: they often carry exhibitor websites
    html = re.sub(r'(?is)<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"\2 [\1]", html)
    html = re.sub(r"(?i)<(br|/p|/div|/li|/tr|/h[1-6])[^>]*>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    import html as h
    text = h.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _page_text(page):
    """Extract text; read two-column pages (exhibitor lists, directories) column by column."""
    words = page.extract_words()
    if len(words) > 40:
        mid = page.width / 2
        crossing = sum(1 for w in words if w["x0"] < mid - 2 < w["x1"] or w["x0"] < mid + 2 < w["x1"])
        left = sum(1 for w in words if w["x1"] <= mid)
        if crossing <= len(words) * 0.02 and 0.2 < left / len(words) < 0.8:
            halves = [page.crop((0, 0, mid, page.height)), page.crop((mid, 0, page.width, page.height))]
            return "\n".join((h.extract_text() or "") for h in halves)
    return page.extract_text() or ""


def _pdf_to_text(data):
    import pdfplumber
    parts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages):
            parts.append(f"\n--- page {i + 1} ---\n" + _page_text(page))
    return "".join(parts)


def direct(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        if r.ok and r.content[:4] == b"%PDF":
            return _pdf_to_text(r.content)
        # soft 404: the server answered a .pdf URL with an HTML page (often its homepage)
        raise RuntimeError(f"direct: expected a PDF but got {ctype or 'unknown'} (HTTP {r.status_code}) - wrong URL?")
    text = _html_to_text(r.text)
    if not r.ok or _looks_blocked(r.text) or len(text) < MIN_USEFUL_CHARS:
        raise RuntimeError(f"direct: HTTP {r.status_code}, {len(text)} chars{' (bot check)' if _looks_blocked(r.text) else ''}")
    return text


def openserp_extract(url):
    r = requests.get(f"{config.OPENSERP_URL}/extract",
                     params={"url": url, "format": "json", "mode": "auto"}, timeout=180)
    if not r.ok:
        raise RuntimeError(f"openserp: HTTP {r.status_code} {r.text[:150]}")
    try:
        data = r.json()
        text = data.get("markdown") or data.get("text") or ""
    except ValueError:
        text = r.text
    if _looks_blocked(text) or len(text) < MIN_USEFUL_CHARS:
        raise RuntimeError(f"openserp: {len(text)} chars{' (bot check)' if _looks_blocked(text) else ''}")
    return text


def firecrawl(url):
    if not keys.configured("firecrawl"):
        raise RuntimeError("firecrawl: FIRECRAWL_API_KEY not set")
    return keys.run("firecrawl", lambda k: _firecrawl(url, k))


def _firecrawl(url, key):
    r = requests.post("https://api.firecrawl.dev/v2/scrape", timeout=180,
                      headers={"Authorization": f"Bearer {key.value}"},
                      json={"url": url, "formats": ["markdown"], "onlyMainContent": False, "waitFor": 3000,
                            "proxy": "basic"})
    if r.status_code in (402, 429):
        raise ProviderBusy(f"firecrawl HTTP {r.status_code}" + (" (rate limited)" if r.status_code == 429 else ""))
    if not r.ok:
        raise RuntimeError(f"firecrawl: HTTP {r.status_code} {r.text[:150]}")
    keys.count(key)
    text = (r.json().get("data") or {}).get("markdown") or ""
    if _looks_blocked(text) or len(text) < MIN_USEFUL_CHARS:
        raise RuntimeError(f"firecrawl: {len(text)} chars")
    return text


def tinyfetch(url):
    """TinyFish TinyFetch (docs read 2026-11-18): POST https://api.fetch.tinyfish.ai, X-API-Key.
    Free 1,000 URLs/day, 150/min. Returns markdown; per-URL errors such as bot_blocked / login_required."""
    if not keys.configured("tinyfish"):
        raise RuntimeError("tinyfetch: TINYFISH_API_KEY not set")
    return keys.run("tinyfish", lambda k: _tinyfetch(url, k))


def _tinyfetch(url, key):
    r = requests.post("https://api.fetch.tinyfish.ai", timeout=130, headers={"X-API-Key": key.value},
                      json={"urls": [url], "format": "markdown", "links": True, "per_url_timeout_ms": 90000})
    if r.status_code in (402, 429):
        raise ProviderBusy(f"tinyfetch HTTP {r.status_code}" + (" (rate limited)" if r.status_code == 429 else ""))
    if not r.ok:
        raise RuntimeError(f"tinyfetch: HTTP {r.status_code} {r.text[:150]}")
    keys.count(key, extra_bucket="fetch:" + datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    d = r.json()
    if d.get("errors"):
        e = d["errors"][0]
        raise RuntimeError(f"tinyfetch: {e.get('error')} {e.get('status_code') or ''}".strip())
    res = (d.get("results") or [{}])[0]
    text = res.get("text") or ""
    if _looks_blocked(text) or len(text) < MIN_USEFUL_CHARS:  # judge the page text, not the appended links
        raise RuntimeError(f"tinyfetch: only {len(text)} chars of page text (JS-loaded list? try `lf capture`)")
    if res.get("links"):  # keep link targets: exhibitor lists often carry company websites
        text += "\n\nLINKS:\n" + "\n".join(res["links"][:2000])
    return text


METHODS = {"direct": direct, "tinyfetch": tinyfetch, "openserp": openserp_extract, "firecrawl": firecrawl}


def fetch(con, url, mode="auto", refresh=False):
    """Returns dict(path, method, chars) or dict(error, tried)."""
    if not refresh:
        row = con.execute("SELECT * FROM fetch_log WHERE url=? AND status='ok'", (url,)).fetchone()
        if row:
            return {"path": row["path"], "method": "cache:" + row["method"], "chars": row["chars"]}
    order = ["direct", "tinyfetch", "openserp", "firecrawl"] if mode == "auto" else [mode]
    if url.lower().split("?")[0].endswith(".pdf") and mode == "auto":
        order = ["direct", "tinyfetch", "firecrawl"]  # renderers would return the HTML of a soft-404 page
    tried = []
    for m in order:
        try:
            text = METHODS[m](url)
        except Exception as e:  # noqa: BLE001 - any failure means: try next method
            tried.append(str(e)[:200])
            continue
        path = _path_for(url)
        path.write_text(f"SOURCE: {url}\nFETCHED: {now()} via {m}\n\n{text}")
        con.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?,?)", (url, str(path), m, "ok", len(text), now()))
        con.commit()
        return {"path": str(path), "method": m, "chars": len(text)}
    con.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?,?)", (url, None, mode, "failed", 0, now()))
    con.commit()
    return {"error": "all methods failed - use `lf capture <url>` (visible browser you drive)", "tried": tried}
