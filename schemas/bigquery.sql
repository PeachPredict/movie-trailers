-- BigQuery DDL for movie-trailers.
--
-- All counters are INT64 (YouTube returns them as strings — the clients parse them).
-- Time-series tables are partitioned by date and clustered by youtube_video_id.
-- The pipeline writes via MERGE for idempotency; natural keys are documented per-table.
--
-- Substitute ${DATASET} when applying (e.g. movie_trailers_dev or movie_trailers).

CREATE TABLE IF NOT EXISTS `${DATASET}.movies` (
  tmdb_id INT64 NOT NULL,
  imdb_id STRING,
  title STRING,
  original_title STRING,
  original_language STRING,
  primary_release_date DATE,
  genres ARRAY<STRUCT<id INT64, name STRING>>,
  status STRING,
  popularity FLOAT64,
  poster_path STRING,                       -- TMDB relative path; build URL with https://image.tmdb.org/t/p/<size><path>
  backdrop_path STRING,                     -- TMDB relative path
  overview STRING,
  tagline STRING,
  homepage STRING,
  runtime_minutes INT64,
  production_companies ARRAY<STRUCT<id INT64, name STRING, logo_path STRING, origin_country STRING>>,
  origin_countries ARRAY<STRING>,           -- ISO 3166-1 codes from production_countries
  spoken_languages ARRAY<STRUCT<iso_639_1 STRING, english_name STRING, name STRING>>,
  collected_at TIMESTAMP NOT NULL
)
OPTIONS (description = "Slowly-changing movie metadata. MERGE key: tmdb_id.");

CREATE TABLE IF NOT EXISTS `${DATASET}.tv_shows` (
  tmdb_id INT64 NOT NULL,
  name STRING,
  original_name STRING,
  original_language STRING,
  first_air_date DATE,
  status STRING,
  genres ARRAY<STRUCT<id INT64, name STRING>>,
  vote_average FLOAT64,
  vote_count INT64,
  popularity FLOAT64,
  poster_path STRING,                       -- TMDB relative path
  backdrop_path STRING,                     -- TMDB relative path
  overview STRING,
  tagline STRING,
  homepage STRING,
  production_companies ARRAY<STRUCT<id INT64, name STRING, logo_path STRING, origin_country STRING>>,
  networks ARRAY<STRUCT<id INT64, name STRING, logo_path STRING, origin_country STRING>>,
  origin_countries ARRAY<STRING>,
  spoken_languages ARRAY<STRUCT<iso_639_1 STRING, english_name STRING, name STRING>>,
  collected_at TIMESTAMP NOT NULL
)
OPTIONS (description = "Slowly-changing TV-show metadata. MERGE key: tmdb_id.");

CREATE TABLE IF NOT EXISTS `${DATASET}.tv_seasons` (
  tv_tmdb_id INT64 NOT NULL,
  season_number INT64 NOT NULL,
  name STRING,
  air_date DATE,
  episode_count INT64,
  overview STRING,
  poster_path STRING,                       -- TMDB relative path
  collected_at TIMESTAMP NOT NULL
)
OPTIONS (description = "TV seasons. MERGE key: (tv_tmdb_id, season_number).");

