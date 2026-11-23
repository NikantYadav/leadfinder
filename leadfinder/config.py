"""Paths and settings. Reads KEY=VALUE pairs from ./.env (no external dependency)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"            # downloaded pages: untrusted content, never executed
OUTPUT = ROOT / "output"
DB_PATH = DATA / "leads.db"
BROWSER_PROFILE = DATA / "browser-profile"


def _load_env():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0].split("\t#", 1)[0]  # allow inline comments
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def get(key, default=None):
    v = os.environ.get(key, default)
    return v if v not in ("", None) else default


OPENSERP_URL = get("OPENSERP_URL", "http://127.0.0.1:7000")
# Minimum seconds between OpenSERP Google queries; Google shows captchas when this is too fast.
OPENSERP_MIN_GAP = float(get("OPENSERP_MIN_GAP", "45"))
# Minutes to rest a provider after a captcha / rate limit.
TINYSEARCH_MIN_GAP = float(get("TINYSEARCH_MIN_GAP", "2.2"))  # 30 req/min limit per key
COOLDOWN_MINUTES = float(get("COOLDOWN_MINUTES", "30"))

for d in (DATA, RAW, OUTPUT):
    d.mkdir(parents=True, exist_ok=True)
