"""The prediction log: persist calls, resolve them, score the record.

Two kinds of falsifiable prediction:

- `trailer_due` (from findings): "a new trailer lands within N days, before
  release". Resolved against the `trailers` table.
- `opening_tier` (recorded here, once per movie in its final week): "this movie's
  pre-release trailer views put it in the high / mid / low tier of the last 180
  days' releases — and its box office will land in the same tier". Resolved
  against `vw_movie_box_office` (ANCINE admissions, then OMDb US gross, then TMDB
  worldwide revenue, whichever exists), ranking the outcome within the same cohort.

Writes go through the same idempotent BigQuery helpers as the pipeline; one open
prediction per (movie, kind) at a time.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.content.findings import (
    TRAILER_DUE_HORIZON_DAYS,
    Finding,
    trailer_due_horizon,
)
from movie_trailers.models import ContentPredictionRow

HORIZON_DAYS = TRAILER_DUE_HORIZON_DAYS  # upper bound; capped at days-to-release per call

# --- opening_tier ---
OPENING_KIND = "opening_tier"
OPENING_MAX_DAYS_TO_RELEASE = 7  # the call is made in the final week
OPENING_MIN_PEAK_VIEWS = 1000  # at least one trailer-day with this many new views
OPENING_MIN_STATS_DAYS = 3
OPENING_COHORT_DAYS = 180  # tiers are relative to releases in this trailing window
OPENING_MIN_COHORT = 20
OPENING_HORIZON_DAYS = 45  # informational: when the first outcome is normally scorable
OPENING_EXPIRE_DAYS = 120  # no outcome by then → 'miss' with observed_metric='none' (unscorable)
TIER_HIGH_PCT = 0.80
TIER_LOW_PCT = 0.50
# (market, metric, days after release the number must be good through) — in priority order
MARKET_PRIORITY: list[tuple[str, str, int]] = [
    ("BR", "admissions", 28),
    ("US", "domestic_gross_usd", 28),
    ("WW", "revenue_usd", 45),
]

_FIELDS = list(ContentPredictionRow.model_fields.keys())


def tier_of(value: float, cohort: list[float]) -> str:
    """'high' (top 20%), 'low' (bottom half) or 'mid', by rank within `cohort`."""
    n = len(cohort)
    pct = sum(1 for c in cohort if c < value) / n if n else 0.0
    if pct >= TIER_HIGH_PCT:
        return "high"
    if pct < TIER_LOW_PCT:
        return "low"
    return "mid"


def percentile_of(value: float, cohort: list[float]) -> float:
    n = len(cohort)
    return sum(1 for c in cohort if c < value) / n if n else 0.0


def _table(bq: BigQueryClient, name: str) -> str:
    return f"`{bq.project}.{bq.dataset}.{name}`"


def open_predictions(bq: BigQueryClient) -> list[dict[str, Any]]:
    """All predictions still awaiting resolution."""
    return bq.query(
        f"SELECT * FROM {_table(bq, 'content_predictions')} WHERE status = 'open'"
    )


def record_predictions(
    bq: BigQueryClient,
    findings: list[Finding],
    *,
    today: date,
    horizon_days: int = HORIZON_DAYS,
) -> int:
    """Open a new prediction for each `trailer_due` finding lacking an open one.

    Idempotent: re-running on the same day merges to the same prediction_id, and
    a movie that already has an open prediction is skipped (no duplicate episode).
    The horizon is `min(horizon_days, days_to_release)`: the call is "a trailer
    before release", so a movie opening in 9 days gets a 9-day horizon, not 21.
    Returns the number of predictions newly written.
    """
    has_open = {p["movie_tmdb_id"] for p in open_predictions(bq)}
    now = datetime.now(UTC)
    rows: list[ContentPredictionRow] = []
    for f in findings:
        if f.kind != "trailer_due" or f.movie_tmdb_id in has_open:
            continue
        d2r = f.metrics.get("days_to_release")
        rows.append(
            ContentPredictionRow(
                prediction_id=f"{f.movie_tmdb_id}-{today.isoformat()}",
                kind=f.kind,
                movie_tmdb_id=f.movie_tmdb_id,
                title=f.title,
                predicted_at=today,
                horizon_days=min(horizon_days, trailer_due_horizon(d2r)),
                days_to_release_at_prediction=f.metrics.get("days_to_release"),
                vs_peak_at_prediction=f.metrics.get("vs_peak"),
                basis=f.headline,
                status="open",
                created_at=now,
                updated_at=now,
            )
        )
    if not rows:
        return 0
    bq.merge_rows(
        table="content_predictions",
        rows=rows,
        merge_keys=["prediction_id"],
        update_fields=[c for c in _FIELDS if c != "prediction_id"],
        insert_fields=_FIELDS,
    )
    return len(rows)


def resolve_predictions(
    bq: BigQueryClient, *, today: date
) -> dict[str, int]:
    """Resolve open predictions: 'hit' if a new trailer landed in window, else 'miss'.

    A hit = the movie published a trailer strictly after predicted_at and within
    horizon_days. A miss = the horizon has fully elapsed with no such trailer.
    Predictions still inside their window stay open. Returns {hits, misses}.
    """
    preds = [p for p in open_predictions(bq) if p["kind"] == "trailer_due"]
    if not preds:
        return {"hits": 0, "misses": 0}

    # Inline the integer movie ids — the BigQuery client param helper has no
    # array support, and these are our own INT64 ids (no injection surface).
    ids_sql = ", ".join(str(int(p["movie_tmdb_id"])) for p in preds)
    trailers = bq.query(
        f"""
        SELECT movie_tmdb_id, youtube_video_id, DATE(published_at) AS pub
        FROM {_table(bq, 'trailers')}
        WHERE content_kind = 'movie'
          AND movie_tmdb_id IN ({ids_sql})
          AND published_at IS NOT NULL
        ORDER BY published_at
        """
    )
    by_movie: dict[int, list[dict[str, Any]]] = {}
    for t in trailers:
        by_movie.setdefault(t["movie_tmdb_id"], []).append(t)

    now = datetime.now(UTC)
    updates: list[dict[str, Any]] = []
    hits = misses = 0
    for p in preds:
        pred_at: date = p["predicted_at"]
        deadline_passed = (today - pred_at).days > p["horizon_days"]
        # earliest trailer published after the prediction
        new_trailer = next(
            (t for t in by_movie.get(p["movie_tmdb_id"], []) if t["pub"] > pred_at),
            None,
        )
        if new_trailer:
            lag = (new_trailer["pub"] - pred_at).days
            if lag <= p["horizon_days"]:
                updates.append(
                    {
                        "prediction_id": p["prediction_id"],
                        "status": "hit",
                        "resolved_at": today.isoformat(),
                        "resolved_youtube_video_id": new_trailer["youtube_video_id"],
                        "resolved_lag_days": lag,
                        "updated_at": now.isoformat(),
                    }
                )
                hits += 1
                continue
        if deadline_passed:
            updates.append(
                {
                    "prediction_id": p["prediction_id"],
                    "status": "miss",
                    "resolved_at": today.isoformat(),
                    "resolved_youtube_video_id": None,
                    "resolved_lag_days": None,
                    "updated_at": now.isoformat(),
                }
            )
            misses += 1

    if updates:
        bq.update_from_dicts(
            table="content_predictions",
            rows=updates,
            merge_keys=["prediction_id"],
            update_clause_sql=(
                "T.status = S.status, T.resolved_at = S.resolved_at, "
                "T.resolved_youtube_video_id = S.resolved_youtube_video_id, "
                "T.resolved_lag_days = S.resolved_lag_days, T.updated_at = S.updated_at"
            ),
        )
    return {"hits": hits, "misses": misses}


# --- opening_tier: record -------------------------------------------------------------

_SIGNAL_UPCOMING = """
WITH cand AS (
  SELECT tmdb_id, title, primary_release_date AS rel
  FROM `{ds}.movies`
  WHERE primary_release_date BETWEEN @today AND DATE_ADD(@today, INTERVAL @max_d2r DAY)
),
latest AS (
  SELECT youtube_video_id,
         ARRAY_AGG(view_count ORDER BY collected_date DESC LIMIT 1)[OFFSET(0)] AS views
  FROM `{ds}.trailer_stats_daily`
  WHERE collected_date >= DATE_SUB(@today, INTERVAL 400 DAY)
  GROUP BY youtube_video_id
),
vel AS (
  SELECT youtube_video_id, MAX(delta_views) AS peak, COUNT(DISTINCT collected_date) AS n_days
  FROM `{ds}.vw_trailer_daily_metrics`
  WHERE collected_date >= DATE_SUB(@today, INTERVAL 400 DAY)
  GROUP BY youtube_video_id
)
SELECT c.tmdb_id, c.title, c.rel, DATE_DIFF(c.rel, @today, DAY) AS d2r,
       SUM(l.views) AS total_views, MAX(v.peak) AS peak_daily_views, MAX(v.n_days) AS n_days
