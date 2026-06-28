"""Acceptance gate: does the distilled student reproduce the Claude decay finding?

Re-runs the original study using the SHIPPED runtime path (`score_comment_sets`,
ONNX + numpy) over every movie with >=2 sequential at_discovery trailers, and
asserts the three documented signatures hold:
  1. >=55% of movies decline first->last  (study: 63%)
  2. negative median first->last delta      (study: -5.5 pts)
  3. dose-response: more trailers -> bigger decline
Also confirms the lexicon-only proxy stays BLIND (proving the student learned the
semantic fatigue a regex can't see).

Run:
    cd <repo root>
    EXCITEMENT_MODEL_DIR=benchmarks/excitement/artifacts uv run python benchmarks/excitement/06_acceptance_test.py
"""
from __future__ import annotations

import os
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("EXCITEMENT_MODEL_DIR", str(Path("benchmarks/excitement/artifacts")))

from movie_trailers.clients.bigquery import BigQueryClient  # noqa: E402
from movie_trailers.config import load_settings  # noqa: E402
from movie_trailers.content import excitement_model as em  # noqa: E402

SQL = """
WITH tr AS (
  SELECT movie_tmdb_id, youtube_video_id, published_at
  FROM `{ds}.trailers`
  WHERE content_kind='movie' AND movie_tmdb_id IS NOT NULL AND published_at IS NOT NULL
),
cs AS (
  SELECT youtube_video_id,
         ARRAY_AGG(STRUCT(text, like_count, rank) ORDER BY rank LIMIT 30) AS comments
  FROM `{ds}.trailer_comments_snapshots`
  WHERE snapshot_date >= '2026-01-01' AND snapshot_kind='at_discovery' AND text IS NOT NULL
  GROUP BY youtube_video_id
),
m AS (
  SELECT movie_tmdb_id FROM tr JOIN (SELECT DISTINCT youtube_video_id FROM cs) USING(youtube_video_id)
  GROUP BY 1 HAVING COUNT(*) >= 2
)
SELECT tr.movie_tmdb_id, tr.youtube_video_id, tr.published_at, cs.comments
FROM cs JOIN tr USING(youtube_video_id) JOIN m USING(movie_tmdb_id)
"""


def _lexicon_score(comments: list[dict]) -> float:
    texts = [(c.get("text") or "") for c in comments]
    return em._lexicon_rate(texts)


def _slope(ys: list[float]) -> float:
    n = len(ys)
    xs = list(range(n))
    mx, my = st.mean(xs), st.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den if den else 0.0


def _summarize(seqs: dict[int, list[float]], label: str) -> float:
    fl = [e[-1] - e[0] for e in seqs.values()]
    dec = sum(1 for d in fl if d < 0)
    n = len(seqs)
    print(f"\n[{label}]  movies={n}")
    print(f"  declined first->last: {dec}/{n} = {dec / n:.0%}")
    print(f"  median first->last delta: {st.median(fl):+.2f}   mean slope: {st.mean([_slope(e) for e in seqs.values()]):+.3f}")
    bucket = defaultdict(list)
    for e in seqs.values():
        b = "2" if len(e) == 2 else ("3-4" if len(e) <= 4 else "5+")
        bucket[b].append(e[-1] - e[0])
    for b in ["2", "3-4", "5+"]:
        if bucket[b]:
            print(f"    {b:>3} trailers: n={len(bucket[b]):3d}  median first->last {st.median(bucket[b]):+.1f}")
    return dec / n


def main() -> None:
    s = load_settings()
    bq = BigQueryClient(project=s.gcp_project, dataset=s.bq_dataset, location=s.bq_location)
    rows = bq.query(SQL.format(ds=f"{s.gcp_project}.{s.bq_dataset}"))

    by_movie: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_movie[r["movie_tmdb_id"]].append(
            {"vid": r["youtube_video_id"], "pub": r["published_at"],
             "comments": [{"text": c["text"], "like_count": c["like_count"], "rank": c["rank"]} for c in r["comments"]]}
        )
    for m in by_movie:
        by_movie[m].sort(key=lambda x: x["pub"])

    # Student scores (shipped runtime path), batched.
    flat = [t["comments"] for ts in by_movie.values() for t in ts]
    holder: dict = {}
    scores: list[float | None] = []
    for i in range(0, len(flat), 256):
        scores.extend(em.score_comment_sets(flat[i : i + 256], holder=holder))
    print(f"scored {len(flat)} trailers with model_version={em.model_version(holder=holder)}")

    student: dict[int, list[float]] = {}
    proxy: dict[int, list[float]] = {}
    k = 0
    for m, ts in by_movie.items():
        s_seq, p_seq = [], []
        for t in ts:
            sc = scores[k]
            k += 1
            if sc is not None:
                s_seq.append(sc)
                p_seq.append(_lexicon_score(t["comments"]))
        if len(s_seq) >= 2:
            student[m] = s_seq
            proxy[m] = p_seq

    student_decline = _summarize(student, "STUDENT (distilled, shipped path)")
    proxy_decline = _summarize(proxy, "LEXICON PROXY (should be blind ~50%)")

    median_fl = st.median([e[-1] - e[0] for e in student.values()])
    bucket = defaultdict(list)
    for e in student.values():
        b = "2" if len(e) == 2 else ("3-4" if len(e) <= 4 else "5+")
        bucket[b].append(e[-1] - e[0])
    dose_ok = st.median(bucket["5+"]) < st.median(bucket["2"]) if bucket["5+"] and bucket["2"] else True

    print("\n=== ACCEPTANCE ===")
    checks = {
        "decline_rate >= 55%": student_decline >= 0.55,
        "median first->last < 0": median_fl < 0,
        "dose-response (5+ worse than 2)": dose_ok,
        "proxy stays blind (decline < 58%)": proxy_decline < 0.58,
    }
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if not all(checks.values()):
        sys.exit(1)
    print("\nALL CHECKS PASSED — the cheap student reproduces the finding.")


if __name__ == "__main__":
    main()
