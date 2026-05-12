from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import structlog

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.clients.youtube import YouTubeClient
from movie_trailers.config import Settings
from movie_trailers.models import TrailerStatsDailyRow
from movie_trailers.pipeline._common import parse_iso8601_duration

log = structlog.get_logger()

BATCH_SIZE = 50


def run_stats(
    *,
    youtube: YouTubeClient,
    bq: BigQueryClient,
    settings: Settings,
    today: date | None = None,
    limit: int | None = None,
) -> int:
    """Collect daily stats for every active trailer.

    Returns count of stats rows written. Updates `trailers.last_collected_at`,
    `tracking_status`, and `channel_id`/`channel_title` (the first time we see them).
    """
    today = today or datetime.now(UTC).date()
    active_ids = _select_active_video_ids(bq, settings, today, limit=limit)
    if not active_ids:
        log.info("stats.no_active_trailers")
        return 0

    stats_rows: list[TrailerStatsDailyRow] = []
    channel_updates: list[dict[str, Any]] = []
    unavailable_ids: list[str] = []
    now = datetime.now(UTC)

    for chunk in _chunks(active_ids, BATCH_SIZE):
        try:
            items = youtube.videos_list(chunk)
        except Exception as exc:
            log.error("stats.batch_failed", error=str(exc), chunk_size=len(chunk))
            raise
        returned_ids: set[str] = set()
        for item in items:
            vid = item["id"]
            returned_ids.add(vid)
            statistics = item.get("statistics") or {}
            snippet = item.get("snippet") or {}
            content_details = item.get("contentDetails") or {}
            status = item.get("status") or {}
            region_restriction = content_details.get("regionRestriction") or {}
            stats_rows.append(
                TrailerStatsDailyRow(
                    youtube_video_id=vid,
                    collected_date=today,
                    view_count=_to_int(statistics.get("viewCount")),
                    like_count=_to_int(statistics.get("likeCount")),
                    comment_count=_to_int(statistics.get("commentCount")),
                    favorite_count=_to_int(statistics.get("favoriteCount")),
                    collected_at=now,
                )
            )
            channel_updates.append(
                {
                    "youtube_video_id": vid,
                    "channel_id": snippet.get("channelId"),
                    "channel_title": snippet.get("channelTitle"),
                    "thumbnail_url": _pick_thumbnail(snippet.get("thumbnails")),
                    "description": snippet.get("description"),
                    "duration_seconds": parse_iso8601_duration(content_details.get("duration")),
                    "definition": content_details.get("definition"),
                    "embeddable": status.get("embeddable"),
                    "region_blocked": region_restriction.get("blocked") or [],
                    "last_collected_at": now,
                }
            )
        # IDs requested but not returned = deleted/private/region-blocked
        for missing in set(chunk) - returned_ids:
            unavailable_ids.append(missing)

    _insert_stats(bq, stats_rows, today)
    _apply_channel_updates(bq, channel_updates)
    _mark_unavailable(bq, unavailable_ids, now)
    _mark_ended(bq, today)
    log.info(
        "stats.done",
        active=len(active_ids),
        stats_written=len(stats_rows),
        unavailable=len(unavailable_ids),
        quota_units_used=youtube.quota_units_used,
    )
    return len(stats_rows)


def _select_active_video_ids(
    bq: BigQueryClient, settings: Settings, today: date, *, limit: int | None
) -> list[str]:
    sql = f"""
    SELECT youtube_video_id
    FROM `{bq.project}.{bq.dataset}.trailers`
    WHERE tracking_status = 'active'
      AND (published_at IS NULL OR DATE(published_at) <= @today)
      AND (
        tracking_end_date IS NULL
        OR @today <= DATE_ADD(tracking_end_date, INTERVAL @grace DAY)
      )
    ORDER BY tracking_end_date IS NULL, tracking_end_date ASC
    LIMIT @limit
    """
    params: dict[str, Any] = {
        "today": today,
        "grace": settings.tracking_grace_days,
        "limit": limit if limit is not None else settings.max_active_trailers,
    }
    rows = bq.query(sql, params)
    return [r["youtube_video_id"] for r in rows]


def _insert_stats(bq: BigQueryClient, rows: list[TrailerStatsDailyRow], today: date) -> None:
    if not rows:
        return
    fields = [
        "youtube_video_id", "collected_date", "view_count", "like_count",
        "comment_count", "favorite_count", "collected_at",
    ]
    bq.merge_rows(
        table="trailer_stats_daily",
        rows=rows,
        merge_keys=["youtube_video_id", "collected_date"],
        update_fields=[c for c in fields if c not in {"youtube_video_id", "collected_date"}],
        insert_fields=fields,
        partition_filter=f"T.collected_date = DATE('{today.isoformat()}')",
    )


def _apply_channel_updates(bq: BigQueryClient, updates: list[dict[str, Any]]) -> None:
    if not updates:
        return
    bq.update_from_dicts(
        table="trailers",
        rows=updates,
        merge_keys=["youtube_video_id"],
        update_clause_sql=(
            "T.channel_id = COALESCE(S.channel_id, T.channel_id),"
            " T.channel_title = COALESCE(S.channel_title, T.channel_title),"
            " T.thumbnail_url = COALESCE(S.thumbnail_url, T.thumbnail_url),"
            " T.description = COALESCE(S.description, T.description),"
            " T.duration_seconds = COALESCE(S.duration_seconds, T.duration_seconds),"
            " T.definition = COALESCE(S.definition, T.definition),"
            " T.embeddable = COALESCE(S.embeddable, T.embeddable),"
            " T.region_blocked = IF(ARRAY_LENGTH(S.region_blocked) > 0, S.region_blocked, T.region_blocked),"
            " T.last_collected_at = S.last_collected_at"
        ),
    )


_THUMB_PREFERENCE = ("maxres", "standard", "high", "medium", "default")


def _pick_thumbnail(thumbnails: dict[str, Any] | None) -> str | None:
    if not thumbnails:
        return None
    for size in _THUMB_PREFERENCE:
        url = (thumbnails.get(size) or {}).get("url")
        if url:
            return url
    return None


def _mark_unavailable(bq: BigQueryClient, ids: list[str], now: datetime) -> None:
    if not ids:
        return
    rows = [{"youtube_video_id": v, "now": now} for v in ids]
    bq.update_from_dicts(
        table="trailers",
        rows=rows,
        merge_keys=["youtube_video_id"],
        update_clause_sql="T.tracking_status = 'unavailable', T.last_collected_at = S.now",
    )


def _mark_ended(bq: BigQueryClient, today: date) -> None:
    sql = f"""
    UPDATE `{bq.project}.{bq.dataset}.trailers`
    SET tracking_status = 'ended'
    WHERE tracking_status = 'active'
      AND tracking_end_date IS NOT NULL
      AND @today > DATE_ADD(tracking_end_date, INTERVAL 30 DAY)
    """
    bq.query(sql, {"today": today})


def _chunks(seq: list[str], size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
