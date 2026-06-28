# Movie-trailers MCP server

A **read-only** [MCP](https://modelcontextprotocol.io/) server that exposes the
trailer dataset (BigQuery) as tools an LLM client can call. It performs no
writes and spends no YouTube quota — every tool is a query against BigQuery.

It mirrors the `digest/` pattern: same `Settings` and `BigQueryClient` as the
rest of the app. See [`server.py`](server.py) for the tool wiring and
[`queries.py`](queries.py) for the SQL.

## Running

```sh
uv run mt mcp                       # stdio transport
# or
python -m movie_trailers.mcp.server
```

Needs the same environment as the pipeline: `GCP_PROJECT`, `BQ_DATASET`
(default `movie_trailers`), `BQ_LOCATION`, and Google ADC
(`gcloud auth application-default login`).

### Connecting a client (Claude Desktop / IDE)

Point an MCP client at the `mt mcp` command, e.g. in a Claude Desktop config:

```json
{
  "mcpServers": {
    "movie-trailers": {
      "command": "uv",
      "args": ["run", "mt", "mcp"],
      "cwd": "/path/to/movie-trailers"
    }
  }
}
```

## Conventions

- **Read-only.** No tool writes to BigQuery. (The prediction log and the
  comment-excitement scores are written by the daily pipeline's `predictions` and
  `excitement` phases, never by the MCP server — this server only *reads* them.)
- **Derived metrics come from the view.** Δviews/Δlikes/ratios are read from
  `vw_trailer_daily_metrics`, not the raw `trailer_stats_daily` table.
- **Image URLs are built for you.** TMDB paths are stored raw (e.g. `/abc.jpg`);
  tools that return artwork add a ready-to-use `poster_url`
  (`https://image.tmdb.org/t/p/w500<path>`) next to the raw `poster_path`.
- **`trailers` is polymorphic.** A row is a movie (`movie_tmdb_id`) or a TV
  season (`tv_tmdb_id` + `tv_season_number`); `search_trailers` returns whichever
  applies. Engagement tools are movie-scoped (keyed by `movie_tmdb_id`).
- **All dates/timestamps** come back as ISO strings.

## Tools

### `search_trailers(content_kind=None, country=None, tracking_status="active", query_text=None, limit=25)`
Find tracked trailers for upcoming movies/TV.
- `content_kind`: `"movie"` | `"tv"`; omit for both.
- `country`: ISO-3166-1 origin code (`"US"`, `"KR"`, `"IN"`, …) matched against the title's origin countries.
- `tracking_status`: `"active"` (default) | `"ended"` | `"unavailable"`.
- `query_text`: case-insensitive substring on the movie/show title.
- `limit`: capped at 100.

Returns one row per trailer with `youtube_video_id`, `content_kind`,
**`movie_tmdb_id`** (feed it to `movie_engagement_trend`), `title`,
`trailer_name`, `video_type`, `published_at`, `release_date`,
`tracking_end_date`, `origin_countries`, `poster_url`, `thumbnail_url`.

> *"Active Korean TV trailers"* · *"movie trailers releasing soon in Brazil"*

---

### `trailer_metrics(youtube_video_id, days=30)`
Daily view/like growth and engagement ratios for **one** trailer, from
`vw_trailer_daily_metrics`. Returns a per-day series: `collected_date`,
`view_count`, `like_count`, `comment_count`, `delta_views`, `delta_likes`,
`delta_comments`, `like_view_ratio`, `comment_view_ratio`, `days_since_publish`.

> *"How fast is video XYZ gaining views over the last 14 days?"*

---

### `top_comments(youtube_video_id, snapshot_kind=None, limit=30)`
Top captured YouTube comments for a trailer, by relevance `rank`.
- `snapshot_kind`: `"at_discovery"` | `"pre_release"`; omit for the latest snapshot.
- `limit`: capped at 30.

Returns `snapshot_kind`, `snapshot_date`, `rank`, `text`, `like_count`,
`total_reply_count`, `author_display_name`, `published_at`. Ranks reflect
YouTube's relevance order *at snapshot time* and are not stable across captures.

> *"What are people saying in the top comments on this trailer?"*

---

### `trending(period_days=7, content_kind=None, country=None, limit=20)`
Trailers gaining the most views over a recent window (sums `delta_views`).
- `period_days`: window length (default 7).
- `content_kind` / `country`: optional filters (as in `search_trailers`).
- `limit`: capped at 100.

Returns trailers ranked by `views_gained`, with `likes_gained`, current
`view_count`, `avg_like_view_ratio`, `title`, and `poster_url`.

> *"Which upcoming-movie trailers blew up this week in the US?"*

---

### `movie_engagement_trend(movie_tmdb_id, days=30)`
Scores a movie on **two independent axes** (≈+0.10 correlated), aggregated
across all its trailers:
- **Quality** — like/view ratio → `quality_classification` of `gaining` /
  `losing` / `steady` (±5% start→end band), plus `slope_per_day`,
  `pct_change_over_window`, `ratio_start/end`, `avg_like_view_ratio`.
- **Demand** — view velocity → a `demand` block: `demand_state` of `rebounding`
  / `holding` / `cooling` / `spent`, `current_velocity`, `peak_velocity`,
  `velocity_vs_peak`, `post_peak_half_life_days`.

Also returns a combined `verdict` string, the daily `series`, `n_trailers`,
`new_trailers_in_window`, and `recent_launches`. A movie can be "loved but
fading" (steady quality / spent demand) or the reverse.

> *"Is Movie 1234 gaining or losing engagement, and is it due for a new trailer?"*

---

### `interesting_findings(days=30, top=6)`
Surfaces the most newsworthy movie states (one per movie, ranked by salience):
- `trailer_due` — demand spent + release near → a new trailer is likely imminent.
- `surging` — view velocity still near its peak.
- `quality_slide` — like/view falling fast.

Each finding has `kind`, `movie_tmdb_id`, `title`, `headline`, `salience`,
`metrics`, and the daily new-views `series`. These are the raw material for the
draft posts in the weekly digest.

> *"What's worth posting about today?"*

---

### `engagement_decay(days=45, unit="movie")`
How like/view engagement trends across the dataset, fitted **per unit** and
summarized.
- `unit`: `"movie"` (fit each movie's summed-trailer ratio) | `"trailer"` (fit
  each individual trailer).
- `days`: lookback window (default 45).

Returns `units`, `losing` / `steady` / `gaining` counts, `losing_share`,
`median_pct_change`, `view_weighted_pct_change`, and a `by_movie_trailer_count`
split (`"1"`, `"2-3"`, `"4+"`) so you can see whether single-trailer movies decay
harder than ones that keep launching. (In this dataset ~60% of movies are
losing, median ≈ −9%.)

> Note: aggregating *all* trailers into one pooled ratio masks the decay (fresh
> trailers keep entering at peak) — which is why this tool fits per unit. Use
> `unit="movie"` or `unit="trailer"`, not a global total.

---

### `comment_excitement_decay(model_version=None)`
Does comment *excitement* decay across a movie's sequential trailers? Each trailer's
`at_discovery` comment section is scored 0–100 by a distilled local model (ONNX
MiniLM + Ridge — no LLM call), then per movie the score is fitted against the
trailer's ordinal position. Summarized across movies and split by trailer count.
- `model_version`: pin a specific student version; omit for the latest.

Returns `movies`, `declining` / `steady` / `rising` counts, `declining_share`,
`median_first_last_delta` (points), `mean_slope_per_trailer`, and a
`by_movie_trailer_count` split (`"2"`, `"3-4"`, `"5+"`). The dose-response is the
signal: movies that keep dropping trailers cool hardest. A lexicon proxy is blind
to this — the decay is semantic (trailer fatigue, sarcasm).

> *"Are studios wearing audiences out with too many trailers?"*

---

### `movie_excitement_trend(movie_tmdb_id, model_version=None)`
One movie's comment excitement across its trailer sequence — the per-movie view
behind `comment_excitement_decay`. Returns the ordered `trailers` (`trailer_ordinal`,
`excitement`, `published_at`), the `first_last_delta`, `slope_per_trailer`, and a
`verdict` (cooling / steady / warming).

> *"Did each new trailer for movie 1234 land flatter than the last?"*

---

### `prediction_track_record()`
The trailer-prediction scoreboard. Each `trailer_due` prediction (logged daily
by the pipeline) is a **hit** when a new trailer drops within its horizon, a
**miss** when the window passes. Returns `hits`, `misses`, `open`, `hit_rate`,
and `avg_hit_lag_days`.

> *"How accurate have the automated trailer predictions been?"*

## Typical chains

- **Discover → drill in:** `search_trailers` (get `movie_tmdb_id`) →
  `movie_engagement_trend` → `top_comments`.
- **What's hot:** `trending` or `interesting_findings` → `trailer_metrics` for
  the specific video.
- **Dataset-level:** `engagement_decay` for the decay picture;
  `prediction_track_record` for prediction accuracy.
