# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this codebase is

A daily pipeline that captures YouTube trailer metadata for upcoming movies and TV shows worldwide.
Discovery is sourced from TMDB; metadata (stats + top-30 comments) is collected from the YouTube
Data API v3; storage is BigQuery on GCP. Designed to run as a Cloud Run Job triggered by
Cloud Scheduler.

## Architecture in one diagram

```
Cloud Scheduler ──daily 14:00 UTC──▶ Cloud Run Job (mt run-daily)
                                           │
                          phase order: discover_movies →
                          discover_tv → stats → comments
                                           │
                       TMDB ──┐             ├──▶ BigQuery (MERGE)
                              ↓             │
                       YouTube Data API ────┘
```

- **Discover** (movies + TV) is TMDB-only, **zero YouTube quota**. Each TV show's upcoming
  seasons anchor the trailer-tracking windows; series-level trailers attach to the most-imminent
  upcoming season. Each TMDB details call uses `append_to_response=external_ids,credits,watch/providers` so credits and watch_providers come for free — no extra round-trips.
- **Stats** runs every day for every `tracking_status='active'` trailer. Batched 50 per
  `videos.list` call (parts: `statistics,snippet,contentDetails,status`). Missing IDs in the
  response are marked `tracking_status='unavailable'`. First-sighting trailer technicals
  (duration, definition, embeddable, region_blocked, thumbnail, description) are written via
  COALESCE so they never overwrite existing values.
- **Comments** captures top-30 by relevance **twice per trailer in its lifetime**: at first
  discovery, then on the day before `tracking_end_date`. `comments_*_captured_at` columns on
  `trailers` are the idempotency guard — once set, the pipeline never re-captures.
- Tracking window: trailer `published_at` → movie `release_date` (or TV season `air_date`),
  plus a `tracking_grace_days` buffer.

## Key quota math (default YouTube quota = 10K/day)

- `videos.list`: 1 unit per call, up to 50 IDs.
- `commentThreads.list`: 1 unit per call.
- Per-day spend ≈ `(active / 50) + new_discoveries + entering_pre_release_window`.
- With ~5–10k active trailers, daily spend is typically 1–2k units. Massive headroom.

## Build / test / run

```sh
uv sync                                   # install
uv run pytest                             # unit tests (respx-mocked HTTP)
uv run ruff check src tests               # lint
uv run mt run-daily --verbose             # full run (needs env vars + BQ)
uv run mt run-daily --dataset=movie_trailers_dev --limit=20 --verbose   # dry-ish run
```

Single phase: `--skip-movies --skip-tv --skip-stats --skip-comments` flags compose.

Digest email (read-only against BQ):

```sh
uv run mt send-digest --period=week --dry-run --out /tmp/digest.html   # preview
uv run mt send-digest --period=week                                    # send
uv run mt send-digest --period=month                                   # later cadence
```

## Code layout

- `src/movie_trailers/clients/` — TMDB, YouTube, BigQuery clients (HTTP retries via tenacity).
- `src/movie_trailers/pipeline/` — one module per phase; `_common.py` holds shared filters.
- `src/movie_trailers/digest/` — read-only BQ queries + HTML render + SMTP send for the weekly/monthly digest email (`mt send-digest`). No writes; safe to run anytime.
- `src/movie_trailers/models.py` — pydantic row types that map 1:1 to BigQuery tables.
- `src/movie_trailers/cli.py` — `mt run-daily` and `mt send-digest` Typer entrypoints.
- `schemas/bigquery.sql` — DDL with `${DATASET}` placeholder.

## BigQuery write pattern

All writes go through one of two helpers on `BigQueryClient`:
- `merge_rows(...)` — load rows into a temp `_staging_<table>_<uuid>` table, then `MERGE` into target. Idempotent.
- `update_from_dicts(...)` — same staging mechanism but issues an `UPDATE … FROM staging` (for partial column updates like `channel_id`, `last_collected_at`, status flips).

Don't add ad-hoc `INSERT` queries — they break idempotency on re-runs.

## Non-obvious things to know

- **`trailers` is polymorphic.** A row is either `(content_kind='movie', movie_tmdb_id=...)` OR `(content_kind='tv', tv_tmdb_id=..., tv_season_number=...)`. Exactly one branch is set. The `trailer_stats_daily` and `trailer_comments_snapshots` tables key only on `youtube_video_id` so they work for both.
- **Trailer filter** is Teaser + Trailer + Official Trailer (the last is a name-based promotion within `type=Trailer`). Clips/Featurettes/Behind-the-Scenes are dropped at discovery.
- **TMDB `release_date` and TV `air_date` drift.** Discover re-runs every day and recomputes `tracking_end_date`, so a postponed release keeps its trailers in the active set.
- **YouTube quota resets at midnight Pacific Time** — the schedule (14:00 UTC) is chosen to be well clear of that boundary.
- **Comments order=`relevance` is non-deterministic.** The `rank` column on `trailer_comments_snapshots` reflects YouTube's opinion at snapshot time — it's not stable across captures.
- **`credits` table dedupes by `(tmdb_id, content_kind, credit_kind, person_id)`.** A crew person who holds multiple key jobs (e.g. Director + Writer) gets one row with `job = "Director, Writer"` (comma-joined). Cast is top-10 by `order`; crew is filtered to Director/Writer/Screenplay/Producer/Executive Producer/Creator.
- **Derived metrics live in a view, not a table.** `vw_trailer_daily_metrics` computes per-day Δviews / Δlikes / engagement ratios on the fly via window functions over `trailer_stats_daily`. API consumers should query the view, not the raw table.
- **TMDB image paths are stored raw**, not as full URLs. `poster_path` / `backdrop_path` / `logo_path` / `profile_path` are all relative paths like `/abc.jpg`. Build URLs with `https://image.tmdb.org/t/p/<size><path>` at the API layer so consumers can pick their own size.

## What's deferred (not implemented yet)

- `box_office` phase (TMDB budget/revenue post-theatrical) — table exists in DDL; pipeline module not yet built.
- Per-country revenue breakdown (would need a paid source).
- Secret Manager (env vars are passed via `--set-env-vars` on the Cloud Run Job).
- Terraform / IaC.
