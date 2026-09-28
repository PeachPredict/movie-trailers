from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.clients.tmdb import TMDBClient
from movie_trailers.clients.youtube import YouTubeClient
from movie_trailers.config import Settings
from movie_trailers.models import (
    CreditRow,
    MovieRow,
    TrackingStatus,
    TrailerRow,
    WatchProviderRow,
)
from movie_trailers.pipeline._common import (
    classify_video_type,
    credits_from_details,
    is_trailer_video,
    parse_companies,
    parse_genres,
    parse_languages,
    parse_origin_countries,
    parse_published_at,
    title_languages,
    watch_providers_from_details,
)

log = structlog.get_logger()


def run_discover_movies(
    *,
    tmdb: TMDBClient,
    bq: BigQueryClient,
    settings: Settings,
    today: date | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """Discover upcoming movies and their trailers.

    Returns (movies_upserted, trailers_upserted).
    """
    today = today or datetime.now(UTC).date()
    horizon_end = today + timedelta(days=30 * settings.discover_window_months)

    seen_tmdb_ids: set[int] = set()
    movie_rows: list[MovieRow] = []
    trailer_rows: list[TrailerRow] = []
    watch_rows: list[WatchProviderRow] = []
    credit_rows: list[CreditRow] = []

    for region in settings.tmdb_regions:
        log.info(
            "discover_movies.region",
            region=region,
            window=f"{today.isoformat()}..{horizon_end.isoformat()}",
        )
        for movie in tmdb.discover_movies(
            release_date_gte=today.isoformat(),
            release_date_lte=horizon_end.isoformat(),
            region=region,
        ):
            tmdb_id = int(movie["id"])
            if tmdb_id in seen_tmdb_ids:
                continue
            seen_tmdb_ids.add(tmdb_id)
            if limit is not None and len(seen_tmdb_ids) > limit:
                break

            try:
                details = tmdb.movie_details(tmdb_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("discover_movies.details_failed", tmdb_id=tmdb_id, error=str(exc))
                continue

            release_date_str = details.get("release_date") or movie.get("release_date")
            release_date = (
                date.fromisoformat(release_date_str) if release_date_str else None
            )
            now = datetime.now(UTC)
            movie_rows.append(
                MovieRow(
                    tmdb_id=tmdb_id,
                    imdb_id=details.get("imdb_id"),
                    title=details.get("title"),
                    original_title=details.get("original_title"),
                    original_language=details.get("original_language"),
                    primary_release_date=release_date,
                    genres=parse_genres(details.get("genres")),
                    status=details.get("status"),
                    popularity=details.get("popularity"),
                    poster_path=details.get("poster_path"),
                    backdrop_path=details.get("backdrop_path"),
                    overview=details.get("overview"),
                    tagline=details.get("tagline"),
                    homepage=details.get("homepage"),
                    runtime_minutes=details.get("runtime"),
                    production_companies=parse_companies(details.get("production_companies")),
                    origin_countries=parse_origin_countries(details),
                    spoken_languages=parse_languages(details.get("spoken_languages")),
                    collected_at=now,
                )
            )
            watch_rows.extend(
                watch_providers_from_details(
                    details, tmdb_id=tmdb_id, content_kind="movie", now=now
                )
            )
            credit_rows.extend(
                credits_from_details(
                    details, tmdb_id=tmdb_id, content_kind="movie", now=now
                )
            )

            try:
                videos = tmdb.movie_videos(tmdb_id, title_languages(details))
            except Exception as exc:  # noqa: BLE001
                log.warning("discover_movies.videos_failed", tmdb_id=tmdb_id, error=str(exc))
                continue

            trailer_rows.extend(
                movie_trailer_rows(videos, tmdb_id=tmdb_id, release_date=release_date, now=now)
            )
        if limit is not None and len(seen_tmdb_ids) >= limit:
            break

    _upsert_movies(bq, movie_rows)
    _upsert_trailers(bq, trailer_rows)
    _upsert_watch_providers(bq, watch_rows)
    _upsert_credits(bq, credit_rows)
    log.info(
        "discover_movies.done",
        movies=len(movie_rows),
        trailers=len(trailer_rows),
        watch_providers=len(watch_rows),
        credits=len(credit_rows),
    )
    return len(movie_rows), len(trailer_rows)


def movie_trailer_rows(
    videos: list[dict[str, Any]],
    *,
    tmdb_id: int,
    release_date: date | None,
    now: datetime,
    tracking_status: TrackingStatus = "active",
) -> list[TrailerRow]:
    return [
        TrailerRow(
            youtube_video_id=str(v["key"]),
            content_kind="movie",
            movie_tmdb_id=tmdb_id,
            video_type=classify_video_type(v),  # type: ignore[arg-type]
            name=v.get("name"),
            published_at=parse_published_at(v.get("published_at")),
            official=v.get("official"),
            tracking_end_date=release_date,
            first_seen_at=now,
            last_collected_at=now,
            tracking_status=tracking_status,
        )
        for v in videos
        if is_trailer_video(v)
    ]


def run_backfill_movie_trailers(
    *,
    tmdb: TMDBClient,
    youtube: YouTubeClient,
    bq: BigQueryClient,
    settings: Settings,
    since: date,
    today: date | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """Re-fetch TMDB videos in each title's own languages for movies already in `movies`.

    One-off repair for the non-English trailers the English-only /videos call used
    to drop (English ones are skipped — discovery already has them). Covers
    movies released on/after `since`, including ones that already left the discovery
    window. New trailers of movies past `release + tracking_grace_days` are inserted
    as `ended` (so comments never label a post-release snapshot `at_discovery`), but
    every new trailer gets one stats snapshot now so it has views/channel/duration.

    Returns (movies_scanned, new_trailers).
    """
    from movie_trailers.pipeline.stats import collect_stats

    today = today or datetime.now(UTC).date()
    sql = f"""
    SELECT tmdb_id, original_language, spoken_languages, primary_release_date
    FROM `{bq.project}.{bq.dataset}.movies`
    WHERE primary_release_date >= @since
    ORDER BY popularity DESC
    LIMIT @limit
    """
    movies = bq.query(sql, {"since": since, "limit": limit if limit is not None else 1_000_000})
    known = {
        r["youtube_video_id"]
        for r in bq.query(f"SELECT youtube_video_id FROM `{bq.project}.{bq.dataset}.trailers`")
    }

    rows: list[TrailerRow] = []
    now = datetime.now(UTC)
    for m in movies:
        release_date: date | None = m["primary_release_date"]
        try:
            videos = tmdb.movie_videos(int(m["tmdb_id"]), title_languages(m))
        except Exception as exc:  # noqa: BLE001
            log.warning("backfill.videos_failed", tmdb_id=m["tmdb_id"], error=str(exc))
            continue
        past_window = release_date is not None and today > release_date + timedelta(
            days=settings.tracking_grace_days
        )
        # Only what the old English-only request dropped; English videos were already
        # collected daily while the movie was in the discovery window.
        videos = [v for v in videos if v.get("iso_639_1") != "en"]
        rows.extend(
            r
            for r in movie_trailer_rows(
                videos,
                tmdb_id=int(m["tmdb_id"]),
                release_date=release_date,
                now=now,
                tracking_status="ended" if past_window else "active",
            )
            if r.youtube_video_id not in known
            # Post-release uploads ("Coming to Disney+", home-video spots) are outside
            # the trailer window and would never have been tracked by discovery.
            and not (
                release_date and r.published_at and r.published_at.date() > release_date
            )
        )

    _upsert_trailers(bq, rows)
    new_ids = list(dict.fromkeys(r.youtube_video_id for r in rows))
    collect_stats(youtube=youtube, bq=bq, video_ids=new_ids, today=today)
    log.info("backfill.done", movies=len(movies), new_trailers=len(new_ids))
    return len(movies), len(new_ids)


def _upsert_movies(bq: BigQueryClient, rows: list[MovieRow]) -> None:
    fields = [
        "tmdb_id", "imdb_id", "title", "original_title", "original_language",
        "primary_release_date", "genres", "status", "popularity",
        "poster_path", "backdrop_path", "overview", "tagline", "homepage",
        "runtime_minutes", "production_companies", "origin_countries",
        "spoken_languages", "collected_at",
    ]
    bq.merge_rows(
        table="movies",
        rows=rows,
        merge_keys=["tmdb_id"],
        update_fields=[c for c in fields if c != "tmdb_id"],
        insert_fields=fields,
    )


def _upsert_watch_providers(bq: BigQueryClient, rows: list[WatchProviderRow]) -> None:
    if not rows:
        return
    fields = [
        "tmdb_id", "content_kind", "region", "kind", "provider_id",
        "provider_name", "logo_path", "display_priority", "link", "collected_at",
    ]
    bq.merge_rows(
        table="watch_providers",
        rows=rows,
        merge_keys=["tmdb_id", "content_kind", "region", "kind", "provider_id"],
        update_fields=["provider_name", "logo_path", "display_priority", "link", "collected_at"],
        insert_fields=fields,
    )


def _upsert_credits(bq: BigQueryClient, rows: list[CreditRow]) -> None:
    if not rows:
        return
    fields = [
        "tmdb_id", "content_kind", "credit_kind", "person_id", "name",
        "character", "job", "department", "profile_path", "order_index", "collected_at",
    ]
    # job/character is part of the natural key (a person can be both Director and Writer),
    # but BQ MERGE keys must be non-null. We approximate with a unique-enough key including job.
    bq.merge_rows(
        table="credits",
        rows=rows,
        merge_keys=["tmdb_id", "content_kind", "credit_kind", "person_id"],
        update_fields=["name", "character", "job", "department", "profile_path",
                       "order_index", "collected_at"],
        insert_fields=fields,
    )


def _upsert_trailers(bq: BigQueryClient, rows: list[TrailerRow]) -> None:
    # Don't overwrite first_seen_at, comments_*_captured_at, tracking_status,
    # or comments_disabled — those are managed by stats/comments pipelines.
    fields = [
        "youtube_video_id", "content_kind", "movie_tmdb_id", "tv_tmdb_id", "tv_season_number",
        "video_type", "name", "published_at", "channel_id", "channel_title", "official",
        "tracking_end_date", "thumbnail_url", "first_seen_at", "last_collected_at",
        "tracking_status", "comments_disabled", "comments_at_discovery_captured_at",
        "comments_pre_release_captured_at", "transcript_captured_at",
    ]
    discovery_update_fields = [
        "video_type", "name", "published_at", "official",
        "tracking_end_date", "last_collected_at",
        # content_kind + linkage fields shouldn't change once a trailer is bound
    ]
    bq.merge_rows(
        table="trailers",
        rows=rows,
        merge_keys=["youtube_video_id"],
        update_fields=discovery_update_fields,
        insert_fields=fields,
    )
