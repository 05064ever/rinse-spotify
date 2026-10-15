"""Spotify Web API client (Development Mode, post-Feb-2026 endpoints) + track matching.

Endpoints used: /search (limit<=10), POST /me/playlists, GET /me/playlists,
GET/POST/PUT /playlists/{id}/items. Auth: authorization-code flow with refresh token.
"""
import base64
import difflib
import json
import os
import re
import secrets as pysecrets
import time
import urllib.parse

from .common import SECRETS_PATH, HttpError, http, load_config, log, norm, split_artists

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
AUTH_URL = "https://accounts.spotify.com/authorize"
REQUEST_GAP = 0.35  # seconds between Spotify calls


class QuotaExceeded(Exception):
    pass


class AuthExpired(Exception):
    pass


# ---------------- credentials ----------------

def load_secrets():
    """Environment variables first; the local secrets file (if any) overrides them, because it
    holds the newest refresh token when Spotify rotates it during a run."""
    s = {}
    for k in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN"):
        if os.environ.get(k):
            s[k] = os.environ[k].strip()
    if os.path.exists(SECRETS_PATH):
        with open(SECRETS_PATH) as f:
            s.update({k: v for k, v in json.load(f).items() if v})
    return s


def save_secrets(s):
    keep = {k: v for k, v in s.items() if k.startswith("SPOTIFY_")}
    fd = os.open(SECRETS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(keep, f)


def authorize_url():
    cfg, s = load_config(), load_secrets()
    q = {"client_id": s["SPOTIFY_CLIENT_ID"], "response_type": "code", "redirect_uri": cfg["redirect_uri"],
         "scope": cfg["scopes"], "state": pysecrets.token_urlsafe(8), "show_dialog": "true"}
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


def _basic(s):
    raw = f"{s['SPOTIFY_CLIENT_ID']}:{s['SPOTIFY_CLIENT_SECRET']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def exchange_code(code_or_url):
    cfg, s = load_config(), load_secrets()
    code = code_or_url
    if "code=" in code_or_url:
        code = urllib.parse.parse_qs(urllib.parse.urlparse(code_or_url).query)["code"][0]
    d = http("POST", TOKEN_URL, headers=_basic(s),
             data={"grant_type": "authorization_code", "code": code, "redirect_uri": cfg["redirect_uri"]})
    s["SPOTIFY_REFRESH_TOKEN"] = d["refresh_token"]
    save_secrets(s)
    return d["refresh_token"]


class Spotify:
    def __init__(self):
        self.s = load_secrets()
        missing = [k for k in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN")
                   if not self.s.get(k)]
        if missing:
            raise AuthExpired("missing credentials: " + ", ".join(missing))
        self.token, self.token_exp, self.last = None, 0, 0.0
        self.calls = 0

    def _refresh(self):
        try:
            d = http("POST", TOKEN_URL, headers=_basic(self.s),
                     data={"grant_type": "refresh_token", "refresh_token": self.s["SPOTIFY_REFRESH_TOKEN"]})
        except HttpError as e:
            if e.status in (400, 401):
                raise AuthExpired(f"refresh failed ({e.status}): {e.body[:200]}")
            raise
        self.token = d["access_token"]
        self.token_exp = time.time() + d.get("expires_in", 3600) - 60
        if d.get("refresh_token") and d["refresh_token"] != self.s["SPOTIFY_REFRESH_TOKEN"]:
            # Spotify rotated the refresh token: keep the new one locally and flag it.
            self.s["SPOTIFY_REFRESH_TOKEN"] = d["refresh_token"]
            save_secrets(self.s)
            self.rotated = True

    def req(self, method, path, params=None, body=None):
        for attempt in range(6):
            if not self.token or time.time() > self.token_exp:
                self._refresh()
            wait = REQUEST_GAP - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.calls += 1
            try:
                return http(method, API + path, params=params, json_body=body,
                            headers={"Authorization": f"Bearer {self.token}"})
            except HttpError as e:
                if e.status == 401:
                    self.token = None
                    continue
                if e.status == 429:
                    if "QUOTA_EXCEEDED" in e.body:
                        raise QuotaExceeded(e.body[:300])
                    ra = int(e.headers.get("Retry-After") or e.headers.get("retry-after") or 30)
                    if ra > 600:
                        raise QuotaExceeded(f"Retry-After {ra}s")
                    log(f"rate limited, sleeping {ra}s")
                    time.sleep(ra + 1)
                    continue
                if e.status == 403 and "premium" in e.body.lower():
                    raise AuthExpired("app owner needs an active Premium subscription: " + e.body[:200])
                raise
        raise HttpError(429, "gave up after repeated rate limiting")

    # ---------- playlists ----------
    def my_playlists(self):
        out, offset = {}, 0
        while True:
            d = self.req("GET", "/me/playlists", params={"limit": 50, "offset": offset})
            for p in d.get("items", []):
                if p:
                    out.setdefault(p["name"], p["id"])
            if not d.get("next"):
                return out
            offset += 50

    def create_playlist(self, name, description, public):
        d = self.req("POST", "/me/playlists", body={"name": name, "description": description, "public": public})
        return d["id"]

    def playlist_uris(self, pid):
        uris, offset = [], 0
        while True:
            d = self.req("GET", f"/playlists/{pid}/items",
                         params={"limit": 100, "offset": offset, "fields": "next,items(item(uri),track(uri))"})
            for it in d.get("items", []):
                t = it.get("item") or it.get("track") or {}
                if t.get("uri"):
                    uris.append(t["uri"])
            if not d.get("next"):
                return uris
            offset += 100

    def replace_items(self, pid, uris):
        self.req("PUT", f"/playlists/{pid}/items", body={"uris": uris[:100]})
        self.add_items(pid, uris[100:])

    def add_items(self, pid, uris):
        for i in range(0, len(uris), 100):
            self.req("POST", f"/playlists/{pid}/items", body={"uris": uris[i:i + 100]})

    def update_details(self, pid, description):
        self.req("PUT", f"/playlists/{pid}", body={"description": description})

    # ---------- search ----------
    def search(self, q):
        d = self.req("GET", "/search", params={"q": q, "type": "track", "limit": 10})
        return d.get("tracks", {}).get("items", [])


# ---------------- matching ----------------

VERSION_RX = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
NEUTRAL = re.compile(r"^(original( mix| version)?|extended( mix| version)?|club mix|album version|radio edit|"
                     r"edit|single( version| edit)?|short edit|(clean|explicit|dirty)( version| edit)?|main mix|"
                     r"remaster(ed)?( \d{4})?|\d{4} remaster(ed)?|mixed|vocal mix|instrumental)$")


def split_title(title):
    """'Song (X Remix) [Extended]' -> ('song', ['x remix', 'extended'])"""
    t = title.replace("–", "-")
    # drop featured-artist clauses, with or without brackets: "Song ft X", "Song (feat. X)"
    t = re.sub(r"\s*[\(\[]\s*(feat\.?|ft\.?|featuring)\s[^\)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(r"\s+(feat\.?|ft\.?|featuring)\s.*?(?=\s[\(\[-]|$)", "", t, flags=re.I)
    # Spotify style "Song - X Remix"
    parts = t.split(" - ")
    versions = [norm(v) for v in VERSION_RX.findall(t)]
    base = VERSION_RX.sub(" ", parts[0])
    if len(parts) > 1:
        versions += [norm(VERSION_RX.sub(" ", p)) for p in parts[1:]]
    versions = [v for v in versions if v and not v.startswith(("feat ", "ft ", "featuring "))]
    return norm(base), versions


def version_key(versions):
    """Return the distinguishing version text (remix/edit/vip/dub names), or '' for neutral."""
    keys = [v for v in versions if not NEUTRAL.match(v)]
    return " ".join(keys)


def sim(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def score_candidate(artist, title, cand):
    src_base, src_vers = split_title(title)
    c_base, c_vers = split_title(cand["name"])
    t_sim = sim(src_base, c_base)
    src_artists = split_artists(artist)
    cand_artists = [norm(a["name"]) for a in cand.get("artists", [])]
    a_sim = max((sim(a, b) for a in src_artists for b in cand_artists), default=0.0)
    sv, cv = version_key(src_vers), version_key(c_vers)
    if not sv and not cv:
        v_ok = 1.0
    elif sv and cv:
        v_ok = max(sim(sv, cv), 1.0 if set(sv.split()) <= set(cv.split()) else 0.0)
    elif sv and not cv:
        v_ok = 0.0          # wanted a remix, got the original
    else:
        v_ok = 0.3          # wanted original, got a remix
    return round(0.45 * t_sim + 0.35 * a_sim + 0.20 * v_ok, 3), t_sim, a_sim, v_ok


def _q_escape(s):
    return re.sub(r'["\[\]\(\)]', " ", s).strip()


def match_track(sp, artist, title):
    base_title = VERSION_RX.sub(" ", title).split(" - ")[0]
    first_artist = re.split(r",|&| feat\.? | ft\.? | x | vs\.? ", artist, flags=re.I)[0]
    queries = [f'track:"{_q_escape(base_title)}" artist:"{_q_escape(first_artist)}"',
               f"{_q_escape(first_artist)} {_q_escape(title)}"]
    best = None
    for q in queries:
        for c in sp.search(q):
            sc = score_candidate(artist, title, c)
            if not best or sc[0] > best[0][0]:
                best = (sc, c)
        if best and best[0][1] >= 0.9 and best[0][2] >= 0.85 and best[0][3] >= 0.9:
            break  # confident: skip the fallback query
    if not best:
        return {"status": "none"}
    (score, t_sim, a_sim, v_ok), c = best
    if t_sim >= 0.85 and a_sim >= 0.8 and v_ok >= 0.6:
        status = "auto"
    elif t_sim >= 0.7 and a_sim >= 0.6:
        status = "review"
    else:
        status = "none"
    return {"status": status, "uri": c["uri"], "name": c["name"],
            "artists": ", ".join(a["name"] for a in c.get("artists", [])),
            "score": score, "t": round(t_sim, 2), "a": round(a_sim, 2), "v": round(v_ok, 2)}
