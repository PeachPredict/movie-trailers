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
  comments_pre_release_captured_at TIMESTAMP
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
  phase STRING NOT NULL,                    -- 'discover_movies' | 'discover_tv' | 'stats' | 'comments' | 'box_office'
  started_at TIMESTAMP NOT NULL,
  finished_at TIMESTAMP,
  trailers_processed INT64,
  quota_units_used INT64,
  errors INT64,
  notes STRING
)
PARTITION BY DATE(started_at)
OPTIONS (description = "Per-phase operational telemetry for each daily run.");

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
