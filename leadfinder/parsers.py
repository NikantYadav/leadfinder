"""Deterministic parsers for common exhibitor-list layouts found in fair guides / show catalogues.
Claude still reviews the output; these save it from hand-copying hundreds of rows.

hall-stall: "Company Name  <hall> <stall>" rows, where long names wrap and the hall/stall cell is vertically
centred on the name, e.g.
    Andritz Schuler India Private
    5 A103
    Limited
"""
import re

STALL_RE = re.compile(r"^(?P<name>.*?)\s*(?P<hall>\b\d{1,2}[A-Z]?)\s+(?P<stall>[A-Z]{1,2}\s?-?\s?\d{1,4}[A-Z]?)$")
_NOISE_CI = re.compile(r"^(hall|stall|company name|no\.?\s*no\.?|hall stall|exhibitors[’']? .*list.*|"
                       r"--- page \d+ ---|\d{1,3})$", re.I)
_NOISE_CS = re.compile(r"^([A-Z]|CMY|CM|MY|CY|K)$")  # alphabet index letters, print colour marks


class _Noise:
    @staticmethod
    def match(line):
        return _NOISE_CI.match(line) or _NOISE_CS.match(line)


NOISE_RE = _Noise


def parse_hall_stall(text, section_marker=None):
    rows = []
    if section_marker:
        # keep only pages containing the marker (e.g. "Exhibitors’ Main List")
        pages = text.split("--- page ")
        text = "\n".join("--- page " + p for p in pages if section_marker.lower() in p.lower())
    lines = [" ".join(l.split()) for l in text.splitlines()]
    buf, prev_after = [], 0
    for line in lines:
        if not line or NOISE_RE.match(line):
            if line.startswith("--- page") or (line and section_marker and section_marker.lower() in line.lower()):
                buf, prev_after = [], 0  # new page/section: no wrap across it
            continue
        m = STALL_RE.match(line)
        if not m:
            buf.append(line)
            continue
        # buffer = [after-lines of previous entry] + [before-lines of this entry]; centring => after == before
        give_back = min(prev_after, len(buf))
        if rows and give_back:
            rows[-1]["name"] += " " + " ".join(buf[:give_back])
        before = buf[give_back:]
        name_parts = before + ([m.group("name")] if m.group("name") else [])
        rows.append({"name": " ".join(name_parts).strip(), "hall": m.group("hall"),
                     "stall": m.group("stall").replace(" ", "")})
        prev_after = len(before) if not m.group("name") else len(before)
        if not m.group("name") and not before:
            prev_after = 0
        buf = []
    if rows and buf and prev_after:
        rows[-1]["name"] += " " + " ".join(buf[:prev_after])
    seen, out = set(), []
    for r in rows:
        key = (r["name"].lower(), r["stall"])
        if r["name"] and key not in seen:
            seen.add(key)
            out.append(r)
    return out