CREATE TABLE IF NOT EXISTS `${DATASET}.trailers` (
  youtube_video_id STRING NOT NULL,
  content_kind STRING NOT NULL,             -- 'movie' | 'tv'
  movie_tmdb_id INT64,                      -- set when content_kind = 'movie'
  tv_tmdb_id INT64,                         -- set when content_kind = 'tv'
  tv_season_number INT64,                   -- set when content_kind = 'tv'
  video_type STRING,                        -- 'Teaser' | 'Trailer' | 'Official Trailer'
  name STRING,
  published_at TIMESTAMP,
  channel_id STRING,
  channel_title STRING,
  official BOOL,
  tracking_end_date DATE,                   -- recomputed every discover run
  thumbnail_url STRING,                     -- best-available YouTube thumbnail (maxres → high → medium → default)
  description STRING,                       -- YouTube snippet.description
  duration_seconds INT64,                   -- parsed from ISO 8601 contentDetails.duration
  definition STRING,                        -- 'hd' | 'sd'
  embeddable BOOL,
  region_blocked ARRAY<STRING>,             -- ISO 3166-1 codes; usually empty
  first_seen_at TIMESTAMP NOT NULL,
  last_collected_at TIMESTAMP,
  tracking_status STRING NOT NULL,          -- 'active' | 'ended' | 'unavailable'
  comments_disabled BOOL NOT NULL,          -- pipeline always inserts explicit value
  comments_at_discovery_captured_at TIMESTAMP,
  comments_pre_release_captured_at TIMESTAMP,
  transcript_captured_at TIMESTAMP          -- guard: stamped success or permanent failure; never re-runs
)
OPTIONS (description = "Polymorphic trailer registry. MERGE key: youtube_video_id. Exactly one of {movie_tmdb_id} or {tv_tmdb_id, tv_season_number} is set.");

CREATE TABLE IF NOT EXISTS `${DATASET}.trailer_stats_daily` (
  youtube_video_id STRING NOT NULL,
  collected_date DATE NOT NULL,
  view_count INT64,
  like_count INT64,
  comment_count INT64,
  favorite_count INT64,
  collected_at TIMESTAMP NOT NULL
)
PARTITION BY collected_date
CLUSTER BY youtube_video_id
OPTIONS (
  description = "Daily YouTube stats per trailer. MERGE key: (youtube_video_id, collected_date).",
  require_partition_filter = TRUE
);

CREATE TABLE IF NOT EXISTS `${DATASET}.trailer_comments_snapshots` (
  youtube_video_id STRING NOT NULL,
  snapshot_kind STRING NOT NULL,            -- 'at_discovery' | 'pre_release'
  snapshot_date DATE NOT NULL,
  comment_id STRING NOT NULL,
  text STRING,
  like_count INT64,
  total_reply_count INT64,
  author_channel_id STRING,
  author_display_name STRING,
  published_at TIMESTAMP,
  updated_at TIMESTAMP,
  rank INT64,                               -- 1..30, YouTube relevance order at snapshot time
  collected_at TIMESTAMP NOT NULL
)
PARTITION BY snapshot_date
CLUSTER BY youtube_video_id
OPTIONS (
  description = "Top-30 comments per trailer, captured twice in lifetime. MERGE key: (youtube_video_id, snapshot_kind, comment_id). `rank` is YouTube's relevance order at snapshot time — not stable.",
  require_partition_filter = TRUE
);

CREATE TABLE IF NOT EXISTS `${DATASET}.box_office` (
  tmdb_id INT64 NOT NULL,
  budget INT64,
  revenue INT64,                            -- worldwide, USD per TMDB
  runtime_minutes INT64,
  release_date_used DATE,
  tmdb_popularity_at_capture FLOAT64,
  captured_at TIMESTAMP NOT NULL
)
OPTIONS (description = "Post-theatrical-run box-office snapshot. MERGE key: tmdb_id. V1 covers worldwide totals only.");

CREATE TABLE IF NOT EXISTS `${DATASET}.trailer_transcripts` (
  youtube_video_id STRING NOT NULL,
  source STRING NOT NULL,                   -- 'yta' | 'whisper'
  track_kind STRING,                        -- 'manual' | 'auto-generated' when source='yta'; NULL for whisper
  language STRING,                          -- BCP-47 / ISO 639-1
  text STRING,
  word_count INT64,
  char_count INT64,
  error STRING,                             -- non-NULL when both methods failed; text will be NULL
  captured_at TIMESTAMP NOT NULL
)
OPTIONS (description = "Per-trailer transcript, captured once at discovery. MERGE key: youtube_video_id. yta primary, faster-whisper small fallback.");

