# Transcript-capture benchmark

Compares two ways to get the spoken-text transcript of a YouTube trailer:

| Method | How it works | License posture |
|---|---|---|
| `youtube-transcript-api` | Scrapes YouTube's internal `timedtext` endpoint that powers the in-player CC button | Gray area. Personal/research use is widely tolerated; commercial republishing is not. YouTube has broken this endpoint in the past. |
| Whisper (via `faster-whisper`) | Downloads audio with `yt-dlp`, transcribes locally with a Whisper model | Worse posture: `yt-dlp` explicitly violates YouTube ToS. Output quality usually beats YT's auto-captions. |

Neither path uses the official YouTube Data API. The official `captions.download` endpoint requires OAuth as the channel owner, which is not available for studio-owned trailer channels.

## Fixed test set

`trailers.json` — 5 English trailers from the production `movie_trailers` dataset, spanning:

- Short (31s teaser) → long (150s international trailer)
- Tiny indie (103 views) → blockbuster A24 release (9M views)
- Several official-studio uploads + one self-published indie

## Dependencies

System:
- `ffmpeg` (used by yt-dlp + faster-whisper). On macOS: `brew install ffmpeg yt-dlp`.

Python (installed inline via `uv run --with ...`):
- `youtube-transcript-api`
- `yt-dlp` (also as Python lib)
- `faster-whisper` (CTranslate2 backend; ~150 MB for the `base` model, downloaded on first run)

## Run

```sh
cd benchmarks/transcripts
uv run --with youtube-transcript-api --with yt-dlp --with faster-whisper python run_benchmark.py
```

First run downloads:
- ~150 MB Whisper `base` model
- ~5 × 1–3 MB of audio (one m4a per trailer)

Subsequent runs reuse both. Total wall time on Apple Silicon: roughly 30 s for the `yta` pass + 20–60 s for the Whisper pass.

## Output

| File | What |
|---|---|
| `results.json` | Per-trailer numbers: success flag, latency, char/word counts, Jaccard similarity, error class |
| `results.md` | Human-readable summary table |
| `out/<video_id>.yta.txt` | Transcript text from youtube-transcript-api |
| `out/<video_id>.whisper.txt` | Transcript text from Whisper |
| `audio/<video_id>.m4a` | Downloaded audio (safe to delete; cached for re-runs) |

## What you'll learn from this benchmark

- **Coverage**: how often each method succeeds on official studio uploads vs. small channels (some studios disable captions; auto-generated ones are not always English).
- **Latency**: yta is typically <1 s/video; Whisper is bound by audio download + GPU/CPU transcription speed.
- **Quality**: Jaccard ≈ 0.7+ when both transcripts agree at the word level. Big divergences usually mean one of them caught dialog the other missed (Whisper) or hallucinated through music (Whisper's known weakness).
- **Cost trajectory**: yta is free; Whisper at scale ≈ $0.006/min on the OpenAI API, or free on local hardware.

## Switching Whisper model size

Edit `WHISPER_MODEL` near the top of `run_benchmark.py`:
`tiny` → `base` → `small` → `medium` → `large-v3` → `distil-large-v3`. Models are auto-downloaded on first use.
