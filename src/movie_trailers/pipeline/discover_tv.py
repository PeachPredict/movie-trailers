from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.clients.tmdb import TMDBClient
from movie_trailers.config import Settings
from movie_trailers.models import CreditRow, TrailerRow, TvSeasonRow, TvShowRow, WatchProviderRow
from movie_trailers.pipeline._common import (
    classify_video_type,
    credits_from_details,
    is_trailer_video,
    parse_companies,
    parse_genres,
    parse_languages,
    parse_origin_countries,
    parse_published_at,
    watch_providers_from_details,
)

log = structlog.get_logger()


def run_discover_tv(
    *,
    tmdb: TMDBClient,
    bq: BigQueryClient,
    settings: Settings,
    today: date | None = None,
    limit: int | None = None,
) -> tuple[int, int, int]:
    """Discover upcoming TV shows, their upcoming seasons, and their trailers.

    Returns (shows_upserted, seasons_upserted, trailers_upserted).
    """
    today = today or datetime.now(UTC).date()
    horizon_end = today + timedelta(days=30 * settings.discover_window_months)

    seen_show_ids: set[int] = set()
    show_rows: list[TvShowRow] = []
    season_rows: list[TvSeasonRow] = []
    trailer_rows: list[TrailerRow] = []
    watch_rows: list[WatchProviderRow] = []
    credit_rows: list[CreditRow] = []

    for show in tmdb.discover_tv(
        first_air_date_gte=today.isoformat(),
        first_air_date_lte=horizon_end.isoformat(),
    ):
        tmdb_id = int(show["id"])
        if tmdb_id in seen_show_ids:
            continue
        seen_show_ids.add(tmdb_id)
        if limit is not None and len(seen_show_ids) > limit:
            break

        try:
            details = tmdb.tv_details(tmdb_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("discover_tv.details_failed", tmdb_id=tmdb_id, error=str(exc))
            continue

        now = datetime.now(UTC)
        first_air_str = details.get("first_air_date") or show.get("first_air_date")
        first_air_date = date.fromisoformat(first_air_str) if first_air_str else None
        show_rows.append(
            TvShowRow(
                tmdb_id=tmdb_id,
                name=details.get("name"),
                original_name=details.get("original_name"),
                original_language=details.get("original_language"),
                first_air_date=first_air_date,
                status=details.get("status"),
                genres=parse_genres(details.get("genres")),
                vote_average=details.get("vote_average"),
                vote_count=details.get("vote_count"),
                popularity=details.get("popularity"),
                poster_path=details.get("poster_path"),
                backdrop_path=details.get("backdrop_path"),
                overview=details.get("overview"),
                tagline=details.get("tagline"),
                homepage=details.get("homepage"),
                production_companies=parse_companies(details.get("production_companies")),
                networks=parse_companies(details.get("networks")),
                origin_countries=parse_origin_countries(details),
                spoken_languages=parse_languages(details.get("spoken_languages")),
                collected_at=now,
            )
        )
        watch_rows.extend(
            watch_providers_from_details(details, tmdb_id=tmdb_id, content_kind="tv", now=now)
        )
        credit_rows.extend(
            credits_from_details(details, tmdb_id=tmdb_id, content_kind="tv", now=now)
        )

        # Upcoming seasons only (air_date in the future or unknown).
        upcoming_seasons = _select_upcoming_seasons(details.get("seasons") or [], today)
        if not upcoming_seasons:
            continue

        for season in upcoming_seasons:
            season_number = int(season["season_number"])
            air_str = season.get("air_date")
            air_date_val = date.fromisoformat(air_str) if air_str else None
            season_rows.append(
                TvSeasonRow(
                    tv_tmdb_id=tmdb_id,
                    season_number=season_number,
                    name=season.get("name"),
                    air_date=air_date_val,
                    episode_count=season.get("episode_count"),
                    overview=season.get("overview"),
                    poster_path=season.get("poster_path"),
                    collected_at=now,
                )
            )

        # Most-imminent upcoming season anchors series-level trailers.
        anchor_season = min(
            upcoming_seasons,
            key=lambda s: s.get("air_date") or "9999-12-31",
        )
        anchor_season_number = int(anchor_season["season_number"])
        anchor_air_date = (
            date.fromisoformat(anchor_season["air_date"])
            if anchor_season.get("air_date")
            else None
        )

        # Per-season videos.
        for season in upcoming_seasons:
            season_number = int(season["season_number"])
            season_air = (
                date.fromisoformat(season["air_date"]) if season.get("air_date") else None
            )
            try:
                videos = tmdb.tv_season_videos(tmdb_id, season_number)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "discover_tv.season_videos_failed",
                    tmdb_id=tmdb_id,
                    season=season_number,
                    error=str(exc),
                )
                videos = []
            for v in videos:
                if not is_trailer_video(v):
                    continue
                trailer_rows.append(
                    _tv_trailer_row(
                        v,
                        tv_tmdb_id=tmdb_id,
                        tv_season_number=season_number,
                        tracking_end_date=season_air,
                        now=now,
                    )
                )

        # Series-level videos → attach to the anchor season.
        try:
            series_videos = tmdb.tv_videos(tmdb_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("discover_tv.series_videos_failed", tmdb_id=tmdb_id, error=str(exc))
            series_videos = []
        for v in series_videos:
            if not is_trailer_video(v):
                continue
            trailer_rows.append(
                _tv_trailer_row(
                    v,
                    tv_tmdb_id=tmdb_id,
                    tv_season_number=anchor_season_number,
                    tracking_end_date=anchor_air_date,
                    now=now,
                )
            )

    _upsert_shows(bq, show_rows)
    _upsert_seasons(bq, season_rows)
    _upsert_trailers(bq, trailer_rows)
    _upsert_watch_providers(bq, watch_rows)
    _upsert_credits(bq, credit_rows)
    log.info(
        "discover_tv.done",
        shows=len(show_rows),
        seasons=len(season_rows),
        trailers=len(trailer_rows),
        watch_providers=len(watch_rows),
        credits=len(credit_rows),
    )
    return len(show_rows), len(season_rows), len(trailer_rows)


def _select_upcoming_seasons(
    seasons: list[dict[str, Any]], today: date
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in seasons:
        if int(s.get("season_number", 0)) == 0:
            # season_number 0 = specials, skip
            continue
        air_str = s.get("air_date")
        if not air_str:
            # No air date yet — treat as upcoming (gives us a trailer-tracking anchor).
            out.append(s)
            continue
        try:
            if date.fromisoformat(air_str) >= today:
                out.append(s)
        except ValueError:
            continue
    return out


def _tv_trailer_row(
    v: dict[str, Any],
    *,
    tv_tmdb_id: int,
    tv_season_number: int,
    tracking_end_date: date | None,
    now: datetime,
) -> TrailerRow:
    return TrailerRow(
        youtube_video_id=str(v["key"]),
        content_kind="tv",
        tv_tmdb_id=tv_tmdb_id,
        tv_season_number=tv_season_number,
        video_type=classify_video_type(v),  # type: ignore[arg-type]
        name=v.get("name"),
        published_at=parse_published_at(v.get("published_at")),
        official=v.get("official"),
        tracking_end_date=tracking_end_date,
        first_seen_at=now,
        last_collected_at=now,
    )


def _upsert_shows(bq: BigQueryClient, rows: list[TvShowRow]) -> None:
    fields = [
        "tmdb_id", "name", "original_name", "original_language", "first_air_date",
        "status", "genres", "vote_average", "vote_count", "popularity",
        "poster_path", "backdrop_path", "overview", "tagline", "homepage",
        "production_companies", "networks", "origin_countries", "spoken_languages",
        "collected_at",
    ]
    bq.merge_rows(
        table="tv_shows",
        rows=rows,
        merge_keys=["tmdb_id"],
        update_fields=[c for c in fields if c != "tmdb_id"],
        insert_fields=fields,
    )


def _upsert_seasons(bq: BigQueryClient, rows: list[TvSeasonRow]) -> None:
    fields = [
        "tv_tmdb_id", "season_number", "name", "air_date",
        "episode_count", "overview", "poster_path", "collected_at",
    ]
    bq.merge_rows(
        table="tv_seasons",
        rows=rows,
        merge_keys=["tv_tmdb_id", "season_number"],
        update_fields=[c for c in fields if c not in {"tv_tmdb_id", "season_number"}],
        insert_fields=fields,
    )


def _upsert_trailers(bq: BigQueryClient, rows: list[TrailerRow]) -> None:
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
    ]
    bq.merge_rows(
        table="trailers",
        rows=rows,
        merge_keys=["youtube_video_id"],
        update_fields=discovery_update_fields,
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
    bq.merge_rows(
        table="credits",
        rows=rows,
        merge_keys=["tmdb_id", "content_kind", "credit_kind", "person_id"],
        update_fields=["name", "character", "job", "department", "profile_path",
                       "order_index", "collected_at"],
        insert_fields=fields,
    )
