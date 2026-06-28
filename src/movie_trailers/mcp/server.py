"""FastMCP server exposing the trailer dataset as read-only tools.

Run via `mt mcp` (stdio transport, for Claude Desktop / IDE MCP clients) or
`python -m movie_trailers.mcp.server`. Reuses the same `Settings` and
`BigQueryClient` as the rest of the app; performs no writes.

Note: `from mcp.server.fastmcp import FastMCP` resolves to the installed `mcp`
package — this local package is `movie_trailers.mcp`, so there's no shadowing.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from movie_trailers.clients.bigquery import BigQueryClient, _serialize
from movie_trailers.config import load_settings
from movie_trailers.mcp import queries

mcp = FastMCP("movie-trailers")

_bq: BigQueryClient | None = None


def _client() -> BigQueryClient:
    """Lazily build a single BigQueryClient from env settings."""
    global _bq
    if _bq is None:
        s = load_settings()
        _bq = BigQueryClient(project=s.gcp_project, dataset=s.bq_dataset, location=s.bq_location)
    return _bq


def _rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make BQ rows JSON-safe (dates/datetimes → ISO strings)."""
    return [_serialize(r) for r in rows]


@mcp.tool()
def search_trailers(
    content_kind: str | None = None,
    country: str | None = None,
    tracking_status: str = "active",
    query_text: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Search tracked trailers for upcoming movies/TV.

    Args:
        content_kind: 'movie' or 'tv' to restrict; omit for both.
        country: ISO 3166-1 origin country code (e.g. 'US', 'KR', 'IN').
        tracking_status: 'active' (default), 'ended', or 'unavailable'.
        query_text: case-insensitive substring match on the movie/show title.
        limit: max rows (capped at 100).

    Returns one row per trailer with title, video id, movie_tmdb_id (feed it to
    movie_engagement_trend), video_type, release_date, origin_countries, and a
    ready-to-use poster_url.
    """
    return _rows(
        queries.search_trailers(
            _client(),
            content_kind=content_kind,
            country=country,
            tracking_status=tracking_status,
            query_text=query_text,
            limit=limit,
        )
    )


@mcp.tool()
def trailer_metrics(youtube_video_id: str, days: int = 30) -> list[dict[str, Any]]:
    """Daily view/like growth and engagement ratios for one trailer.

    Args:
        youtube_video_id: the trailer's YouTube video id.
        days: lookback window in days (default 30).

    Returns a per-day series from vw_trailer_daily_metrics: view/like/comment
    counts, their daily deltas, like/comment-to-view ratios, and days_since_publish.
    """
    return _rows(queries.trailer_metrics(_client(), youtube_video_id=youtube_video_id, days=days))


@mcp.tool()
def top_comments(
    youtube_video_id: str,
    snapshot_kind: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Top captured YouTube comments for a trailer, by relevance rank.

    Args:
        youtube_video_id: the trailer's YouTube video id.
        snapshot_kind: 'at_discovery' or 'pre_release'; omit for the latest snapshot.
        limit: max comments (capped at 30).

    Ranks reflect YouTube's relevance order at snapshot time and are not stable
    across captures.
    """
    return _rows(
        queries.top_comments(
            _client(),
            youtube_video_id=youtube_video_id,
            snapshot_kind=snapshot_kind,
            limit=limit,
        )
    )


@mcp.tool()
def trending(
    period_days: int = 7,
    content_kind: str | None = None,
    country: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Trailers gaining the most views over a recent window.

    Args:
        period_days: window length in days (default 7).
        content_kind: 'movie' or 'tv' to restrict; omit for both.
        country: ISO 3166-1 origin country code to restrict.
        limit: max rows (capped at 100).

    Returns trailers ranked by total views_gained, with likes_gained, current
    view_count, avg like/view ratio, title, and poster_url.
    """
    return _rows(
        queries.trending(
            _client(),
            period_days=period_days,
            content_kind=content_kind,
            country=country,
            limit=limit,
        )
    )


@mcp.tool()
def movie_engagement_trend(movie_tmdb_id: int, days: int = 30) -> dict[str, Any]:
    """Score a movie on two independent engagement axes over time.

    Aggregated across all the movie's trailers, per day:
      - quality: likes ÷ views, classified gaining / losing / steady (±5% band).
      - demand: view velocity (daily new views), classified rebounding / holding
        / cooling / spent by how far it has fallen below its peak, with a
        post-peak half-life. These axes are near-uncorrelated, so a movie can be
        'loved but fading' or 'broadly watched but lukewarm'.

    Args:
        movie_tmdb_id: the movie's TMDB id (see search_trailers output).
        days: lookback window in days (default 30).

    Returns: a combined `verdict`, `quality_classification` (+ slope/pct_change),
    a `demand` block (demand_state, current/peak velocity, velocity_vs_peak,
    post_peak_half_life_days), the daily series, trailer count, and any
    recent_launches in the window. Studios launch a fresh trailer to refresh
    demand, so launches are surfaced next to the verdict.
    """
    return _serialize(
        queries.movie_engagement_trend(_client(), movie_tmdb_id=movie_tmdb_id, days=days)
    )


@mcp.tool()
def interesting_findings(days: int = 30, top: int = 6) -> list[dict[str, Any]]:
    """Surface the most newsworthy movie states, ready to turn into posts.

    Scans the dataset for: 'trailer_due' (demand spent + release near → a new
    trailer is likely imminent), 'surging' (a trailer still near its peak views),
    and 'quality_slide' (like/view falling fast). Each finding includes a
    headline, the driving metrics, a salience score, and the daily new-views
    series — the raw material for a film-fan or engineering-portfolio post.

    Args:
        days: lookback window for the scan (default 30).
        top: max findings to return (default 6), one per movie.
    """
    from movie_trailers.content.findings import detect_findings

    found = detect_findings(_client(), days=days, top=top)
    return [_serialize(f.model_dump()) for f in found]


@mcp.tool()
def engagement_decay(days: int = 45, unit: str = "movie") -> dict[str, Any]:
    """How like/view engagement trends over time, per movie or per trailer.

    Args:
        days: lookback window (default 45).
        unit: 'movie' (default) fits each movie's summed-trailer like/view trend;
            'trailer' fits each individual trailer.

    Returns the cross-unit summary (losing/steady/gaining counts, median and
    view-weighted % change) plus a split by the parent movie's trailer count, so
    you can see whether single-trailer movies decay harder than ones that keep
    launching. In this dataset ~60% of movies are losing (median ≈ −9%).
    """
    return _serialize(queries.engagement_decay(_client(), days=days, unit=unit))


@mcp.tool()
def comment_excitement_decay(model_version: str | None = None) -> dict[str, Any]:
    """Does comment excitement decay across a movie's sequential trailers?

    For each movie with ≥2 trailers, fits the distilled comment-excitement score
    (0–100, from a local ONNX+Ridge model) against the trailer's ordinal position
    and summarizes the first→last change across movies, split by trailer count.
    In this dataset ~60% of movies decline (median ≈ −2 pts after regularization),
    with a clean dose-response: movies with 5+ trailers fall hardest. A lexicon
    proxy is blind to this — the decay is semantic (trailer fatigue, sarcasm).

    Args:
        model_version: pin a specific student version; omit for the latest.
    """
    return _serialize(queries.comment_excitement_decay(_client(), model_version=model_version))


@mcp.tool()
def movie_excitement_trend(movie_tmdb_id: int, model_version: str | None = None) -> dict[str, Any]:
    """One movie's comment excitement across its trailer sequence.

    Returns the ordered trailers (ordinal, excitement), the first→last delta, the
    per-trailer slope, and a verdict (cooling / steady / warming) — the per-movie
    view behind comment_excitement_decay.

    Args:
        movie_tmdb_id: the movie's TMDB id (see search_trailers output).
        model_version: pin a specific student version; omit for the latest.
    """
    return _serialize(
        queries.movie_excitement_trend(
            _client(), movie_tmdb_id=movie_tmdb_id, model_version=model_version
        )
    )


@mcp.tool()
def prediction_track_record() -> dict[str, Any]:
    """The trailer-prediction scoreboard: hits, misses, hit rate, avg lag, open calls.

    Each 'trailer_due' prediction is confirmed (hit) when a new trailer actually
    drops within its horizon, or marked a miss when the window passes. Use this to
    report how accurate the automated predictions have been.
    """
    from movie_trailers.content.predictions import track_record

    return _serialize(track_record(_client()))


def main() -> None:
    """Entrypoint for `mt mcp` and `python -m movie_trailers.mcp.server`."""
    mcp.run()


if __name__ == "__main__":
    main()