FROM cand c
JOIN `{ds}.trailers` t ON t.movie_tmdb_id = c.tmdb_id AND t.content_kind = 'movie'
JOIN latest l USING (youtube_video_id)
LEFT JOIN vel v USING (youtube_video_id)
GROUP BY 1, 2, 3, 4
"""

_SIGNAL_COHORT = """
WITH rel AS (
  SELECT tmdb_id, primary_release_date AS rel
  FROM `{ds}.movies`
  WHERE primary_release_date BETWEEN DATE_SUB(@today, INTERVAL @cohort_days DAY)
                                 AND DATE_SUB(@today, INTERVAL 1 DAY)
),
per_trailer AS (
  SELECT t.movie_tmdb_id, s.youtube_video_id,
         ARRAY_AGG(s.view_count ORDER BY s.collected_date DESC LIMIT 1)[OFFSET(0)] AS views
  FROM `{ds}.trailer_stats_daily` s
  JOIN `{ds}.trailers` t USING (youtube_video_id)
  JOIN rel r ON r.tmdb_id = t.movie_tmdb_id
  WHERE t.content_kind = 'movie'
    AND s.collected_date >= DATE_SUB(@today, INTERVAL 600 DAY)
    AND s.collected_date <= r.rel
  GROUP BY 1, 2
)
SELECT movie_tmdb_id, SUM(views) AS total_views FROM per_trailer GROUP BY 1
"""


def cohort_signals(bq: BigQueryClient, *, today: date) -> list[float]:
    """Pre-release total trailer views of every movie released in the trailing cohort window."""
    ds = f"{bq.project}.{bq.dataset}"
    rows = bq.query(_SIGNAL_COHORT.format(ds=ds), {"today": today, "cohort_days": OPENING_COHORT_DAYS})
    return [float(r["total_views"]) for r in rows if r["total_views"]]


def record_opening_tier(bq: BigQueryClient, *, today: date) -> int:
    """Log one `opening_tier` call per movie entering its final pre-release week."""
    ds = f"{bq.project}.{bq.dataset}"
    cohort = cohort_signals(bq, today=today)
    if len(cohort) < OPENING_MIN_COHORT:
        return 0
    already = {
        int(r["movie_tmdb_id"]) for r in bq.query(
            f"SELECT DISTINCT movie_tmdb_id FROM {_table(bq, 'content_predictions')} WHERE kind = @k",
            {"k": OPENING_KIND},
        )
    }
    cands = bq.query(
        _SIGNAL_UPCOMING.format(ds=ds), {"today": today, "max_d2r": OPENING_MAX_DAYS_TO_RELEASE}
    )
    now = datetime.now(UTC)
    rows: list[ContentPredictionRow] = []
    for c in cands:
        tmdb_id = int(c["tmdb_id"])
        views = float(c["total_views"] or 0)
        if (
            tmdb_id in already or views <= 0
            or (c["peak_daily_views"] or 0) < OPENING_MIN_PEAK_VIEWS
            or (c["n_days"] or 0) < OPENING_MIN_STATS_DAYS
        ):
            continue
        tier = tier_of(views, cohort)
        pct = percentile_of(views, cohort)
        rows.append(
            ContentPredictionRow(
                prediction_id=f"{tmdb_id}-{today.isoformat()}-opening",
                kind=OPENING_KIND,
                movie_tmdb_id=tmdb_id,
                title=c["title"],
                predicted_at=today,
                horizon_days=OPENING_HORIZON_DAYS,
                days_to_release_at_prediction=c["d2r"],
                basis=(
                    f"{c['title']} opens in {c['d2r']}d; {int(views):,} pre-release trailer views "
                    f"= top {100 - pct * 100:.0f}% of the last {OPENING_COHORT_DAYS}d releases "
                    f"(n={len(cohort)}) → {tier} tier at the box office"
                ),
                status="open",
                predicted_value=tier,
                signal_value=views,
                created_at=now,
                updated_at=now,
            )
        )
    if not rows:
        return 0
    bq.merge_rows(
        table="content_predictions", rows=rows, merge_keys=["prediction_id"],
        update_fields=[c for c in _FIELDS if c != "prediction_id"], insert_fields=_FIELDS,
    )
    return len(rows)


# --- opening_tier: resolve -------------------------------------------------------------

def _outcomes(bq: BigQueryClient) -> list[dict[str, Any]]:
    """Every (movie, market, metric) outcome with the movie's release date."""
    return bq.query(
        f"""
        SELECT v.tmdb_id, v.market, v.metric, v.value, v.through_date, m.primary_release_date AS rel
        FROM {_table(bq, 'vw_movie_box_office')} v
        JOIN {_table(bq, 'movies')} m USING (tmdb_id)
        WHERE v.value > 0 AND m.primary_release_date IS NOT NULL
        """
    )


