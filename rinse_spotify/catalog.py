"""Turn crawled mixes into a track catalog: play counts, dates, and a primary genre bucket."""
from collections import Counter, defaultdict

from .common import load_config, load_json, track_key

# MixesDB style category -> (playlist genre, weight). Anything not listed (artist names,
# "Various", players...) is ignored for genre purposes.
BUCKETS = {
    "House": ["House", "Deep House", "Tech House", "Deep Tech House", "Progressive House", "Acid House",
              "Euro House", "Vocal House", "Hard House", "Hip House", "House (Old School)"],
    "Techno": ["Techno", "Minimal", "Dub Techno", "Deep Techno", "Detroit Techno", "Acid", "EBM", "Trance",
               "Industrial"],
    "UK Garage & Funky": ["UK Garage", "2 Step", "Bassline", "UK Funky", "Speed Garage"],
    "Dubstep & Bass": ["Dubstep", ("Bass", 0.5)],
    "Grime": ["Grime"],
    "Hip Hop & R&B": ["Hip Hop", "R&B", "Trap", "Drill", "UK Drill"],
    "Jungle & Drum & Bass": ["Drum & Bass", "Jungle", "Hardcore"],
    "Breaks & Electro": ["Breakbeat", "Breaks", "Electro", "Broken Beat", "Juke", "Footwork", "Ghetto", "Rave"],
    "Disco, Funk & Soul": ["Disco", "Funk", "Soul", "Jazz", "Rare Groove", "Future Jazz", "Latin", "Boogie"],
    "Amapiano & Afro": ["Amapiano", "Afrobeat", "Afrobeats", "Gqom", "Afro House", "World Music"],
    "Dub & Dancehall": ["Dub", "Dancehall", "Reggae"],
    "Ambient & Experimental": ["Ambient", "Experimental", "Downtempo", "Electronica (Synthesizer)", "IDM",
                               "Leftfield", "Trip Hop", "Synthwave"],
}
STYLE_TO_BUCKET = {}
for bucket, styles in BUCKETS.items():
    for s in styles:
        name, w = (s if isinstance(s, tuple) else (s, 1.0))
        STYLE_TO_BUCKET[name] = (bucket, w)


def mix_buckets(cats):
    w = defaultdict(float)
    for c in cats:
        if c in STYLE_TO_BUCKET:
            b, x = STYLE_TO_BUCKET[c]
            w[b] = max(w[b], x)
    total = sum(w.values())
    return {b: x / total for b, x in w.items()} if total else {}


def build_catalog():
    cfg = load_config()
    mixes = load_json("mixes.json", {})
    cat = {}
    spellings = defaultdict(Counter)
    for mtitle, m in mixes.items():
        if m["tl"] not in cfg["include_tracklist_status"] or not m["tracks"]:
            continue
        mb = mix_buckets(m["cats"])
        seen = set()
        for artist, title, label in m["tracks"]:
            k = track_key(artist, title)
            if k in seen or k.startswith("|") or k.endswith("|"):
                continue
            seen.add(k)
            spellings[k][(artist, title)] += 1
            t = cat.setdefault(k, {"plays": 0, "first": m["date"], "last": m["date"], "label": label,
                                   "genre_w": defaultdict(float), "months": Counter(), "mixes": []})
            t["plays"] += 1
            t["first"] = min(t["first"], m["date"])
            t["last"] = max(t["last"], m["date"])
            t["label"] = t["label"] or label
            for b, x in mb.items():
                t["genre_w"][b] += x
            if len(m["date"]) >= 7:
                t["months"][m["date"][:7]] += 1
            t["mixes"].append(mtitle)
    for k, t in cat.items():
        (t["artist"], t["title"]), _ = spellings[k].most_common(1)[0]
        gw = t.pop("genre_w")
        t["genre"] = max(gw.items(), key=lambda kv: (kv[1], kv[0]))[0] if gw else None
    return cat


def priority_order(cat):
    """Most-played first, then most recent: the order we spend Spotify search quota in."""
    return sorted(cat, key=lambda k: (-cat[k]["plays"], tuple(-ord(c) for c in cat[k]["last"])))


def genre_counts(cat):
    return Counter(t["genre"] for t in cat.values())
