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
                          discover_tv → search_trailers → stats →
                          news → comments → excitement → transcripts →
                          predictions → box_office → sacnilk → wikipedia
                                           │
                       TMDB ──┐             ├──▶ BigQuery (MERGE)
                              ↓             │
                       YouTube Data API ────┤
                       OMDb (US gross) ─────┤
                       Sacnilk pages (IN) ──┤
                       Wikipedia infobox ───┤
                       GDELT GKG (BigQuery public dataset, news signal)
                       ANCINE CSV (BR admissions, `mt load-ancine`)
```

- **Discover** (movies + TV) is TMDB-only, **zero YouTube quota**. Each TV show's upcoming
  seasons anchor the trailer-tracking windows; series-level trailers attach to the most-imminent
  upcoming season. Each TMDB details call uses `append_to_response=external_ids,credits,watch/providers` so credits and watch_providers come for free — no extra round-trips.
- **Search** (`search_trailers`) is the YouTube fallback for upcoming movies that still have
  **no** trailer after discovery: Indian/Arabic titles (`SEARCH_LANGUAGES` / `SEARCH_COUNTRIES`,
  TMDB's blind spot) plus any title with TMDB popularity ≥ `SEARCH_MIN_POPULARITY` (default 3 —
  TMDB's post-2025 popularity scale puts the upcoming median near 1, so 3 ≈ top 5%).
  `search.list` costs 100 units, so it's rationed: one search per title per release
  checkpoint (`SEARCH_CHECKPOINTS_DAYS`, default 45/21/10/4 days out — max 400 units per
  title), a hard per-run cap (`SEARCH_MAX_UNITS`, default 2500), most-popular first, and
  any match stops all further searching for that title. Results are filtered hard
  (`evaluate_candidate`: title match with accent-folding, trailer word in ~20 languages,
  junk words incl. season/game/update/theme/short film, "- Topic"/"Edits" channels, off-year
  in the title, fan-made markers in description/tags — which also condemn the whole
  channel — and a 15–360s duration via one 1-unit `videos.list`). **Short titles** (≤2
  tokens, or 3 with a stopword) collide with games/TV/other films, so they must be
  confirmed by a top-8 cast / director / production-company name in the video's
  title+description+tags+channel, or come from a studio/streamer channel on
  `SEARCH_TRUSTED_CHANNELS`; non-Latin-script titles may instead carry an "official" marker. Every drop is logged with its reason in `trailer_search_log.rejected` — tune the
  filters from there, not by re-spending quota. `mt recheck-search-trailers [--dry-run]`
  re-applies the filters to past search-found rows and flips failures to `unavailable`.
- **Stats** runs every day for every `tracking_status='active'` trailer. Batched 50 per
  `videos.list` call (parts: `statistics,snippet,contentDetails,status`). Missing IDs in the
  response are marked `tracking_status='unavailable'`. First-sighting trailer technicals
  (duration, definition, embeddable, region_blocked, thumbnail, description) are written via
  COALESCE so they never overwrite existing values.
- **Comments** captures top-30 by relevance **twice per trailer in its lifetime**: at first
  discovery, then on the day before `tracking_end_date`. `comments_*_captured_at` columns on
  `trailers` are the idempotency guard — once set, the pipeline never re-captures.
- **Transcripts** run only on peach_server (YouTube blocks GCP egress), with every other
  phase skipped there. youtube-transcript-api in the title's languages first, faster-whisper
  fallback with per-segment confidence filtering. Each row carries `quality`
  (`ok|suspect|hallucinated|empty`) — **consumers filter `quality='ok'`**; the VAD-off
  "music" rescue pass hallucinates ("Thanks for watching!") often enough that the gate exists.
  Transient failures (IP block, yt-dlp) retry up to 3 runs; `mt requeue-transcripts` clears
  the guard for a class of rows.
- **Box office** (`box_office`, after predictions): for tracked movies released in the last
  180 days, TMDB `revenue` (worldwide, lifetime) and — with `OMDB_API_KEY` — OMDb's US
  domestic gross, re-observed weekly per title into `box_office_observations` (long format).
  Brazilian admissions are a file, not an API: `mt load-ancine` (peach_server, weekly)
  parses ANCINE's open-data CSV and matches titles to `movies` by original title (then a
  verified TMDB search on the Brazilian title); unmatched titles are listed, never guessed.
  `vw_movie_box_office` gives one value per (movie, market).
- **Sacnilk** (`sacnilk`, Indian releases 1–120 days out): scrapes the public
  `sacnilk.com/movie/<slug>` pages **politely** — one index request + ≤60 pages/run,
  ≥3 s apart, identified User-Agent, `robots.txt` re-checked, and every page verified
  (release date ±10 days *and* a top-cast/director name from `credits` in its Cast & Crew
  block) before a number is trusted; `sacnilk_pages` caches matched/unmatched/not-found so
  failures retry weekly. A 403/429/503 stops the run (`sacnilk.blocked`) — move the phase to
  peach_server then. Rows: `IN/india_net_inr`, `IN/india_gross_inr`, `IN/overseas_gross_inr`,
  `WW/worldwide_gross_inr` (cumulative) and `IN/net_inr_daily` per day.
- **Wikipedia** (`wikipedia`, all tracked films 7–180 days after release): infobox `gross` /
  `budget` via the Wikidata sitelink for the IMDb id (`wikipedia_pages` cache), parsed from
  `{{INR|330–400}} crore`, `₹143.60 crore`, `$12.3 million` etc. → `WW/gross_inr` or
  `WW/revenue_usd`. Wikidata's own box-office claims are empty for recent films — don't use them.
- **News** (`news`, after stats): GDELT GKG on BigQuery (`gdelt-bq.gdeltv2.gkg_partitioned`,
  ~150 MB/day scanned ≈ $0.01/day). the target names (titles + top cast / directors, normalized) are passed
  as query parameters and **equi-joined** against the exploded `AllNames` / `V2Persons` —
  never LIKE scans. GDELT is in the US multi-region, so that query runs there (`location="US"`)
  and must not reference our us-central1 dataset; results are merged from Python. `movie_news_daily.articles_used` = confirmed-by-a-person count for short
  titles, plain title count otherwise. A pre-release *signal* (`media_surge` finding, digest
  "News (14d)" column, MCP `movie_news`), not an outcome. `--news-days N` backfills (≤60).
- Tracking window: trailer `published_at` → movie `release_date` (or TV season `air_date`),
  plus a `tracking_grace_days` buffer. Stats collects while inside the window and flips
  `tracking_status='ended'` the day after it — so `active` means exactly "still collected".

## Key quota math (default YouTube quota = 10K/day)

- `videos.list`: 1 unit per call, up to 50 IDs.
- `commentThreads.list`: 1 unit per call.
- `search.list`: **100 units** per call (+1 for the duration check). Capped at `SEARCH_MAX_UNITS`.
- Per-day spend ≈ `(active / 50) + new_discoveries + entering_pre_release_window + 101 × searches`.
- With ~5–10k active trailers, daily spend is typically 1–2k units. Massive headroom.

## Build / test / run

```sh
uv sync                                   # install
uv run pytest                             # unit tests (respx-mocked HTTP)
uv run ruff check src tests               # lint
uv run mt run-daily --verbose             # full run (needs env vars + BQ)
uv run mt run-daily --dataset=movie_trailers_dev --limit=20 --verbose   # dry-ish run
```

Single phase: `--skip-movies --skip-tv --skip-search --skip-stats --skip-comments` flags compose.

One-off repair for the non-English trailers the old English-only TMDB call dropped
(zero search quota; 1 unit per 50 new trailers for their one stats snapshot):

```sh
uv run mt backfill-trailers --since 2026-05-12
```

Digest email (read-only against BQ):

```sh
uv run mt send-digest --period=week --dry-run --out /tmp/digest.html   # preview
uv run mt send-digest --period=week                                    # send
uv run mt send-digest --period=month                                   # later cadence
```

## Code layout

- `src/movie_trailers/clients/` — TMDB, YouTube, OMDb, BigQuery clients (HTTP retries via tenacity).
  `BigQueryClient.table_exists` lets optional phases/queries degrade on datasets missing the
  newer DDL (excitement, box office).
- `src/movie_trailers/pipeline/` — one module per phase; `_common.py` holds shared filters;
  `box_office.py` (TMDB + OMDb outcomes) and `ancine.py` (BR admissions CSV loader) are the
  outcome side; `search_trailers.evaluate_candidate` is the single YouTube-result filter.
- `src/movie_trailers/digest/` — read-only BQ queries + HTML render + SMTP send for the weekly/monthly digest email (`mt send-digest`). The digest also embeds the content section (see below). No writes; safe to run anytime.
- `src/movie_trailers/content/` — the content engine. `findings.py` scans for newsworthy movie states (incl. `excitement_fatigue`); `predictions.py` is the **prediction log** (the daily `predictions` phase is the sole writer) with two kinds: `trailer_due` (a trailer lands within min(21, days-to-release) days; fires only 7–45 days out) and `opening_tier` (one call per movie in its final pre-release week: high/mid/low tier from pre-release trailer views vs the last 180 days' releases, scored when the box office — BR admissions, else US gross, else WW revenue — ranks in the same tier of the same cohort; expired-with-no-data calls are `unscorable`, excluded from the hit rate); `drafts.py` renders dual-format (film-fan + engineering) posts; `polish.py` optional Claude rewrite; `render.py` the email section. `excitement_model.py` is the **distilled, torch-free comment-excitement scorer** (ONNX MiniLM embeddings + a numpy Ridge head; the head ships in `content/artifacts/ridge.npz`, the embedder is baked into the image). Drafts nothing externally — a human posts manually.
- `src/movie_trailers/mcp/` — read-only MCP server (`mt mcp`) exposing the dataset as tools (search/metrics/comments/trending/engagement-trend/findings/decay/track-record/excitement-decay/excitement-trend). Tool reference: [`src/movie_trailers/mcp/README.md`](src/movie_trailers/mcp/README.md).
- `benchmarks/excitement/` — **dev-only** distillation pipeline (never shipped): Claude labels comment-excitement, then trains the local student that the `excitement` phase runs. Refresh runbook + acceptance gate in its [`README.md`](benchmarks/excitement/README.md).
- `src/movie_trailers/models.py` — pydantic row types that map 1:1 to BigQuery tables.
- `src/movie_trailers/cli.py` — `mt run-daily`, `mt backfill-trailers`, `mt send-digest`, `mt suggest-content` (read-only preview), `mt mcp`, plus the repairs/loaders `mt requeue-transcripts`, `mt recheck-search-trailers`, `mt load-ancine`.
- `schemas/bigquery.sql` ends with a dated **migrations** section (idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS`). New columns/tables are never auto-created: apply them to each dataset by hand before deploying code that reads them (2026-10-11: `trailer_transcripts`, `trailer_search_log`, `content_predictions` columns; `box_office_observations` + `vw_movie_box_office`).
- `schemas/bigquery.sql` — DDL with `${DATASET}` placeholder.