def resolve_opening_tier(bq: BigQueryClient, *, today: date) -> dict[str, int]:
    """Score open `opening_tier` calls once a market's number is mature enough.

    Markets are tried in MARKET_PRIORITY; the observed tier ranks the outcome among
    movies released within ±OPENING_COHORT_DAYS in that market. Hit ⇔ same tier.
    Calls with no outcome OPENING_EXPIRE_DAYS after release close as unscorable
    (status 'miss', observed_metric 'none') — reported separately, not in the rate.
    """
    preds = [p for p in open_predictions(bq) if p["kind"] == OPENING_KIND]
    if not preds:
        return {"hits": 0, "misses": 0, "unscorable": 0}
    ids_sql = ", ".join(str(int(p["movie_tmdb_id"])) for p in preds)
    releases = {
        int(r["tmdb_id"]): r["primary_release_date"]
        for r in bq.query(
            f"SELECT tmdb_id, primary_release_date FROM {_table(bq, 'movies')} WHERE tmdb_id IN ({ids_sql})"
        )
    }
    outcomes = _outcomes(bq)
    by_key = {(int(o["tmdb_id"]), o["market"], o["metric"]): o for o in outcomes}
    by_market: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for o in outcomes:
        by_market.setdefault((o["market"], o["metric"]), []).append(o)

    now = datetime.now(UTC)
    updates: list[dict[str, Any]] = []
    counts = {"hits": 0, "misses": 0, "unscorable": 0}
    for p in preds:
        tmdb_id = int(p["movie_tmdb_id"])
        release = releases.get(tmdb_id)
        if release is None:
            continue
        scored = None
        for market, metric, min_days in MARKET_PRIORITY:
            o = by_key.get((tmdb_id, market, metric))
            if not o or o["through_date"] < release + timedelta(days=min_days):
                continue
            cohort = [
                float(x["value"]) for x in by_market[(market, metric)]
                if int(x["tmdb_id"]) != tmdb_id
                and abs((x["rel"] - release).days) <= OPENING_COHORT_DAYS
            ]
            if len(cohort) < OPENING_MIN_COHORT:
                continue
            observed = tier_of(float(o["value"]), cohort)
            scored = (market, metric, float(o["value"]), observed)
            break
        if scored:
            market, metric, value, observed = scored
            hit = observed == p["predicted_value"]
            counts["hits" if hit else "misses"] += 1
            updates.append({
                "prediction_id": p["prediction_id"], "status": "hit" if hit else "miss",
                "resolved_at": today.isoformat(), "observed_value": value,
                "observed_metric": metric, "observed_market": market,
                "updated_at": now.isoformat(),
            })
        elif today > release + timedelta(days=OPENING_EXPIRE_DAYS):
            counts["unscorable"] += 1
            updates.append({
                "prediction_id": p["prediction_id"], "status": "miss",
                "resolved_at": today.isoformat(), "observed_value": None,
                "observed_metric": "none", "observed_market": None,
                "updated_at": now.isoformat(),
            })
    if updates:
        bq.update_from_dicts(
            table="content_predictions", rows=updates, merge_keys=["prediction_id"],
            update_clause_sql=(
                "T.status = S.status, T.resolved_at = S.resolved_at, "
                "T.observed_value = S.observed_value, T.observed_metric = S.observed_metric, "
                "T.observed_market = S.observed_market, T.updated_at = S.updated_at"
            ),
        )
    return counts


