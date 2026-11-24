"""Sonnet decides who fits the search's personas, from free-text LinkedIn headlines and comments (show posts).

Keyword matching on headlines misses real fits ('Deputy Plant Manager' for a buyer persona) and lets wrong ones in
('outsourcing' for 'sourcing', a social-media manager whose headline says 'sales'). One tool-less Sonnet call per
BATCH people reads each person's headline and own words and answers, per person:
  group     the persona code they fit, null when none, "unclear" when the headline says too little ('Manager')
  role      their current job title in a few words      company   their current employer, or null
  at_show   from their OWN text only: "visited" (they met someone / were at a stall as a visitor),
            "exhibited" (they staffed a stand / invite people to their stand), or "none"
Answers are cached per search, person and persona version (search_cache 'persona_judge'), so a re-run asks nothing
twice. When Sonnet can't be reached, the caller's fallback (keyword titles + wording patterns) decides instead.
"""
import hashlib
import json
import re

from . import db

BATCH = 50
CACHE = "persona_judge"
AT_SHOW = ("visited", "exhibited", "none")


def persona_brief(ind, icps):
    """What Sonnet needs to know about the personas: the user's description and each group's definition."""
    lines = []
    for i in icps:
        lines.append(f'- {i["code"]}: {i.get("name") or ""}. {i.get("description") or ""} Typical titles: '
                     f'{", ".join((i.get("titles") or i.get("title_keywords") or [])[:10])}. Never: '
                     f'{", ".join(i.get("exclude_keywords") or []) or "-"}.')
    return f"The user's description: {(ind['persona_text'] or ind['name'] or '').strip()[:1200]}\nGroups:\n" + \
        "\n".join(lines)


def version(ind, icps):
    return hashlib.sha1((persona_brief(ind, icps)).encode()).hexdigest()[:12]


def key(c):
    """One verdict per person AND what was judged: the same person commenting on two posts, or with a new headline
    (e.g. read from their profile), is judged again."""
    who = (c.get("linkedin_url") or c.get("name") or "").split("?")[0].lower()
    what = hashlib.sha1(f'{c.get("headline") or ""}|{c.get("text") or ""}'.encode()).hexdigest()[:10]
    return f"{who}|{what}"


def _prompt(brief, batch, show_word):
    rows = []
    for n, c in enumerate(batch, 1):
        own = (c.get("text") or "").strip().replace("\n", " ")[:400]
        rows.append(f'{n}. name: {c.get("name")} | headline: {(c.get("headline") or "").strip()[:300]}'
                    + (f' | {c["how"]}: "{own}"' if own else f' | {c.get("how") or "reacted to a post"}')
                    + (f' | post by {c["post_author"]}' if c.get("post_author") else ""))
    return f"""You sort LinkedIn people for a market-research tool that looks for interviewees. They appeared around
LinkedIn posts about a trade show ({show_word}).

{brief}

For EACH person below decide:
- "group": the code of the group whose people this person is (their current job and seniority fit the group's
  description and typical titles; the company rule is checked elsewhere, ignore it). null if they fit no group
  (students, recruiters, marketers when no group wants marketers, unrelated jobs). "unclear" only when the headline
  is too vague to tell (e.g. just "Manager") or empty.
- "role": their current job title in at most 8 words, from the headline; null if none.
- "company": their current employer named in the headline; null if none (never guess).
- "at_show": judged ONLY from the person's own words quoted after their headline: "visited" if they say they met
  someone, talked at a stall/booth, or visited the show as a visitor; "exhibited" if they staffed a stand or invite
  people to their own stand; otherwise "none". Generic praise ("congrats", "great to see", "nice post") is "none".
  A reaction without words is always "none".

Return ONLY a JSON array, one object per person in order: [{{"n": 1, "group": ..., "role": ..., "company": ...,
"at_show": ...}}, ...]

PEOPLE:
""" + "\n".join(rows)


def _parse(out, batch, codes):
    m = re.search(r"\[.*\]", out or "", re.S)
    try:
        rows = json.loads(m.group(0)) if m else None
    except ValueError:
        rows = None
    if not isinstance(rows, list):
        return None
    got = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            n = int(r.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= len(batch):
            continue
        g = r.get("group")
        g = g if g in codes or g == "unclear" else None
        at = r.get("at_show") if r.get("at_show") in AT_SHOW else "none"
        if not (batch[n - 1].get("text") or "").strip():
            at = "none"   # a reaction has no words to judge
        role = (str(r.get("role")).strip()[:90] or None) if r.get("role") else None
        comp = (str(r.get("company")).strip()[:80] or None) if r.get("company") else None
        got[n - 1] = {"group": g, "role": role, "company": comp, "at_show": at}
    return got if len(got) >= len(batch) * 0.8 else None   # a cut-off answer is retried by the fallback


def judge(con, ind, icps, candidates, show_word="a trade show", log=print, ask=None):
    """-> ({key: verdict}, how): how = 'sonnet', 'cache' or 'fallback:<n>' (n people Sonnet didn't answer for).
    ask(prompt) -> text, defaults to a tool-less Sonnet call (tests pass their own)."""
    if ask is None:
        from .pipeline import _claude_text

        def ask(p):
            return _claude_text(p, "sonnet", "low", timeout=400)
    brief, ver = persona_brief(ind, icps), version(ind, icps)
    codes = {i["code"] for i in icps}
    out, todo = {}, []
    for c in candidates:
        k = key(c)
        row = con.execute("SELECT response FROM search_cache WHERE provider=? AND query=? AND params=?",
                          (CACHE, f'{ind["id"]}:{k}', ver)).fetchone()
        if row:
            out[k] = json.loads(row["response"])
        elif k not in {key(t) for t in todo}:
            todo.append(c)
    missed = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        log(f"  Sonnet is checking {len(batch)} people against the personas")
        got = _parse(ask(_prompt(brief, batch, show_word)), batch, codes)
        if got is None:
            missed += len(batch)
            continue
        for n, v in got.items():
            k = key(batch[n])
            out[k] = v
            con.execute("INSERT OR REPLACE INTO search_cache VALUES (?,?,?,?,?)",
                        (CACHE, f'{ind["id"]}:{k}', ver, json.dumps(v), db.now()))
        missed += len(batch) - len(got)
        con.commit()
    how = f"fallback:{missed}" if missed else ("sonnet" if todo else "cache")
    return out, how