## BigQuery write pattern

All writes go through one of two helpers on `BigQueryClient`:
- `merge_rows(...)` — load rows into a temp `_staging_<table>_<uuid>` table, then `MERGE` into target. Idempotent.
- `update_from_dicts(...)` — same staging mechanism but issues an `UPDATE … FROM staging` (for partial column updates like `channel_id`, `last_collected_at`, status flips).

Don't add ad-hoc `INSERT` queries — they break idempotency on re-runs.

## Non-obvious things to know

- **`trailers` is polymorphic.** A row is either `(content_kind='movie', movie_tmdb_id=...)` OR `(content_kind='tv', tv_tmdb_id=..., tv_season_number=...)`. Exactly one branch is set. The `trailer_stats_daily` and `trailer_comments_snapshots` tables key only on `youtube_video_id` so they work for both.
- **TMDB `/videos` defaults to English.** Without `include_video_language` it returns only
  `en` videos, silently dropping e.g. every Hindi/Tamil/Malayalam trailer. All three video
  calls pass the title's `original_language` (+ `spoken_languages` for non-English titles —
  on Indian films those are the dubbed releases) plus `en,null`. English titles stay
  `en`-only: their spoken languages are incidental dialogue and pull in dubbed
  foreign-market trailers.
- **Search-found trailers** have `official = NULL` (not curated by TMDB); their provenance is
  `trailer_search_log.matched_video_ids`, and every result the filter dropped is in
  `trailer_search_log.rejected` (JSON, with the reason). That table is not auto-created on fresh datasets.
