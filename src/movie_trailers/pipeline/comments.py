from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal

import structlog

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.clients.youtube import (
    CommentsDisabledError,
    QuotaExceededError,
    YouTubeClient,
)
from movie_trailers.config import Settings
from movie_trailers.models import TrailerCommentSnapshotRow

log = structlog.get_logger()

TOP_N = 30


def run_comments(
    *,
    youtube: YouTubeClient,
    bq: BigQueryClient,
    settings: Settings,
    today: date | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """Capture at-discovery and pre-release comment snapshots.

    Returns (at_discovery_count, pre_release_count) — count of trailers captured.
    """
    today = today or datetime.now(UTC).date()
    at_disco = _select_targets(bq, kind="at_discovery", today=today, limit=limit)
    pre_rel = _select_targets(bq, kind="pre_release", today=today, limit=limit)

    log.info("comments.targets", at_discovery=len(at_disco), pre_release=len(pre_rel))

    at_done = _capture_for(
        youtube, bq, video_ids=at_disco, kind="at_discovery", today=today
    )
    pre_done = _capture_for(
        youtube, bq, video_ids=pre_rel, kind="pre_release", today=today
    )

    log.info(
        "comments.done",
        at_discovery=at_done,
        pre_release=pre_done,
        quota_units_used=youtube.quota_units_used,
    )
    return at_done, pre_done


def _select_targets(
    bq: BigQueryClient,
    *,
    kind: Literal["at_discovery", "pre_release"],
    today: date,
    limit: int | None,
) -> list[str]:
    if kind == "at_discovery":
        where = (
            "tracking_status = 'active'"
            " AND comments_disabled = FALSE"
            " AND comments_at_discovery_captured_at IS NULL"
        )
    else:
        where = (
            "tracking_status = 'active'"
            " AND comments_disabled = FALSE"
            " AND comments_pre_release_captured_at IS NULL"
            " AND tracking_end_date IS NOT NULL"
            " AND tracking_end_date BETWEEN @today AND DATE_ADD(@today, INTERVAL 1 DAY)"
        )
    cap = limit if limit is not None else 100000
    sql = f"""
    SELECT youtube_video_id
    FROM `{bq.project}.{bq.dataset}.trailers`
    WHERE {where}
    LIMIT @cap
    """
    rows = bq.query(sql, {"today": today, "cap": cap})
    return [r["youtube_video_id"] for r in rows]


def _capture_for(
    youtube: YouTubeClient,
    bq: BigQueryClient,
    *,
    video_ids: list[str],
    kind: Literal["at_discovery", "pre_release"],
    today: date,
) -> int:
    if not video_ids:
        return 0
    captured_at = datetime.now(UTC)
    snapshot_rows: list[TrailerCommentSnapshotRow] = []
    success_ids: list[str] = []
    disabled_ids: list[str] = []

    for vid in video_ids:
        try:
            result = youtube.comment_threads_top(vid, max_results=TOP_N)
        except CommentsDisabledError:
            disabled_ids.append(vid)
            continue
        except QuotaExceededError:
            log.warning("comments.quota_exhausted", processed=len(success_ids))
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("comments.fetch_failed", video_id=vid, error=str(exc))
            continue

        for rank, item in enumerate(result.items, start=1):
            top = (item.get("snippet") or {}).get("topLevelComment", {}).get("snippet", {})
            snapshot_rows.append(
                TrailerCommentSnapshotRow(
                    youtube_video_id=vid,
                    snapshot_kind=kind,
                    snapshot_date=today,
                    comment_id=item["id"],
                    text=top.get("textDisplay") or top.get("textOriginal"),
                    like_count=_to_int(top.get("likeCount")),
                    total_reply_count=_to_int(
                        (item.get("snippet") or {}).get("totalReplyCount")
                    ),
                    author_channel_id=(top.get("authorChannelId") or {}).get("value"),
                    author_display_name=top.get("authorDisplayName"),
                    published_at=_parse_dt(top.get("publishedAt")),
                    updated_at=_parse_dt(top.get("updatedAt")),
                    rank=rank,
                    collected_at=captured_at,
                )
            )
        success_ids.append(vid)

    _insert_snapshots(bq, snapshot_rows)
    _stamp_capture(bq, success_ids, kind=kind, captured_at=captured_at)
    _mark_comments_disabled(bq, disabled_ids, when=captured_at)
    return len(success_ids)


def _insert_snapshots(bq: BigQueryClient, rows: list[TrailerCommentSnapshotRow]) -> None:
    if not rows:
        return
    fields = [
        "youtube_video_id", "snapshot_kind", "snapshot_date", "comment_id",
        "text", "like_count", "total_reply_count", "author_channel_id",
        "author_display_name", "published_at", "updated_at", "rank", "collected_at",
    ]
    snapshot_date = rows[0].snapshot_date.isoformat()
    bq.merge_rows(
        table="trailer_comments_snapshots",
        rows=rows,
        merge_keys=["youtube_video_id", "snapshot_kind", "comment_id"],
        update_fields=[
            c for c in fields if c not in {"youtube_video_id", "snapshot_kind", "comment_id"}
        ],
        insert_fields=fields,
        partition_filter=f"T.snapshot_date = DATE('{snapshot_date}')",
    )


def _stamp_capture(
    bq: BigQueryClient,
    ids: list[str],
    *,
    kind: Literal["at_discovery", "pre_release"],
    captured_at: datetime,
) -> None:
    if not ids:
        return
    column = (
        "comments_at_discovery_captured_at"
        if kind == "at_discovery"
        else "comments_pre_release_captured_at"
    )
    rows = [{"youtube_video_id": v, "captured_at": captured_at} for v in ids]
    bq.update_from_dicts(
        table="trailers",
        rows=rows,
        merge_keys=["youtube_video_id"],
        update_clause_sql=f"T.{column} = S.captured_at",
    )


def _mark_comments_disabled(bq: BigQueryClient, ids: list[str], when: datetime) -> None:
    if not ids:
        return
    rows = [{"youtube_video_id": v, "when": when} for v in ids]
    bq.update_from_dicts(
        table="trailers",
        rows=rows,
        merge_keys=["youtube_video_id"],
        update_clause_sql="T.comments_disabled = TRUE, T.last_collected_at = S.when",
    )


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
