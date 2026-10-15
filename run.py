#!/usr/bin/env python3
"""Rinse FM -> Spotify playlists. Every command is resumable: re-run it and it continues
from the state saved in data/ (and checkpointed to git when a remote is configured).

  python3 run.py crawl [--full]          fetch new/changed Rinse FM tracklists from MixesDB
  python3 run.py stats                   summary of the state files
  python3 run.py auth-url                print the Spotify authorization link
  python3 run.py auth-code <redirected-url>   finish authorization, store refresh token
  python3 run.py match [--limit N] [--minutes M]   search Spotify for unmatched tracks
  python3 run.py sync                    create/update genre + monthly playlists
  python3 run.py export                  write unmatched / to-review CSVs into data/
  python3 run.py update [--minutes M]    crawl + match + sync + checkpoint (used by the weekly task)

Exit codes: 0 ok, 3 Spotify quota exhausted (resume later), 4 Spotify auth missing/expired.
"""
import csv
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rinse_spotify import mixesdb, spotify  # noqa: E402
from rinse_spotify.catalog import BUCKETS, build_catalog, priority_order  # noqa: E402
from rinse_spotify.common import DATA, ROOT, load_config, load_json, log, now_iso, save_json  # noqa: E402

RETRY_NONE_AFTER_DAYS = 90      # re-search "not on Spotify" tracks after this long
CHECKPOINT_EVERY_SEC = 600
JST = timezone(timedelta(hours=9))


def arg(name, default=None, cast=str):
    if name in sys.argv:
        return cast(sys.argv[sys.argv.index(name) + 1])
    return default


# ---------------- git checkpoint ----------------

def checkpoint(msg):
    """Commit data/ and push if a remote exists. Never fatal."""
    def git(*a):
        return subprocess.run(["git", "-C", ROOT, *a], capture_output=True, text=True, timeout=120)
    try:
        git("add", "data")
        if git("diff", "--cached", "--quiet").returncode == 0:
            return
        git("-c", "user.name=rinse-spotify-bot", "-c", "user.email=rinse-spotify-bot@users.noreply.github.com",
            "commit", "-q", "-m", msg)
        if git("remote").stdout.strip():
            r = git("push", "-q", "origin", "HEAD")
            if r.returncode != 0:
                log("git push failed (data is still committed locally):", r.stderr.strip()[:200])
        log("checkpoint saved:", msg)
    except Exception as e:  # noqa: BLE001
        log("checkpoint skipped:", e)


# ---------------- commands ----------------

def stats():
    cfg = load_config()
    mixes = load_json("mixes.json", {})
    use = [m for m in mixes.values() if m["tl"] in cfg["include_tracklist_status"] and m["tracks"]]
    print(f"mixes in window: {len(mixes)}; usable (with tracklist): {len(use)}; "
          f"track plays: {sum(len(m['tracks']) for m in use)}")
    cat = build_catalog()
    matches = load_json("matches.json", {})
    st = Counter(matches[k]["status"] for k in cat if k in matches)
    print(f"unique tracks: {len(cat)}; searched: {sum(st.values())}; "
          f"auto: {st['auto']}  review: {st['review']}  none: {st['none']}  error: {st['error']}")
    pls = load_json("playlists.json", {})
    for name, p in sorted(pls.items()):
        print(f"  {name}: {p.get('count')} tracks (synced {p.get('synced_at')})")


def match(limit=None, minutes=None):
    cat = build_catalog()
    matches = load_json("matches.json", {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETRY_NONE_AFTER_DAYS)).strftime("%Y-%m-%d")
    todo = [k for k in priority_order(cat)
            if k not in matches or matches[k]["status"] == "error"
            or (matches[k]["status"] == "none" and matches[k].get("at", "") < cutoff)]
    if limit:
        todo = todo[:limit]
    log(f"{len(todo)} tracks to search ({len(cat) - len(todo)} already done)")
    if not todo:
        return 0
    try:
        sp = spotify.Spotify()
    except spotify.AuthExpired as e:
        log("Spotify authorization missing/expired:", e)
        return 4
    t0 = last_ck = time.time()
    code = 0
    done = 0
    try:
        for k in todo:
            if minutes and time.time() - t0 > minutes * 60:
                log("time budget reached, stopping (progress saved)")
                break
            t = cat[k]
            try:
                res = spotify.match_track(sp, t["artist"], t["title"])
            except (spotify.QuotaExceeded, spotify.AuthExpired):
                raise
            except Exception as e:  # noqa: BLE001  one bad query must not stop the run
                res = {"status": "error", "err": str(e)[:200]}
            res["at"] = now_iso()[:10]
            res["q"] = f"{t['artist']} - {t['title']}"
            matches[k] = res
            done += 1
            if done % 25 == 0:
                save_json("matches.json", matches)
                st = Counter(matches[x]["status"] for x in todo[:done])
                log(f"searched {done}/{len(todo)}  auto {st['auto']}  review {st['review']}  none {st['none']}")
            if time.time() - last_ck > CHECKPOINT_EVERY_SEC:
                save_json("matches.json", matches)
                checkpoint(f"match progress: {done}/{len(todo)} searched")
                last_ck = time.time()
    except spotify.QuotaExceeded as e:
        log("Spotify quota exhausted, stopping (progress saved). Resume later with: python3 run.py match", e)
        code = 3
    except spotify.AuthExpired as e:
        log("Spotify authorization missing/expired:", e)
        code = 4
    finally:
        save_json("matches.json", matches)
        if getattr(sp, "rotated", False):
            log("NOTE: Spotify issued a new refresh token; update SPOTIFY_REFRESH_TOKEN (see ~/.rinse_spotify_secrets.json)")
    st = Counter(matches[x]["status"] for x in todo[:done])
    log(f"this run: searched {done}, auto {st['auto']}, review {st['review']}, none {st['none']}, "
        f"Spotify calls {sp.calls}")
    return code


