"""Excitement phase: score each trailer's comment-section excitement (0–100).

Runs the distilled local student (ONNX MiniLM + Ridge — see
`content.excitement_model`) over `at_discovery` comment snapshots. Zero YouTube
quota, zero LLM cost. Idempotent: a trailer is scored once per model_version
(re-scored only when the model is retrained to a new version).

Depends on the comments phase having written `at_discovery` snapshots, so it runs
right after it.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

import structlog

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.config import Settings
from movie_trailers.content import excitement_model as em
from movie_trailers.models import TrailerCommentExcitementRow

log = structlog.get_logger()

SCORE_BATCH = 256          # trailers per ONNX batch
_ID_CHUNK = 500            # video ids per comment-fetch query
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PARTITION_FLOOR = "DATE_SUB(CURRENT_DATE(), INTERVAL 730 DAY)"

_SELECT_TARGETS = f"""
WITH have AS (
  SELECT DISTINCT youtube_video_id
  FROM `{{ds}}.trailer_comments_snapshots`
  WHERE snapshot_kind = 'at_discovery' AND snapshot_date >= {_PARTITION_FLOOR}
),
done AS (
  SELECT youtube_video_id
  FROM `{{ds}}.trailer_comment_excitement`
  WHERE scored_date >= {_PARTITION_FLOOR} AND model_version = @model_version
)
SELECT h.youtube_video_id
FROM have h LEFT JOIN done d USING (youtube_video_id)
WHERE d.youtube_video_id IS NULL
"""

_FETCH_COMMENTS = f"""
SELECT
  youtube_video_id,
  ARRAY_AGG(STRUCT(text, like_count, rank) ORDER BY rank LIMIT 30) AS comments,
  COUNT(*) AS n_comments,
  AVG(like_count) AS mean_like_count,
  MAX(snapshot_date) AS source_snapshot_date
FROM `{{ds}}.trailer_comments_snapshots`
WHERE snapshot_kind = 'at_discovery' AND snapshot_date >= {_PARTITION_FLOOR}
  AND text IS NOT NULL
  AND youtube_video_id IN ({{ids}})
GROUP BY youtube_video_id
"""


def run_excitement(
    *,
    bq: BigQueryClient,
    settings: Settings,
    today: date | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """Score un-scored at_discovery trailers. Returns (scored, skipped_no_text)."""
    today = today or datetime.now(UTC).date()
    ds = f"{settings.gcp_project}.{settings.bq_dataset}"

    version = em.model_version()  # cheap: loads only the tiny head
    targets = [
        r["youtube_video_id"]
        for r in bq.query(_SELECT_TARGETS.format(ds=ds), {"model_version": version})
        if _VIDEO_ID_RE.match(r["youtube_video_id"])
    ]
    if limit:
        targets = targets[:limit]
    if not targets:
        log.info("excitement.no_targets", model_version=version)
        return 0, 0

    # Fetch pooled comments for the selected ids (chunked IN-lists; params are
    # scalar-only so ids are inlined — already charset-validated above).
    fetched: list[dict] = []
    for i in range(0, len(targets), _ID_CHUNK):
        chunk = targets[i : i + _ID_CHUNK]
        ids_sql = ", ".join(f"'{v}'" for v in chunk)
        fetched.extend(bq.query(_FETCH_COMMENTS.format(ds=ds, ids=ids_sql)))

    holder: dict = {}
    now = datetime.now(UTC)
    rows: list[TrailerCommentExcitementRow] = []
    skipped = 0
    for i in range(0, len(fetched), SCORE_BATCH):
        batch = fetched[i : i + SCORE_BATCH]
        comment_sets = [
            [{"text": c["text"], "like_count": c["like_count"], "rank": c["rank"]} for c in r["comments"]]
            for r in batch
        ]
        scores = em.score_comment_sets(comment_sets, holder=holder)
        for r, score in zip(batch, scores, strict=True):
            if score is None:
                skipped += 1
                continue
            rows.append(
                TrailerCommentExcitementRow(
                    youtube_video_id=r["youtube_video_id"],
                    model_version=version,
                    scored_date=today,
                    excitement=round(score, 2),
                    snapshot_kind="at_discovery",
                    n_comments=r["n_comments"],
                    mean_like_count=r["mean_like_count"],
                    source_snapshot_date=r["source_snapshot_date"],
                    created_at=now,
                )
            )

    if rows:
        fields = list(TrailerCommentExcitementRow.model_fields.keys())
        bq.merge_rows(
            table="trailer_comment_excitement",
            rows=rows,
            merge_keys=["youtube_video_id", "model_version"],
            update_fields=[f for f in fields if f not in ("youtube_video_id", "model_version")],
            insert_fields=fields,
            partition_filter=f"T.scored_date = DATE('{today.isoformat()}')",
        )
    log.info("excitement.done", scored=len(rows), skipped=skipped, model_version=version)
    return len(rows), skipped
