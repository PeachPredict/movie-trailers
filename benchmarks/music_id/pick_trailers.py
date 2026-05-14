"""Populate trailers.json with Bollywood / regional-Indian trailers from BigQuery.

Filter criteria:
- movies whose `original_language` is one of {hi, ta, te, ml, kn, pa, bn, mr}, OR
  whose `origin_countries` array contains 'IN'
- joined to active trailers in the `trailers` table
- ordered by view count (joined from the latest `trailer_stats_daily` row)

Run:
    cd benchmarks/music_id
    uv run --with google-cloud-bigquery python pick_trailers.py --limit 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
TRAILERS_JSON = HERE / "trailers.json"

INDIC_LANGS = ("hi", "ta", "te", "ml", "kn", "pa", "bn", "mr")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=os.environ.get("GCP_PROJECT"))
    ap.add_argument("--dataset", default=os.environ.get("BQ_DATASET", "movie_trailers"))
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument(
        "--min-duration",
        type=int,
        default=45,
        help="Skip trailers shorter than this (15s windows need room).",
    )
    args = ap.parse_args()

    if not args.project:
        print("ERROR: --project or GCP_PROJECT env var required", file=sys.stderr)
        return 1

    from google.cloud import bigquery

    client = bigquery.Client(project=args.project)

    sql = f"""
    WITH latest_stats AS (
      SELECT youtube_video_id, view_count, collected_date,
             ROW_NUMBER() OVER (PARTITION BY youtube_video_id ORDER BY collected_date DESC) AS rn
      FROM `{args.project}.{args.dataset}.trailer_stats_daily`
    ),
    bollywood_movies AS (
      SELECT tmdb_id, title, original_language
      FROM `{args.project}.{args.dataset}.movies`
      WHERE original_language IN UNNEST(@indic_langs)
         OR 'IN' IN UNNEST(origin_countries)
    )
    SELECT
      t.youtube_video_id,
      m.title AS movie_title,
      m.original_language,
      t.video_type,
      t.name AS trailer_name,
      t.channel_title,
      t.duration_seconds,
      COALESCE(s.view_count, 0) AS view_count
    FROM `{args.project}.{args.dataset}.trailers` t
    JOIN bollywood_movies m ON t.movie_tmdb_id = m.tmdb_id
    LEFT JOIN latest_stats s ON s.youtube_video_id = t.youtube_video_id AND s.rn = 1
    WHERE t.content_kind = 'movie'
      AND t.duration_seconds >= @min_duration
    ORDER BY view_count DESC
    LIMIT @limit
    """

    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("indic_langs", "STRING", list(INDIC_LANGS)),
                bigquery.ScalarQueryParameter("min_duration", "INT64", args.min_duration),
                bigquery.ScalarQueryParameter("limit", "INT64", args.limit),
            ]
        ),
    )
    rows = list(job.result())

    if not rows:
        print(
            "No matching trailers found. Check your dataset has Indian-language "
            "movies and stats. You can hand-edit trailers.json instead."
        )
        return 1

    out = [
        {
            "youtube_video_id": r["youtube_video_id"],
            "movie_title": r["movie_title"],
            "original_language": r["original_language"],
            "video_type": r["video_type"],
            "trailer_name": r["trailer_name"],
            "channel_title": r["channel_title"],
            "duration_seconds": int(r["duration_seconds"] or 0),
            "view_count": int(r["view_count"] or 0),
        }
        for r in rows
    ]
    TRAILERS_JSON.write_text(json.dumps(out, indent=2) + "\n")
    print(f"Wrote {len(out)} trailers to {TRAILERS_JSON.name}")
    for r in out:
        print(
            f"  {r['youtube_video_id']}  [{r['original_language']}]  "
            f"{r['movie_title']!r} ({r['duration_seconds']}s, {r['view_count']:,} views)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
