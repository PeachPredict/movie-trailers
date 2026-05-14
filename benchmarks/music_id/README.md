# Music-ID benchmark (Bollywood focus)

Tests whether free / freemium audio-identification services can extract the
song(s) used inside a YouTube trailer. Bollywood is the primary target —
song promotion is a huge part of how Indian films market themselves, and the
song titles + lyrics matter to downstream analytics far more than the spoken
dialogue.

## Question this study answers

> Given a trailer audio track, can a free/cheap service identify the music
> cues inside it — and how does coverage differ between English-market
> trailers and Bollywood/regional-Indian trailers?

## Methods compared

| Method | License posture | Cost | Bollywood prior |
|---|---|---|---|
| **shazamio** (unofficial Shazam Python client) | Gray — uses Shazam's protocol without an API agreement; fine for research, not for production redistribution | Free, no key | Best — Shazam's catalog covers T-Series / YRF / Saregama / Sony Music India well |
| **AudD** (`api.audd.io/recognize`) | Commercial API with free tier | Free demo token `"test"` ≈ very limited; signup ~$5/mo | Decent — pulls from multiple licensed sources including Indian aggregators |
| **AcoustID + Chromaprint** | Fully open source, MusicBrainz-backed | Free with API key | Weak — MusicBrainz Bollywood coverage is patchy; works best on commercial studio masters that match the album exactly |

All three are tested against the **same audio windows** of the same trailers,
so hit rates are directly comparable.

## Sliding-window approach

Trailers usually contain more than one music cue (e.g. a quiet intro score, a
hook from the title song, a transition into the action montage). A single
fingerprint of the full trailer is rarely useful. The benchmark splits each
audio into **non-overlapping 15-second windows** and queries each service per
window. Aggregate hit rates and the set of unique tracks identified per
trailer are reported.

## Bollywood-specific caveats

- Trailers frequently use **custom trailerized cuts** — re-orchestrated /
  shortened versions of the song that don't fingerprint to the album master.
  Even Shazam misses these.
- The **background score** (instrumental cues by the film's music director)
  is almost never identifiable — it's not released as a separate track until
  later, if ever.
- Realistic identification targets are the **item songs / promo songs / title
  tracks** that are released as singles ahead of the film. These are the
  commercially important ones anyway.
- If a song is identified, the YouTube transcript (already captured by the
  pipeline) often contains its lyrics — a fallback path is to fuzzy-match
  transcript lines against song-lyric DBs (out of scope for this study).

## Picking trailers

`trailers.json` ships empty. Populate it by running the picker, which queries
your BigQuery dataset for Hindi/Tamil/Telugu/Malayalam/Kannada/Punjabi
trailers with the highest view counts:

```sh
cd benchmarks/music_id
uv run --with google-cloud-bigquery python pick_trailers.py --limit 10
```

Or hand-edit `trailers.json` — the schema mirrors `benchmarks/transcripts/trailers.json`.

## Dependencies

System:
- `ffmpeg` (slicing audio into windows)
- `yt-dlp` (audio download)
- `fpcalc` from chromaprint, **only if you enable the AcoustID method**: `brew install chromaprint`

Python (installed inline via `uv run --with ...`):
- `yt-dlp` (also as library)
- `shazamio` (Shazam method)
- `pyacoustid` (AcoustID method)
- `httpx` (AudD method)

## API keys

Both AcoustID and AudD need keys for non-trivial usage. The benchmark reads them
from env and **silently skips** the method if the key is missing, so you can
run a Shazam-only pass without any setup.

- `ACOUSTID_API_KEY` — free, signup at <https://acoustid.org/new-application>
- `AUDD_API_TOKEN` — free demo token `"test"` works for ~10 calls/day; signup at <https://dashboard.audd.io/> for more

## Run

```sh
cd benchmarks/music_id
export ACOUSTID_API_KEY=...          # optional
export AUDD_API_TOKEN=...            # optional; defaults to "test"
uv run --with yt-dlp --with shazamio --with pyacoustid --with httpx python run_benchmark.py
```

Re-uses cached audio (`audio/<video_id>.m4a`) and sliced windows
(`windows/<video_id>/w<NN>.m4a`) across runs.

## Output

| File | What |
|---|---|
| `results.json` | Per-trailer, per-window, per-method: match info, latency, errors |
| `results.md` | Summary table: hit rates and unique-tracks-per-trailer for each method |
| `audio/<video_id>.m4a` | Cached full audio download |
| `windows/<video_id>/w<NN>.m4a` | Cached 15s slices |

## What you'll learn

- Whether Shazam (via shazamio) is good enough on its own for Bollywood
  trailer music identification, or whether a paid AudD plan is worth it.
- How many windows of a typical trailer contain *identifiable* music vs.
  custom score / dialog / silence.
- Whether AcoustID is worth keeping in the toolkit at all for this content
  type, or whether MusicBrainz Bollywood coverage is too thin.
- Order-of-magnitude latency per service (Shazam is typically 1–3s/window;
  AudD adds an HTTP round-trip; AcoustID adds local `fpcalc` time).
