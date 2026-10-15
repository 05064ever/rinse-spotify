"""Crawl MixesDB (MediaWiki API) for Rinse FM mixes and parse their tracklists.

Resumable: every fetched batch is written to data/mixes.json immediately, and a page
is only (re)fetched when its MixesDB revision id differs from the one we stored.
"""
import re
import time

from .common import HttpError, http, load_config, load_json, log, now_iso, save_json

API = "https://www.mixesdb.com/w/api.php"
DELAY = 1.2  # seconds between MixesDB requests (be polite)

NON_STYLE = {"Audio Players", "Players", "SoundCloud", "Mixcloud", "YouTube", "Rinse FM",
             "Radio Shows", "Podcasts", "Video Players", "Hearthis", "Bandcamp"}


def api(params):
    p = {"format": "json", "formatversion": "1"}
    p.update(params)
    time.sleep(DELAY)
    return http("GET", API, params=p)


def title_date(title):
    m = re.match(r"^(\d{4})(?:-(\d{2}|XX))?(?:-(\d{2}|XX))?\s", title)
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    mo = mo if mo and mo != "XX" else None
    d = d if d and d != "XX" else None
    return "-".join(x for x in (y, mo, d) if x)


def in_window(date, since):
    """date may be YYYY, YYYY-MM or YYYY-MM-DD; keep only if certainly >= since."""
    if not date:
        return False
    if len(date) == 10:
        return date >= since
    if len(date) == 7:
        return date > since[:7] or (date == since[:7] and since[8:] == "01")
    return date > since[:4]


def list_category(category, start_prefix):
    """Yield (title, pageid, lastrevid) for every page in the category from start_prefix on."""
    params = {"action": "query", "generator": "categorymembers", "gcmtitle": f"Category:{category}",
              "gcmlimit": "500", "gcmsort": "sortkey", "gcmstartsortkeyprefix": start_prefix,
              "gcmtype": "page", "prop": "info"}
    cont = {}
    while True:
        d = api({**params, **cont})
        for p in d.get("query", {}).get("pages", {}).values():
            yield p["title"], p["pageid"], p.get("lastrevid")
        if "continue" not in d:
            break
        cont = d["continue"]


def fetch_pages(titles):
    """Fetch wikitext + categories for up to 50 titles, merging API continuations."""
    out = {}
    params = {"action": "query", "titles": "|".join(titles), "prop": "revisions|categories",
              "rvprop": "content|ids", "rvslots": "main", "cllimit": "max"}
    cont = {}
    while True:
        d = api({**params, **cont})
        for p in d.get("query", {}).get("pages", {}).values():
            rec = out.setdefault(p["title"], {"pageid": p.get("pageid"), "cats": [], "text": None, "rev": None})
            rec["cats"] += [c["title"].split(":", 1)[1] for c in p.get("categories", [])]
            revs = p.get("revisions")
            if revs:
                rec["rev"] = revs[0].get("revid")
                rec["text"] = revs[0].get("slots", {}).get("main", {}).get("*")
        if "continue" not in d:
            break
        cont = d["continue"]
    return out


# ---------------- tracklist parsing ----------------

_UNKNOWN = re.compile(r"^(\?+|id|unknown|untitled|unreleased|n/?a|-+)$", re.I)


def clean_wiki(s):
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", s)        # [[link|text]] -> text
    s = re.sub(r"\[https?://\S+\s*([^\]]*)\]", r"\1", s)          # [url text] -> text
    s = re.sub(r"\{\{[^}]*\}\}", "", s)                           # templates
    s = re.sub(r"<[^>]+>", "", s)                                  # html tags
    s = s.replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", s).strip()


def parse_line(line):
    s = line.strip()
    if not s or s.startswith(("==", "[[Category", "{{", "|", "}}")):
        return None
    if not re.match(r"^[#*\d\[]", s):
        return None
    s = re.sub(r"^[#*:]+\s*", "", s)
    s = re.sub(r"^\d{1,3}[.)]\s+", "", s)                          # "12. " numbering
    s = re.sub(r"^\[[\d:?\-\s]*\]\s*", "", s)                       # "[05]" / "[1:02:03]" cue
    s = clean_wiki(s)
    s = re.split(r"\s+w/\s+", s)[0]                                  # drop "w/ acapella" overlays
    label = None
    m = re.search(r"\s*\[([^\[\]]*)\]\s*$", s)
    if m:
        label = m.group(1).strip() or None
        s = s[:m.start()].strip()
    if " - " not in s:
        return None
    artist, title = s.split(" - ", 1)
    artist, title = artist.strip(" -"), title.strip(" -")
    if not artist or not title or _UNKNOWN.match(artist) or _UNKNOWN.match(title):
        return None
    if re.fullmatch(r"(id\s*-?\s*)+", f"{artist} {title}", re.I) or "?" == title[:1]:
        return None
    return [artist, title, label]


def parse_tracklist(text):
    if not text:
        return []
    m = re.search(r"==\s*Tracklist\s*==(.*?)(?=\n==[^=]|\[\[Category:|\Z)", text, re.S | re.I)
    body = m.group(1) if m else ""
    tracks = []
    for line in body.splitlines():
        t = parse_line(line)
        if t:
            tracks.append(t)
    return tracks


def crawl(full_rescan=False):
    cfg = load_config()
    since = cfg["since"]
    mixes = load_json("mixes.json", {})
    log(f"listing Category:{cfg['mixesdb_category']} from {since[:4]} ...")
    listing = []
    for title, pageid, rev in list_category(cfg["mixesdb_category"], since[:4]):
        date = title_date(title)
        if in_window(date, since):
            listing.append((title, pageid, rev, date))
    log(f"{len(listing)} pages in window since {since}")
    todo = [t for t in listing if full_rescan or mixes.get(t[0], {}).get("fetched_rev") != t[2]]
    log(f"{len(todo)} new or changed pages to fetch ({len(listing) - len(todo)} unchanged)")
    changed = []
    for i in range(0, len(todo), 50):
        batch = todo[i:i + 50]
        try:
            pages = fetch_pages([t[0] for t in batch])
        except HttpError as e:
            log("MixesDB error, stopping here (progress saved):", e)
            break
        for title, pageid, rev, date in batch:
            p = pages.get(title)
            if not p:
                continue
            cats = p["cats"]
            tl = next((c.split(": ", 1)[1] for c in cats if c.startswith("Tracklist:")), "unknown")
            styles = sorted(c for c in cats if c not in NON_STYLE and not c.startswith("Tracklist")
                            and not re.fullmatch(r"\d{4}", c))
            new = {"pageid": pageid, "date": date, "fetched_rev": p["rev"] or rev, "tl": tl,
                   "cats": styles, "tracks": parse_tracklist(p["text"]), "fetched_at": now_iso()}
            old = mixes.get(title)
            mixes[title] = {**(old or {}), **new}
            changed.append(title)
        save_json("mixes.json", mixes)  # checkpoint after every batch
        log(f"fetched {min(i + 50, len(todo))}/{len(todo)}")
    # forget pages that left the category / window
    keep = {t[0] for t in listing}
    for t in [t for t in mixes if t not in keep]:
        del mixes[t]
    save_json("mixes.json", mixes)
    return changed