def ordered_unique(uris):
    seen, out = set(), []
    for u in uris:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:10000]


def desired_playlists(cfg, cat, matches):
    prefix = cfg["playlist_prefix"]
    ok = {k: matches[k]["uri"] for k in cat if matches.get(k, {}).get("status") == "auto"}
    by_rank = priority_order(cat)
    out = {}
    for g in BUCKETS:
        uris = ordered_unique([ok[k] for k in by_rank if k in ok and cat[k]["genre"] == g])
        desc = (f"Rinse FM 节目近 8 年播过的 {g} 曲目，按被不同节目播放的次数排序。"
                f"歌单来自 MixesDB，每周自动更新。")
        out[f"{prefix} {g}"] = (uris, desc)
    start = cfg["monthly_playlists_from"]
    month = datetime.now(JST).strftime("%Y-%m")
    months = []
    y, mo = map(int, start.split("-"))
    while f"{y:04d}-{mo:02d}" <= month:
        months.append(f"{y:04d}-{mo:02d}")
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    for m in months:
        keys = [k for k in by_rank if k in ok and cat[k]["months"].get(m)]
        keys.sort(key=lambda k: -cat[k]["months"][m])  # stable: ties keep overall rank
        uris = ordered_unique([ok[k] for k in keys])
        desc = f"{m} 在 Rinse FM 节目中播出的曲目，按当月被播次数排序。歌单来自 MixesDB，每周自动更新。"
        out[f"{prefix} {m}"] = (uris, desc)
    return out


def sync():
    cfg = load_config()
    cat = build_catalog()
    matches = load_json("matches.json", {})
    state = load_json("playlists.json", {})
    want = desired_playlists(cfg, cat, matches)
    existing = None
    code = 0
    try:
        sp = spotify.Spotify()
        for name, (uris, desc) in want.items():
            pid = state.get(name, {}).get("id")
            if not pid:
                if not uris:
                    continue
                if existing is None:
                    existing = sp.my_playlists()
                pid = existing.get(name) or sp.create_playlist(name, desc, cfg["playlists_public"])
                state[name] = {"id": pid}
                save_json("playlists.json", state)
            try:
                current = sp.playlist_uris(pid)
            except spotify.HttpError as e:
                if e.status == 404:  # playlist deleted by the user: recreate
                    pid = sp.create_playlist(name, desc, cfg["playlists_public"])
                    state[name] = {"id": pid}
                    current = []
                else:
                    raise
            if current == uris:
                action = "unchanged"
            elif uris[:len(current)] == current:
                sp.add_items(pid, uris[len(current):])
                action = f"+{len(uris) - len(current)} appended"
            else:
                sp.replace_items(pid, uris)
                action = f"rewritten ({len(uris)} tracks)"
            if action != "unchanged":
                sp.update_details(pid, desc)
            state[name].update({"count": len(uris), "synced_at": now_iso()})
            save_json("playlists.json", state)
            log(f"{name}: {len(uris)} tracks, {action}")
    except spotify.QuotaExceeded as e:
        log("Spotify quota exhausted during sync; re-run sync later.", e)
        code = 3
    except spotify.AuthExpired as e:
        log("Spotify authorization missing/expired:", e)
        code = 4
    save_json("playlists.json", state)
    return code


def export():
    cat = build_catalog()
    matches = load_json("matches.json", {})
    rows = {"review": [], "none": []}
    for k in priority_order(cat):
        m = matches.get(k)
        if m and m["status"] in rows:
            t = cat[k]
            rows[m["status"]].append([t["plays"], t["last"], t["genre"] or "", t["artist"], t["title"],
                                      t.get("label") or "", m.get("artists", ""), m.get("name", ""),
                                      m.get("uri", ""), m.get("score", "")])
    hdr = ["plays", "last_played", "genre", "artist", "title", "label", "spotify_artists",
           "spotify_title", "spotify_uri", "score"]
    for name, rs in rows.items():
        path = os.path.join(DATA, f"{name}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(hdr)
            w.writerows(rs)
        log(f"wrote {path} ({len(rs)} rows)")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    code = 0
    if cmd == "crawl":
        mixesdb.crawl(full_rescan="--full" in sys.argv)
        checkpoint("crawl MixesDB")
    elif cmd == "stats":
        stats()
    elif cmd == "auth-url":
        print(spotify.authorize_url())
    elif cmd == "auth-code":
        rt = spotify.exchange_code(sys.argv[2])
        print("authorized. refresh token stored in ~/.rinse_spotify_secrets.json")
        print("SPOTIFY_REFRESH_TOKEN=" + rt)
    elif cmd == "match":
        code = match(limit=arg("--limit", None, int), minutes=arg("--minutes", None, float))
        checkpoint("match Spotify tracks")
    elif cmd == "sync":
        code = sync()
        checkpoint("sync playlists")
    elif cmd == "export":
        export()
    elif cmd == "update":
        mixesdb.crawl()
        checkpoint("weekly update: crawl")
        code = match(minutes=arg("--minutes", 60, float))
        checkpoint("weekly update: match")
        if code != 4:
            code = sync() or code
        checkpoint("weekly update: sync")
        stats()
    else:
        print(__doc__)
    sys.exit(code)


if __name__ == "__main__":
    main()
