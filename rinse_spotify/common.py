"""Shared helpers: paths, atomic JSON state, polite HTTP, text normalisation."""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(ROOT, "config.json")
SECRETS_PATH = os.path.expanduser("~/.rinse_spotify_secrets.json")  # never committed

USER_AGENT = "RinseSpotifyPlaylists/1.0 (personal playlist project; low request rate)"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(name, default):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(name, obj):
    """Atomic write so an interruption never leaves a half-written state file."""
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


class HttpError(Exception):
    def __init__(self, status, body, headers=None):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.headers = headers or {}


def http(method, url, params=None, data=None, headers=None, json_body=None, timeout=40, retries=4):
    """Small urllib wrapper with retries on network errors / 5xx. Raises HttpError otherwise."""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            if 500 <= e.code < 600 and attempt < retries:
                last = HttpError(e.code, txt, dict(e.headers))
                time.sleep(5 * (attempt + 1))
                continue
            raise HttpError(e.code, txt, dict(e.headers))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise last


# ---------- text normalisation (used for de-duplication and matching) ----------

_FEAT = re.compile(r"\s*[\(\[]?\b(feat\.?|ft\.?|featuring|with)\b.*?(?=[\)\]]|$)[\)\]]?", re.I)


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s):
    s = strip_accents(s or "").lower()
    s = s.replace("&", " and ").replace("’", "'")
    s = re.sub(r"[^\w ]+", " ", s).replace("_", " ")  # keep non-Latin letters (JP, CN, Cyrillic...)
    return re.sub(r"\s+", " ", s).strip()


def split_artists(s):
    s = re.sub(r"\b(feat\.?|ft\.?|featuring)\b", ",", s or "", flags=re.I)
    parts = re.split(r",|&| and | x | vs\.? | b2b ", s, flags=re.I)
    return [norm(p) for p in parts if norm(p)]


def track_key(artist, title):
    """Stable key for one recording (artist + full title incl. remix name)."""
    main = split_artists(artist)
    first = main[0] if main else norm(artist)
    return f"{first}|{norm(title)}"


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)
