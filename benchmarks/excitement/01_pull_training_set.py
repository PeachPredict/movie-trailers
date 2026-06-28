"""Pull the trailer-level training set for the excitement distillation.

One row per `at_discovery` trailer that has comments: its top-30 comments (by
relevance rank), plus movie id / title / published_at for stratification and the
later acceptance test. Read-only against BigQuery.

Run:
    cd benchmarks/excitement
    uv run python 01_pull_training_set.py
Output: training_set.jsonl
"""
from __future__ import annotations

import json
from pathlib import Path

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.config import load_settings

HERE = Path(__file__).parent
OUT = HERE / "training_set.jsonl"
MIN_COMMENTS = 3

SQL = """
WITH tr AS (
  SELECT movie_tmdb_id, youtube_video_id, published_at
  FROM `{ds}.trailers`
  WHERE content_kind='movie' AND movie_tmdb_id IS NOT NULL AND published_at IS NOT NULL
),
cs AS (
  SELECT youtube_video_id,
         ARRAY_AGG(STRUCT(text, like_count, rank) ORDER BY rank LIMIT 30) AS comments,
         COUNT(*) AS n
  FROM `{ds}.trailer_comments_snapshots`
  WHERE snapshot_date >= '2026-01-01' AND snapshot_kind='at_discovery' AND text IS NOT NULL
  GROUP BY youtube_video_id
)
SELECT tr.movie_tmdb_id, tr.youtube_video_id, tr.published_at,
       m.title, cs.comments, cs.n
FROM cs
JOIN tr USING (youtube_video_id)
LEFT JOIN `{ds}.movies` m ON m.tmdb_id = tr.movie_tmdb_id
WHERE cs.n >= {min_comments}
"""


def main() -> None:
    s = load_settings()
    bq = BigQueryClient(project=s.gcp_project, dataset=s.bq_dataset, location=s.bq_location)
    ds = f"{s.gcp_project}.{s.bq_dataset}"
    rows = bq.query(SQL.format(ds=ds, min_comments=MIN_COMMENTS))
    with OUT.open("w") as f:
        for r in rows:
            comments = [
                {"text": c["text"], "like_count": c["like_count"], "rank": c["rank"]}
                for c in r["comments"]
            ]
            f.write(
                json.dumps(
                    {
                        "youtube_video_id": r["youtube_video_id"],
                        "movie_tmdb_id": r["movie_tmdb_id"],
                        "title": r["title"],
                        "published_at": r["published_at"].isoformat() if r["published_at"] else None,
                        "comments": comments,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    movies = len({r["movie_tmdb_id"] for r in rows})
    print(f"wrote {len(rows)} trailers across {movies} movies → {OUT}")


if __name__ == "__main__":
    main()