- **TMDB popularity changed scale in 2025.** Upcoming titles now have a median near 1 and p90
  near 2.6 (max ~170); any threshold on `popularity` must be calibrated to that, not to the
  old hundreds-scale.
- **The digest email checks outcomes.** "Resolved since <cutoff>" lists every prediction
  scored in the period with its evidence (the trailer that landed, or the admissions/gross
  the tier was judged on); "Trailer signal vs box office" puts recent releases' pre-release
  views next to their observed outcome. Both read `vw_movie_box_office` and degrade to an
  empty section where it isn't provisioned.
- **Trailer filter** is Teaser + Trailer + Official Trailer (the last is a name-based promotion within `type=Trailer`). Clips/Featurettes/Behind-the-Scenes are dropped at discovery.
- **TMDB `release_date` and TV `air_date` drift.** Discover re-runs every day and recomputes `tracking_end_date`, so a postponed release keeps its trailers in the active set.
- **YouTube quota resets at midnight Pacific Time** — the schedule (14:00 UTC) is chosen to be well clear of that boundary.
- **Comments order=`relevance` is non-deterministic.** The `rank` column on `trailer_comments_snapshots` reflects YouTube's opinion at snapshot time — it's not stable across captures.
- **`credits` table dedupes by `(tmdb_id, content_kind, credit_kind, person_id)`.** A crew person who holds multiple key jobs (e.g. Director + Writer) gets one row with `job = "Director, Writer"` (comma-joined). Cast is top-10 by `order`; crew is filtered to Director/Writer/Screenplay/Producer/Executive Producer/Creator.
- **Derived metrics live in a view, not a table.** `vw_trailer_daily_metrics` computes per-day Δviews / Δlikes / engagement ratios on the fly via window functions over `trailer_stats_daily`. API consumers should query the view, not the raw table.
- **TMDB image paths are stored raw**, not as full URLs. `poster_path` / `backdrop_path` / `logo_path` / `profile_path` are all relative paths like `/abc.jpg`. Build URLs with `https://image.tmdb.org/t/p/<size><path>` at the API layer so consumers can pick their own size.
- **Comment-excitement is a distilled local model, not an LLM call.** The `excitement` phase scores each trailer's `at_discovery` comment section 0–100 using `content/excitement_model.py` (ONNX MiniLM embeddings via onnxruntime + a numpy Ridge head — **no torch, no API**). A Claude teacher trains it offline (`benchmarks/excitement/`); the student reproduces the validated finding that comment excitement decays across a movie's sequential trailers (a lexicon proxy is blind to it — the decay is semantic). Scores are keyed `(youtube_video_id, model_version)`; a retrain bumps `model_version` and re-scores. Per-movie decay lives in `vw_movie_trailer_excitement`.
- **A view over a `require_partition_filter` table must filter the base table in a CTE *before* window functions.** `vw_movie_trailer_excitement` computes `trailer_ordinal`/`n_trailers` with window functions; a `scored_date` predicate placed after them can't push down to partition elimination (BigQuery rejects the query). So the view filters `trailer_comment_excitement` inside a leading `WITH` CTE — then it's queryable with no consumer-side date predicate.

## What's deferred (not implemented yet)

- Per-country revenue beyond BR/IN/US/WW (would need a paid source). Sacnilk footfalls
  are behind their paid tier; a licensed Sacnilk feed would replace the scraper. The ANCINE URL answered
  HTTP 403 from outside Brazil during development — the loader takes a file path too.
- TV titles get no YouTube-search fallback.
- Secret Manager (env vars are passed via `--set-env-vars` on the Cloud Run Job).
- Terraform / IaC.
- **Publishing the content drafts/predictions.** Deliberately not automated. The prediction log is private (BigQuery + the weekly digest email to the operator only); it runs daily to accumulate an unbiased hit rate. Posting to LinkedIn / Reddit / X is a manual step the operator takes from the email once the hit rate is trusted — nothing in this codebase posts externally.
