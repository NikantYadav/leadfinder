"""Import trade-show data from Apify (providers/events.py) into shows / companies / people / evidence.

Budget: every call takes a USD budget and never starts a run whose cap (maxTotalChargeUsd) exceeds what is left of it.
Apify credit is shared with LinkedIn people search, so callers keep budgets small (pipeline default: $2 per search).
"""
import json
import time

from . import db
from .providers import events as ev
from .providers.search import ProviderBusy, ProviderError


def _domain_source(con, cid, website):
    if website:
        con.execute("UPDATE companies SET domain_source='catalog' WHERE id=? AND domain_source IS NULL AND domain IS NOT NULL",
                    (cid,))


def _spent(con, usd):
    """Apify USD spent through this module, per month (the credits page reads Apify's own figure; this is a log)."""
    from datetime import datetime, timezone
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    cents = int(round(usd * 100))
    if cents:
        con.execute("""INSERT INTO credit_usage(provider, month, used) VALUES ('apify_events_cents', ?, ?)
                       ON CONFLICT(provider, month) DO UPDATE SET used=used+?""", (month, cents, cents))


def ingest_exhibitors(con, ind, show_id, rows, list_url):
    """Catalog rows -> companies + confirmed exhibitor evidence. Rows without a profile URL use list_url as the
    source; with neither, the row is skipped (every evidence row needs a URL)."""
    n = 0
    for it in rows:
        r = ev.parse_exhibitor(it)
        if not r or not (r["profile_url"] or list_url):
            continue
        cid = db.upsert_company(con, r["name"], r["website"], r["country"], r["city"], None, ind["id"])
        if cid is None:
            continue
        _domain_source(con, cid, r["website"])
        if "linkedin.com/company/" in (r.get("linkedin_url") or ""):
            con.execute("UPDATE companies SET linkedin_url=COALESCE(linkedin_url, ?) WHERE id=?", (r["linkedin_url"], cid))
        snippet = " | ".join(x for x in (r["booth"] and f"stand {r['booth']}", r["categories"]) if x)
        db.add_evidence(con, "company", cid, "exhibitor", "confirmed", r["profile_url"] or list_url, snippet, show_id)
        n += 1
    con.commit()
    return n


def _spec_for(actor):
    return next((s for s in ev.CATALOGS if s["actor"] == actor), None) or \
        (ev.SKYTHON_SPEC if actor == ev.SKYTHON else {"actor": actor})


def harvest_exhibitors(con, ind, show, budget_usd, limit=None, deadline=None, log=print, ref=None, on_start=None,
                       allow_capped=True):
    """Read one show's exhibitor catalog. Tries the readers route() gives (fair name or directory URL) until one
    returns rows. `ref` adopts a run started earlier (resume). Returns {added, cost, actor, ref, error?}.
    allow_capped=False (the pipeline) leaves out readers capped on the FREE Apify plan unless the plan is paid."""
    if ref:
        routes = [(_spec_for(ref["actor"]), None)]
    else:
        routes = ev.route(show, allow_capped=allow_capped or ev.paid_plan())
    if not routes:
        return {"added": 0, "cost": 0, "error": "no catalog reader knows this show (no supported fair name or "
                                                "directory URL)"}
    src_url = next((u for u in (show.get("exhibitor_list_url"), show.get("url")) if u), None)
    spent, last_err = 0.0, None
    for spec, value in routes:
        actor = spec["actor"]
        lim = limit or int((budget_usd - spent - spec.get("start_fee", 0) - 0.02) / ev.price(spec))  # 0.02: cap margin
        if lim < max(spec.get("min_rows", 0), 10):  # several readers take 0 as "the whole list"
            last_err = "budget too small for this catalog reader"
            continue
        cap = ev.max_charge(spec, lim)
        if not ref and spent + cap > budget_usd + 1e-9:
            last_err = f"budget ${budget_usd:.2f} too small"
            continue
        if not ref:
            log(f"  {show['name']}: reading exhibitors with {actor.split('~')[-1]} ({value[:60]}; up to {lim} rows, "
                f"${cap:.2f} cap)")
        try:
            rows, run, ref = ev.run_actor(actor, ev.exhibitor_body(spec, value, lim) if value else None, cap, deadline,
                                          log, ref=ref, on_start=on_start)
        except (ProviderBusy, ProviderError) as e:
            last_err = str(e)
            ref = None
            continue
        spent += ev.cost(run)
        _spent(con, ev.cost(run))
        added = ingest_exhibitors(con, ind, show["id"], rows, src_url)
        if added:
            return {"added": added, "cost": round(spent, 3), "actor": actor, "ref": ref, "status": run["status"],
                    "rows": len(rows), "limit": lim}
        last_err = next((it.get("error") for it in rows if it.get("error")), None) or f"{actor}: no exhibitors read"
        ref = None
    return {"added": 0, "cost": round(spent, 3), "error": (last_err or "")[:300]}


