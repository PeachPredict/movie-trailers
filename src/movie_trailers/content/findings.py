"""Detect newsworthy states in the trailer dataset.

One BigQuery scan returns per-movie velocity/quality aggregates plus a daily
new-views series (for sparklines); Python classifies each movie into at most one
Finding and ranks by a salience score that rewards bigger audiences and stronger
signals. All read-only.

The detectors encode what this conversation established empirically:
- `trailer_due` — demand spent + release near + no recent trailer. 77% of
  historical launches fired in exactly this state, so it reads as a *prediction*
  ("a new trailer is imminent"), which is the share-worthy hook.
- `surging` — view velocity still near its peak: something is taking off now.
- `quality_slide` — like/view ratio falling fast: broad reach, fading love.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel

from movie_trailers.clients.bigquery import BigQueryClient

# --- detector thresholds ---
SPENT_VS_PEAK = 0.25  # current velocity below 25% of peak = demand "spent"
SURGING_VS_PEAK = 0.80  # still ≥80% of peak = "taking off"
DUE_MAX_DAYS_TO_RELEASE = 45  # a launch only matters if release is this close
DUE_MIN_TRAILER_AGE = 10  # … and nothing new has dropped in this many days
SURGING_MAX_TRAILER_AGE = 10  # surge must be from a recent trailer
SLIDE_RATIO = 0.90  # recent like/view below 90% of older = a real slide
FATIGUE_DROP_PTS = 5.0  # first→last comment-excitement drop (points) that flags fatigue


class Finding(BaseModel):
    """One newsworthy movie state, ready to be drafted into a post."""

    kind: str  # 'trailer_due' | 'surging' | 'quality_slide'
    movie_tmdb_id: int
    title: str
    headline: str  # one-line internal summary; drafts.py expands it
    salience: float
    metrics: dict[str, Any]
    series: list[dict[str, Any]]  # [{date, dv}] daily new-views, for a sparkline


def _scan(bq: BigQueryClient, days: int) -> list[dict[str, Any]]:
    """Per-movie velocity + quality aggregates over the window, one row per movie."""
    sql = f"""
    WITH daily AS (
      SELECT t.movie_tmdb_id, v.collected_date,
             SUM(v.delta_views) AS dv,
             SUM(v.view_count)  AS views,
             SUM(v.like_count)  AS likes
      FROM `{bq.project}.{bq.dataset}.vw_trailer_daily_metrics` v
      JOIN `{bq.project}.{bq.dataset}.trailers` t USING (youtube_video_id)
      WHERE t.content_kind = 'movie' AND t.movie_tmdb_id IS NOT NULL
        AND v.collected_date >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
      GROUP BY 1, 2
    ),
    agg AS (
      SELECT
        movie_tmdb_id,
        AVG(IF(collected_date > DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY), dv, NULL)) AS cur_vel,
        MAX(dv) AS peak_vel,
        AVG(IF(collected_date > DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY),
               SAFE_DIVIDE(likes, views), NULL)) AS lv_recent,
        AVG(IF(collected_date <= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY),
               SAFE_DIVIDE(likes, views), NULL)) AS lv_older,
        ARRAY_AGG(STRUCT(collected_date AS date, dv) ORDER BY collected_date) AS series
      FROM daily GROUP BY 1
    ),
    tr AS (
      SELECT movie_tmdb_id,
             COUNT(*) AS n_trailers,
             COUNTIF(tracking_status = 'active') AS active_trailers,
             DATE_DIFF(CURRENT_DATE(), DATE(MAX(published_at)), DAY) AS last_trailer_age_days
      FROM `{bq.project}.{bq.dataset}.trailers`
      WHERE content_kind = 'movie' AND movie_tmdb_id IS NOT NULL AND published_at IS NOT NULL
      GROUP BY 1
    )
    SELECT a.movie_tmdb_id, a.cur_vel, a.peak_vel, a.lv_recent, a.lv_older, a.series,
           tr.n_trailers, tr.active_trailers, tr.last_trailer_age_days,
           m.title, m.primary_release_date,
           DATE_DIFF(m.primary_release_date, CURRENT_DATE(), DAY) AS days_to_release
    FROM agg a
    JOIN tr USING (movie_tmdb_id)
    JOIN `{bq.project}.{bq.dataset}.movies` m ON m.tmdb_id = a.movie_tmdb_id
    WHERE a.peak_vel > 0
    """
    return bq.query(sql, {"days": days})


def _scan_excitement(bq: BigQueryClient) -> list[dict[str, Any]]:
    """Per-movie comment-excitement decay across its trailer sequence (latest model).

    Returns [] if the excitement table/view isn't present yet (fresh dataset), so
    findings degrade gracefully before the excitement phase has ever run.
    """
    sql = f"""
    WITH e AS (
      SELECT movie_tmdb_id, trailer_ordinal, excitement, published_at, n_trailers
      FROM `{bq.project}.{bq.dataset}.vw_movie_trailer_excitement`
      WHERE model_version = (
        SELECT model_version FROM `{bq.project}.{bq.dataset}.trailer_comment_excitement`
        WHERE scored_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 730 DAY)
        GROUP BY model_version ORDER BY MAX(scored_date) DESC, model_version DESC LIMIT 1
      )
    ),
    agg AS (
      SELECT movie_tmdb_id, ANY_VALUE(n_trailers) AS n_trailers, COUNT(*) AS n,
             SAFE_DIVIDE(COVAR_POP(trailer_ordinal, excitement), VAR_POP(trailer_ordinal)) AS slope,
             ARRAY_AGG(excitement ORDER BY trailer_ordinal)[OFFSET(0)] AS first_exc,
             ARRAY_AGG(excitement ORDER BY trailer_ordinal DESC)[OFFSET(0)] AS last_exc,
             ARRAY_AGG(STRUCT(published_at AS date, excitement AS dv) ORDER BY trailer_ordinal) AS series
      FROM e GROUP BY 1 HAVING n >= 2 AND VAR_POP(trailer_ordinal) > 0
    ),
    pk AS (
      SELECT t.movie_tmdb_id, MAX(v.delta_views) AS peak_vel
      FROM `{bq.project}.{bq.dataset}.vw_trailer_daily_metrics` v
      JOIN `{bq.project}.{bq.dataset}.trailers` t USING (youtube_video_id)
      WHERE t.content_kind = 'movie' AND t.movie_tmdb_id IS NOT NULL
        AND v.collected_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 120 DAY)
      GROUP BY 1
    )
    SELECT a.movie_tmdb_id, a.n_trailers, a.slope, a.first_exc, a.last_exc, a.series,
           pk.peak_vel, m.title
    FROM agg a
    LEFT JOIN pk USING (movie_tmdb_id)
    JOIN `{bq.project}.{bq.dataset}.movies` m ON m.tmdb_id = a.movie_tmdb_id
    """
    try:
        return bq.query(sql)
    except Exception:  # noqa: BLE001 — table/view absent (fresh dataset) → no excitement findings
        return []


def _scale(peak_vel: float | None) -> float:
    """Audience-size weight: log of peak daily new-views, floored so small movies rank low."""
    return math.log10(max(peak_vel or 0, 10))


def detect_findings(bq: BigQueryClient, *, days: int = 30, top: int = 6) -> list[Finding]:
    """Scan the dataset and return the top-`top` newsworthy findings.

    At most one finding per movie (the highest-salience kind), so a digest of
    findings covers distinct movies rather than piling onto one.
    """
    best: dict[int, Finding] = {}

    def offer(f: Finding) -> None:
        cur = best.get(f.movie_tmdb_id)
        if cur is None or f.salience > cur.salience:
            best[f.movie_tmdb_id] = f

    for r in _scan(bq, days):
        series = [{"date": s["date"], "dv": s["dv"]} for s in (r["series"] or [])]
        cur, peak = r["cur_vel"], r["peak_vel"]
        vs_peak = (cur / peak) if cur is not None and peak else None
        d2r = r["days_to_release"]
        age = r["last_trailer_age_days"]
        scale = _scale(peak)
        base = {
            "vs_peak": vs_peak,
            "peak_daily_views": peak,
            "current_daily_views": cur,
            "days_to_release": d2r,
            "last_trailer_age_days": age,
            "n_trailers": r["n_trailers"],
            "active_trailers": r["active_trailers"],
        }

        # trailer_due: the prediction hook
        if (
            vs_peak is not None
            and vs_peak < SPENT_VS_PEAK
            and d2r is not None
            and 1 <= d2r <= DUE_MAX_DAYS_TO_RELEASE
            and (age or 0) >= DUE_MIN_TRAILER_AGE
        ):
            proximity = 1 - d2r / (DUE_MAX_DAYS_TO_RELEASE + 1)
            offer(
                Finding(
                    kind="trailer_due",
                    movie_tmdb_id=r["movie_tmdb_id"],
                    title=r["title"] or f"movie {r['movie_tmdb_id']}",
                    headline=(
                        f"{r['title']} opens in {d2r}d; trailer demand spent "
                        f"({vs_peak:.0%} of peak) — new trailer likely imminent"
                    ),
                    salience=scale * (1 - vs_peak) * proximity,
                    metrics=base,
                    series=series,
                )
            )

        # surging: something taking off now
        if (
            vs_peak is not None
            and vs_peak >= SURGING_VS_PEAK
            and age is not None
            and age <= SURGING_MAX_TRAILER_AGE
        ):
            offer(
                Finding(
                    kind="surging",
                    movie_tmdb_id=r["movie_tmdb_id"],
                    title=r["title"] or f"movie {r['movie_tmdb_id']}",
                    headline=(
                        f"{r['title']} trailer surging — still {vs_peak:.0%} of peak "
                        f"views {age}d in"
                    ),
                    salience=scale * vs_peak,
                    metrics=base,
                    series=series,
                )
            )

        # quality_slide: broad reach, fading love
        lv_r, lv_o = r["lv_recent"], r["lv_older"]
        if lv_r and lv_o and lv_r < lv_o * SLIDE_RATIO:
            drop = 1 - lv_r / lv_o
            offer(
                Finding(
                    kind="quality_slide",
                    movie_tmdb_id=r["movie_tmdb_id"],
                    title=r["title"] or f"movie {r['movie_tmdb_id']}",
                    headline=(
                        f"{r['title']} cooling — like/view down {drop:.0%} this week"
                    ),
                    salience=scale * drop,
                    metrics={**base, "like_view_recent": lv_r, "like_view_older": lv_o},
                    series=series,
                )
            )

    # excitement_fatigue: comment reactions cooling across the trailer sequence
    for r in _scan_excitement(bq):
        first, last = r["first_exc"], r["last_exc"]
        if first is None or last is None:
            continue
        drop = first - last
        if drop < FATIGUE_DROP_PTS:
            continue
        series = [{"date": s["date"], "dv": s["dv"]} for s in (r["series"] or [])]
        offer(
            Finding(
                kind="excitement_fatigue",
                movie_tmdb_id=r["movie_tmdb_id"],
                title=r["title"] or f"movie {r['movie_tmdb_id']}",
                headline=(
                    f"{r['title']} — comment excitement cooling {first:.0f}→{last:.0f} "
                    f"across {r['n_trailers']} trailers"
                ),
                salience=_scale(r["peak_vel"]) * (drop / 20.0),
                metrics={
                    "first_excitement": round(first, 1),
                    "last_excitement": round(last, 1),
                    "excitement_delta": round(last - first, 1),
                    "slope_per_trailer": round(r["slope"], 3) if r["slope"] is not None else None,
                    "n_trailers": r["n_trailers"],
                },
                series=series,
            )
        )

    ranked = sorted(best.values(), key=lambda f: f.salience, reverse=True)
    return ranked[:top]
