"""Fit score, attendance confidence and the '3+ shows a year' counter."""
from datetime import date, timedelta

ATTEND_KINDS = {"exhibitor", "visitor_post", "thanked_by_exhibitor", "order", "delegation", "speaker",
                "buyer_meet", "report_named", "matchmaking", "app_attendee", "testimonial"}
SENIOR = ("chief", "cxo", "ceo", "cro", "cpo", "president", "vp", "vice president", "director", "head", "gm",
          "general manager", "country manager", "national", "partner", "owner", "founder", "managing")
MID = ("manager", "lead", "principal", "senior", "sr.", "key account", "enterprise")
RANK = {"confirmed": 3, "likely": 2, "inferred": 1, None: 0}


def _show_date(s):
    sd = s["start_date"] if "start_date" in s.keys() else None
    if sd:
        try:
            return date.fromisoformat(sd[:10])
        except ValueError:
            pass
    yr = s["year"] if "year" in s.keys() else None
    return date(int(yr), 7, 1) if yr else None  # unknown date -> mid-year of its edition


def best_window_count(shows, window_days=365):
    """Max number of distinct shows that start within any rolling `window_days` window.
    Two editions of the same series only both count if they fall inside the same window."""
    dates = sorted(d for d in (_show_date(s) for s in shows) if d)
    best, j = 0, 0
    for i in range(len(dates)):
        while dates[i] - dates[j] > timedelta(days=window_days - 1):
            j += 1
        best = max(best, i - j + 1)
    return best


def seniority_points(title):
    t = f" {(title or '').lower()} "
    if any(f" {k}" in t for k in SENIOR):
        return 25
    if any(f" {k}" in t for k in MID):
        return 17
    return 8


def score_person(person, person_ev, company_ev, company_window_count, min_shows=3):
    """Returns (score 0-100, confidence, why)."""
    why = []
    s = seniority_points(person["title"])
    if person["icp"]:
        s += 20
        why.append(f"ICP {person['icp']} title: {person['title']}")
    # attendance confidence: strongest of person-level attendance evidence, else company-level
    p_att = [e for e in person_ev if e["kind"] in ATTEND_KINDS]
    c_att = [e for e in company_ev if e["kind"] in ATTEND_KINDS]
    if p_att:
        conf = max((e["confidence"] for e in p_att), key=RANK.get)
        why.append(f"person seen at show ({p_att[0]['kind']})")
    elif c_att:
        conf = "likely" if max(RANK[e["confidence"]] for e in c_att) >= 2 else "inferred"
        kinds = sorted({e["kind"] for e in c_att})
        why.append(f"company at show ({', '.join(kinds)})")
    else:
        conf = "inferred"
    s += {"confirmed": 30, "likely": 18, "inferred": 5}[conf]
    if company_window_count >= min_shows:
        s += 15
        why.append(f"company exhibits at {company_window_count} shows/yr")
    person_shows = {e["show_id"] for e in p_att if e["show_id"]}
    if len(person_shows) >= 2:
        s += 15
        why.append(f"regular visitor ({len(person_shows)} shows)")
    if person["linkedin_url"]:
        s += 5
    if person["email"]:
        s += 5 if person.get("email_status") in ("verified", None) else 2
    flag = person.get("profile_flag") or ""
    if "outside target geo" in flag:
        s -= 25
        why.append(flag)
    if "now at" in flag:
        s -= 30
        why.append(flag)
    return max(min(s, 100), 0), conf, "; ".join(why)