def discover_shows(con, ind, query, countries=None, start_date=None, end_date=None, max_items=40, log=print):
    """10times candidates (not added to the shows table: the research step picks the relevant ones)."""
    cap = max_items * ev.PRICE[ev.TENTIMES] + 0.02
    rows, run, _ = ev.run_actor(ev.TENTIMES, ev.discover_body(query, countries, start_date, end_date, max_items),
                                cap, time.time() + 600, log)
    _spent(con, ev.cost(run))
    out = [e for e in (ev.parse_event(r) for r in rows) if e]
    return {"cost": round(ev.cost(run), 3), "events": out}


def ingest_people(con, ind, show_id, items, min_score=0.3):
    """sherwood output -> companies (exhibitor evidence) and people (exhibitor staff / visitors) with their post URL."""
    comps, people = ev.parse_people(items)
    nc = npl = 0
    for c in comps:
        cid = db.upsert_company(con, c["name"], c["website"], c["country"], None, None, ind["id"])
        if cid is None:
            continue
        _domain_source(con, cid, c["website"])
        if c["linkedin_url"]:
            con.execute("UPDATE companies SET linkedin_url=COALESCE(linkedin_url, ?) WHERE id=?", (c["linkedin_url"], cid))
        if c["url"]:
            db.add_evidence(con, "company", cid, "exhibitor", "confirmed", c["url"], c["snippet"], show_id)
            nc += 1
    for p in people:
        if p["score"] < min_score:
            continue
        conf = "likely" if p["score"] >= 0.7 else "inferred"
        kind = "exhibitor" if p["staff"] else "visitor_post"
        cid = db.upsert_company(con, p["company"], p["company_domain"], industry_id=ind["id"]) if p["company"] else None
        pid = db.upsert_person(con, cid, p["name"], p["title"], None, p["linkedin_url"], source="apify_show_people")
        if pid is None:
            continue
        db.add_evidence(con, "person", pid, kind, conf, p["url"], p["snippet"], show_id)
        if cid and kind == "visitor_post":
            db.add_evidence(con, "company", cid, kind, "inferred", p["url"], p["snippet"], show_id)
        npl += 1
    con.commit()
    return {"exhibitors_with_evidence": nc, "people": npl,
            "visitors": sum(1 for p in people if not p["staff"] and p["score"] >= min_score)}


def show_people(con, ind, show, budget_usd, target=None, deadline=None, log=print, ref=None, role_based=False,
                on_start=None):
    """People who posted/commented about the show on LinkedIn (sherwood). Its sub-actors bill separately (each at
    least $0.30, Google search $0.50), so about 60% of the budget goes to them and the rest caps sherwood's own fee."""
    if not ref:
        if budget_usd < 1.5:
            return {"people": 0, "cost": 0, "error": "budget under $1.50: the sub-actor minimums would eat it"}
        sub = round(budget_usd * 0.6, 2)
        own = round(budget_usd - sub, 2)
        body = ev.people_body(show, target, max_companies=30, max_posts=int(min(150, sub / 0.002 / 4)),
                              role_based=role_based, sub_usd=sub)
        log(f"  {show['name']}: looking for people who posted about it (budget ${budget_usd:.2f})")
        ref = ev.start(ev.PEOPLE, body, own, log=log)
        if on_start:
            on_start(ref)
    run = ev.wait(ref, deadline, log=log)
    items = ev.dataset(ref, run)
    _spent(con, ev.cost(run))
    est = next((i for i in items if i.get("type") == "estimate"), {})
    out = ingest_people(con, ind, show["id"], items)
    return {**out, "cost_own": round(ev.cost(run), 3), "sub_actor_cap": est.get("subActorsCapUsd"),
            "status": run["status"], "ref": ref}


def dump(obj):
    return json.dumps(obj, indent=1, default=str)