# --- scoreboard + period listing -----------------------------------------------------

def _scoreboard(r: dict[str, Any]) -> dict[str, Any]:
    hits, misses = r.get("hits", 0) or 0, r.get("misses", 0) or 0
    resolved = hits + misses
    return {
        "hits": hits,
        "misses": misses,
        "open": r.get("open", 0) or 0,
        "unscorable": r.get("unscorable", 0) or 0,
        "hit_rate": (hits / resolved) if resolved else None,
        "avg_hit_lag_days": r.get("avg_hit_lag_days"),
    }


def resolved_since(bq: BigQueryClient, *, cutoff: date) -> list[dict[str, Any]]:
    """Predictions resolved on/after `cutoff`, with the evidence, newest first."""
    return bq.query(
        f"""
        SELECT p.prediction_id, p.kind, p.title, p.movie_tmdb_id, p.predicted_at, p.horizon_days,
               p.days_to_release_at_prediction, p.status, p.resolved_at, p.resolved_lag_days,
               p.predicted_value, p.signal_value, p.observed_value, p.observed_metric,
               p.observed_market, t.name AS resolved_trailer_name, p.resolved_youtube_video_id
        FROM {_table(bq, 'content_predictions')} p
        LEFT JOIN {_table(bq, 'trailers')} t ON t.youtube_video_id = p.resolved_youtube_video_id
        WHERE p.status IN ('hit', 'miss') AND p.resolved_at >= @cutoff
        ORDER BY p.resolved_at DESC, p.kind, p.title
        """,
        {"cutoff": cutoff},
    )


