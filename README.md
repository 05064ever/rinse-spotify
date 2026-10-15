# Rinse FM → Spotify playlists

Builds Spotify playlists from Rinse FM radio-show tracklists published on MixesDB.

- **Source:** every MixesDB page in `Category:Rinse FM` dated on/after `since` in `config.json`
  whose tracklist is marked complete or incomplete.
- **Genre playlists:** `Rinse FM · <Genre>` — MixesDB style tags of each show, grouped into
  12 buckets (`rinse_spotify/catalog.py`). Each track goes to the bucket it is most associated
  with across all shows that played it. Ordered by how many different shows played it.
- **Monthly playlists:** `Rinse FM · YYYY-MM` — tracks played in shows aired that month.
- Only confident Spotify matches (`auto`) are added. `python3 run.py export` writes
  `data/review.csv` (uncertain matches) and `data/none.csv` (not found on Spotify).

## Resuming after an interruption

All progress lives in `data/` and is committed to git as it goes. To continue, in any session:

```bash
git clone <this repo> && cd rinse-spotify     # or: git pull
python3 run.py stats                          # see where things stand
python3 run.py match                          # continue searching Spotify (skips done tracks)
python3 run.py sync                           # bring playlists up to date
```

Exit code 3 = Spotify quota exhausted (just run again later); 4 = authorization missing/expired.

## Credentials (never committed)

Read from environment variables, falling back to `~/.rinse_spotify_secrets.json`:
`SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN`.

Spotify refresh tokens expire six months after authorization. To renew:
`python3 run.py auth-url` → open the link, approve → `python3 run.py auth-code '<redirected URL>'`,
then put the printed `SPOTIFY_REFRESH_TOKEN` into the cloud environment's variables.

## Weekly update

`python3 run.py update` = crawl (only new/changed MixesDB pages) → match new tracks →
sync playlists → commit & push `data/`.

Stdlib-only Python 3, no installs needed.