CREATE TABLE IF NOT EXISTS `${DATASET}.watch_providers` (
  tmdb_id INT64 NOT NULL,
  content_kind STRING NOT NULL,             -- 'movie' | 'tv'
  region STRING NOT NULL,                   -- ISO 3166-1 alpha-2
  kind STRING NOT NULL,                     -- 'flatrate' | 'rent' | 'buy' | 'free' | 'ads'
  provider_id INT64 NOT NULL,
  provider_name STRING,
  logo_path STRING,
  display_priority INT64,
  link STRING,                              -- TMDB-provided JustWatch deep link for that region
  collected_at TIMESTAMP NOT NULL
)
OPTIONS (description = "Streaming/rent/buy availability per region. MERGE key: (tmdb_id, content_kind, region, kind, provider_id). Source: TMDB /watch/providers via append_to_response.");

CREATE TABLE IF NOT EXISTS `${DATASET}.credits` (
  tmdb_id INT64 NOT NULL,
  content_kind STRING NOT NULL,             -- 'movie' | 'tv'
  credit_kind STRING NOT NULL,              -- 'cast' | 'crew'
  person_id INT64 NOT NULL,
  name STRING,
  character STRING,                         -- populated when credit_kind = 'cast'
  job STRING,                               -- populated when credit_kind = 'crew'
  department STRING,
  profile_path STRING,
  order_index INT64,                        -- cast `order`; crew has none (use 0)
  collected_at TIMESTAMP NOT NULL
)
OPTIONS (description = "Top cast + key crew per title. MERGE key: (tmdb_id, content_kind, credit_kind, person_id, COALESCE(job, character)). Cast is top 10 by `order`; crew is filtered to Director/Writer/Producer/Executive Producer/Creator.");

CREATE TABLE IF NOT EXISTS `${DATASET}.daily_run_log` (
  run_id STRING NOT NULL,
  phase STRING NOT NULL,                    -- 'discover_movies' | 'discover_tv' | 'stats' | 'comments' | 'transcripts' | 'box_office'
  started_at TIMESTAMP NOT NULL,
  finished_at TIMESTAMP,
  trailers_processed INT64,
  quota_units_used INT64,
  errors INT64,
  notes STRING
)
PARTITION BY DATE(started_at)
OPTIONS (description = "Per-phase operational telemetry for each daily run.");

CREATE TABLE IF NOT EXISTS `${DATASET}.content_predictions` (
  prediction_id STRING NOT NULL,            -- '{movie_tmdb_id}-{predicted_at}'
  kind STRING NOT NULL,                     -- 'trailer_due' (room for more later)
  movie_tmdb_id INT64 NOT NULL,
  title STRING,
  predicted_at DATE NOT NULL,               -- day the call was logged
  horizon_days INT64 NOT NULL,              -- expect the trailer within this many days
  days_to_release_at_prediction INT64,
  vs_peak_at_prediction FLOAT64,
  basis STRING,                             -- human-readable rationale snapshot
  status STRING NOT NULL,                   -- 'open' | 'hit' | 'miss'
  resolved_at DATE,
  resolved_youtube_video_id STRING,         -- the trailer that confirmed a hit
  resolved_lag_days INT64,                  -- days from predicted_at to that trailer
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
)
OPTIONS (description = "Trailer-due predictions and their resolutions (the public prediction log). MERGE key: prediction_id. 'hit' = a new trailer appeared within horizon_days of predicted_at; 'miss' = the window passed with none. Only one open prediction per movie at a time.");