def track_record(bq: BigQueryClient) -> dict[str, Any]:
    """Aggregate hit/miss/open counts, hit rate, and average lag-to-hit.

    Top-level keys are the all-kinds totals (consumers predate multiple kinds);
    `by_kind` breaks the same scoreboard out per prediction kind.
    """
    rows = bq.query(
        f"""
        SELECT
          kind,
          COUNTIF(status = 'hit')  AS hits,
          COUNTIF(status = 'miss' AND COALESCE(observed_metric, '') != 'none') AS misses,
          COUNTIF(status = 'open') AS open,
          COUNTIF(status = 'miss' AND observed_metric = 'none') AS unscorable,
          AVG(IF(status = 'hit', resolved_lag_days, NULL)) AS avg_hit_lag_days
        FROM {_table(bq, 'content_predictions')}
        GROUP BY kind
        ORDER BY kind
        """
    )
    by_kind = {r["kind"]: _scoreboard(r) for r in rows}
    total = _scoreboard(
        {
            "hits": sum(s["hits"] for s in by_kind.values()),
            "misses": sum(s["misses"] for s in by_kind.values()),
            "open": sum(s["open"] for s in by_kind.values()),
            "unscorable": sum(s["unscorable"] for s in by_kind.values()),
            "avg_hit_lag_days": next(
                (r["avg_hit_lag_days"] for r in rows if r["kind"] == "trailer_due"), None
            ),
        }
    )
    return {**total, "by_kind": by_kind}