CREATE TABLE IF NOT EXISTS `${DATASET}.trailer_comment_excitement` (
  youtube_video_id STRING NOT NULL,
  model_version STRING NOT NULL,            -- 'mlv1-minilm-l6-20260627' (distilled student version)
  scored_date DATE NOT NULL,                -- day the score was computed
  excitement FLOAT64,                       -- 0..100, predicted comment-section excitement
  snapshot_kind STRING NOT NULL,            -- 'at_discovery' (fresh reaction per trailer)
  n_comments INT64,                         -- top-N comments pooled into this score
  mean_like_count FLOAT64,
  source_snapshot_date DATE,                -- the comment snapshot the score came from
  created_at TIMESTAMP NOT NULL
)
PARTITION BY scored_date
CLUSTER BY youtube_video_id
OPTIONS (
  description = "Per-trailer comment-excitement (0-100) from the distilled local model (ONNX MiniLM + Ridge). MERGE key: (youtube_video_id, model_version). One row per trailer per model version; refreshed when the model is retrained. Source of the per-movie excitement-decay metric.",
  require_partition_filter = TRUE
);

-- -----------------------------------------------------------------------------
-- Derived metrics view: per-trailer daily deltas and engagement ratios.
-- Computed at read time over trailer_stats_daily; no extra storage.
-- API consumers should query this view, not the raw table, for derived signals.
-- -----------------------------------------------------------------------------

CREATE OR REPLACE VIEW `${DATASET}.vw_trailer_daily_metrics` AS
SELECT
  s.youtube_video_id,
  s.collected_date,
  s.view_count,
  s.like_count,
  s.comment_count,
  s.view_count - LAG(s.view_count) OVER w        AS delta_views,
  s.like_count - LAG(s.like_count) OVER w        AS delta_likes,
  s.comment_count - LAG(s.comment_count) OVER w  AS delta_comments,
  SAFE_DIVIDE(s.like_count, s.view_count)        AS like_view_ratio,
  SAFE_DIVIDE(s.comment_count, s.view_count)     AS comment_view_ratio,
  DATE_DIFF(s.collected_date, DATE(t.published_at), DAY) AS days_since_publish
FROM `${DATASET}.trailer_stats_daily` s
LEFT JOIN `${DATASET}.trailers` t USING (youtube_video_id)
-- The base table has require_partition_filter=TRUE; the window below satisfies that
-- and is wide enough to cover a full trailer-tracking + box-office lifecycle.
-- Consumers can add stricter `collected_date` predicates and BigQuery will push them down.
WHERE s.collected_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 730 DAY)
WINDOW w AS (PARTITION BY s.youtube_video_id ORDER BY s.collected_date);

-- -----------------------------------------------------------------------------
-- Per-movie trailer excitement: each movie trailer's comment-excitement score
-- with its ordinal position in the movie's trailer sequence (by published_at).
-- The spine of the excitement-decay metric (do later trailers draw less-excited
-- comments?). Joins to the polymorphic trailers table, movie branch only.
-- -----------------------------------------------------------------------------

-- The partition filter is applied in the `e` CTE, directly on the base table
-- before the join/window — so partition elimination is provable and consumers can
-- query the view without adding their own `scored_date` predicate. (A filter placed
-- after the window functions can't push down through them.)
CREATE OR REPLACE VIEW `${DATASET}.vw_movie_trailer_excitement` AS
WITH e AS (
  SELECT youtube_video_id, excitement, model_version, scored_date
  FROM `${DATASET}.trailer_comment_excitement`
  WHERE scored_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 730 DAY) AND excitement IS NOT NULL
)
SELECT
  t.movie_tmdb_id,
  e.youtube_video_id,
  e.excitement,
  e.model_version,
  e.scored_date,
  t.published_at,
  ROW_NUMBER() OVER (PARTITION BY t.movie_tmdb_id, e.model_version ORDER BY t.published_at) AS trailer_ordinal,
  COUNT(*)     OVER (PARTITION BY t.movie_tmdb_id, e.model_version)                         AS n_trailers
FROM e
JOIN `${DATASET}.trailers` t USING (youtube_video_id)
WHERE t.content_kind = 'movie' AND t.movie_tmdb_id IS NOT NULL;
