"""Build read-only JSON snapshots for the static portfolio site (`docs/data`).

Reuses the existing read-only query layer (`mcp.queries`, `digest.queries`,
`content.findings`/`predictions`) — it only ever runs SELECTs via `bq.query`, never
`merge_rows`/`update_from_dicts`. The output feeds a no-build static site on GitHub
Pages; nothing here is served live.

Each `build_*` returns a JSON-safe structure (via `clients.bigquery._serialize`);
`generate_site_data` writes them as compact files plus a `meta.json` stamp.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from movie_trailers.clients.bigquery import BigQueryClient, _serialize
from movie_trailers.config import Settings
from movie_trailers.content.findings import detect_findings
from movie_trailers.content.predictions import track_record
from movie_trailers.digest.queries import fetch_country_stats
from movie_trailers.mcp import queries

log = structlog.get_logger()

CAROUSEL_LIMIT = 10
CAROUSEL_PERIOD_DAYS = 30
N_EXAMPLE_MOVIES = 3
_LIST_CAP = 5  # cap arrays in MCP example responses for a readable explorer
_TEXT_CAP = 200  # cap comment text length in MCP example responses

_CAROUSEL_FIELDS = (
    "youtube_video_id", "title", "poster_url", "views_gained",
    "likes_gained", "view_count", "release_date", "content_kind",
)


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #
def build_carousel(bq: BigQueryClient) -> list[dict[str, Any]]:
    """Top-N movies by 30-day view growth, trimmed to carousel fields."""
    rows = queries.trending(bq, period_days=CAROUSEL_PERIOD_DAYS, limit=CAROUSEL_LIMIT)
    out = []
    for r in rows:
        if not r.get("poster_url"):
            continue  # never render a broken poster
        out.append(_serialize({k: r.get(k) for k in _CAROUSEL_FIELDS}))
    return out


def build_excitement_decay(bq: BigQueryClient) -> dict[str, Any]:
    """Cross-movie comment-excitement decay summary + dose-response buckets."""
    return _serialize(queries.comment_excitement_decay(bq))


def _pick_example_movies(bq: BigQueryClient, model_version: str, *, want: int) -> list[int]:
    """The `want` movies with the most scored trailers (deterministic order)."""
    rows = bq.query(
        f"""
        SELECT movie_tmdb_id, ANY_VALUE(n_trailers) AS n_trailers
        FROM `{bq.project}.{bq.dataset}.vw_movie_trailer_excitement`
        WHERE model_version = @mv
        GROUP BY movie_tmdb_id
        HAVING n_trailers >= 2
        ORDER BY n_trailers DESC, movie_tmdb_id
        LIMIT @want
        """,
        {"mv": model_version, "want": want},
    )
    return [r["movie_tmdb_id"] for r in rows]


def build_excitement_examples(bq: BigQueryClient) -> dict[str, Any]:
    """Per-trailer excitement sequences for a few representative multi-trailer movies."""
    version = queries._latest_excitement_version(bq)
    if version is None:
        return {"model_version": None, "examples": []}
    movie_ids = _pick_example_movies(bq, version, want=N_EXAMPLE_MOVIES)
    if not movie_ids:
        return {"model_version": version, "examples": []}
    ids_sql = ", ".join(str(int(m)) for m in movie_ids)
    titles = {
        r["tmdb_id"]: r["title"]
        for r in bq.query(
            f"SELECT tmdb_id, title FROM `{bq.project}.{bq.dataset}.movies` "
            f"WHERE tmdb_id IN ({ids_sql})"
        )
    }
    examples = []
    for mid in movie_ids:
        trend = queries.movie_excitement_trend(bq, movie_tmdb_id=mid, model_version=version)
        examples.append(
            _serialize(
                {
                    "movie_tmdb_id": mid,
                    "title": titles.get(mid, f"movie {mid}"),
                    "n_trailers": trend.get("n_trailers"),
                    "first_last_delta": trend.get("first_last_delta"),
                    "slope_per_trailer": trend.get("slope_per_trailer"),
                    "verdict": trend.get("verdict"),
                    "trailers": [
                        {
                            "trailer_ordinal": t["trailer_ordinal"],
                            "excitement": t["excitement"],
                            "published_at": t["published_at"],
                        }
                        for t in trend.get("trailers", [])
                    ],
                }
            )
        )
    return {"model_version": version, "examples": examples}


def build_engagement_decay(bq: BigQueryClient) -> dict[str, Any]:
    """Like/view engagement decay summary across movies (supporting time-series stat)."""
    return _serialize(queries.engagement_decay(bq, days=45, unit="movie"))


VIEW_WINDOW_DAYS = 120
_VIEW_MIN_PEAK = 100_000  # only compare movies with a meaningful audience
_VIEW_MIN_DAYS = 10       # … and enough stats history to show a trend


def _view_candidates(bq: BigQueryClient) -> list[dict[str, Any]]:
    """Movies with trailer count, first-trailer date, peak views, and history length."""
    return bq.query(
        f"""
        WITH tr AS (
          SELECT t.movie_tmdb_id, COUNT(*) AS n_trailers,
                 MIN(DATE(t.published_at)) AS first_pub
          FROM `{bq.project}.{bq.dataset}.trailers` t
          WHERE t.content_kind = 'movie' AND t.movie_tmdb_id IS NOT NULL
            AND t.published_at IS NOT NULL
          GROUP BY 1
        ),
        v AS (
          SELECT t.movie_tmdb_id, MAX(m.view_count) AS peak_views,
                 COUNT(DISTINCT m.collected_date) AS days
          FROM `{bq.project}.{bq.dataset}.vw_trailer_daily_metrics` m
          JOIN `{bq.project}.{bq.dataset}.trailers` t USING (youtube_video_id)
          WHERE t.content_kind = 'movie' AND t.movie_tmdb_id IS NOT NULL
            AND m.collected_date >= DATE_SUB(CURRENT_DATE(), INTERVAL @win DAY)
          GROUP BY 1
        )
        SELECT tr.movie_tmdb_id, tr.n_trailers, tr.first_pub,
               v.peak_views, v.days, mv.title
        FROM tr JOIN v USING (movie_tmdb_id)
        JOIN `{bq.project}.{bq.dataset}.movies` mv ON mv.tmdb_id = tr.movie_tmdb_id
        WHERE v.days >= @min_days AND v.peak_views >= @min_peak
        """,
        {"win": VIEW_WINDOW_DAYS, "min_days": _VIEW_MIN_DAYS, "min_peak": _VIEW_MIN_PEAK},
    )


_MULTI_MIN_TRAILERS = 8   # "many trailers"
_FEW_MAX_TRAILERS = 4     # "fewer trailers"
_MULTI_MIN_DAYS = 40      # the many-trailer pick needs a long-enough history
_DAY_TOLERANCES = (4, 8, 15, 30)  # widen until a same-length fewer-trailer film is found


def _movie_view_series(bq: BigQueryClient, movie_id: int) -> tuple[list[dict[str, Any]], list[int]]:
    """A movie's daily new-views series indexed by day-since-first-observation,
    plus the day-indices on which a new trailer launched (within the window)."""
    rows = bq.query(
        f"""
        SELECT m.collected_date AS d, SUM(m.delta_views) AS dv
        FROM `{bq.project}.{bq.dataset}.vw_trailer_daily_metrics` m
        JOIN `{bq.project}.{bq.dataset}.trailers` t USING (youtube_video_id)
        WHERE t.movie_tmdb_id = {int(movie_id)}
          AND m.collected_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {VIEW_WINDOW_DAYS} DAY)
        GROUP BY 1 ORDER BY 1
        """
    )
    pts = [(r["d"], int(r["dv"] or 0)) for r in rows if r["dv"] is not None]
    if not pts:
        return [], []
    day0 = pts[0][0]
    dates = [d for d, _ in pts]
    series = [{"day": (d - day0).days, "dv": dv} for d, dv in pts]

    launches_raw = bq.query(
        f"""
        SELECT DISTINCT DATE(published_at) AS d
        FROM `{bq.project}.{bq.dataset}.trailers`
        WHERE movie_tmdb_id = {int(movie_id)} AND content_kind = 'movie'
          AND published_at IS NOT NULL
        ORDER BY d
        """
    )
    launches: list[int] = []
    for r in launches_raw:
        ld = r["d"]
        if ld < day0:
            continue  # launched before we started tracking — can't place it on the curve
        ahead = [d for d in dates if d >= ld]
        if not ahead:
            continue
        day = (ahead[0] - day0).days  # snap to the first observed day on/after launch
        if day not in launches:
            launches.append(day)
    return series, sorted(launches)


def _pick_view_pair(cands: list[dict[str, Any]]) -> tuple[dict | None, dict | None]:
    """A famous many-trailer film + a famous fewer-trailer film of similar day-span."""
    multi_pool = [c for c in cands if c["n_trailers"] >= _MULTI_MIN_TRAILERS and c["days"] >= _MULTI_MIN_DAYS]
    if not multi_pool:
        multi_pool = [c for c in cands if c["n_trailers"] >= 3]
    if not multi_pool:
        return None, None
    multi = max(multi_pool, key=lambda c: c["peak_views"])  # famous = most-viewed
    few_pool = [
        c for c in cands
        if c["n_trailers"] <= _FEW_MAX_TRAILERS and c["movie_tmdb_id"] != multi["movie_tmdb_id"]
    ]
    for tol in _DAY_TOLERANCES:
        matches = [c for c in few_pool if abs(c["days"] - multi["days"]) <= tol]
        if matches:
            return multi, max(matches, key=lambda c: c["peak_views"])
    return multi, (max(few_pool, key=lambda c: c["peak_views"]) if few_pool else None)


def build_view_timeseries(bq: BigQueryClient) -> dict[str, Any]:
    """Daily-views curves (x = days tracked) for a famous many-trailer film vs a
    famous fewer-trailer film of similar length, with new-trailer launches starred.
    Demonstrates that more trailers keep re-injecting views against the decay.
    """
    multi, few = _pick_view_pair(_view_candidates(bq))
    if not multi or not few:
        return {"window_days": VIEW_WINDOW_DAYS, "movies": []}

    movies = []
    for chosen in (multi, few):
        series, launches = _movie_view_series(bq, chosen["movie_tmdb_id"])
        if not series:
            continue
        movies.append(
            {
                "movie_tmdb_id": chosen["movie_tmdb_id"],
                "title": chosen["title"] or f"movie {chosen['movie_tmdb_id']}",
                "n_trailers": chosen["n_trailers"],
                "days_tracked": chosen["days"],
                "series": series,
                "launches": launches,
            }
        )
    return {"window_days": VIEW_WINDOW_DAYS, "movies": movies}


def build_coverage(bq: BigQueryClient) -> dict[str, Any]:
    """Global-coverage headline numbers from per-country trailer stats."""
    rows = fetch_country_stats(bq)
    named = [r for r in rows if r.get("country") and r["country"] != "UNKNOWN"]
    unique = bq.query(
        f"SELECT COUNT(*) AS n FROM `{bq.project}.{bq.dataset}.trailers`"
    )[0]["n"]
    top = sorted(named, key=lambda r: r["total_trailers"], reverse=True)[:8]
    return _serialize(
        {
            "unique_trailers": unique,
            "countries_covered": len(named),
            "total_attributions": sum(r["total_trailers"] for r in rows),
            "active_trailers": sum(r["active_trailers"] for r in rows),
            "with_comments": sum(r["with_comments"] for r in rows),
            "with_transcript": sum(r["with_transcript"] for r in rows),
            "top_countries": [
                {"country": r["country"], "total_trailers": r["total_trailers"]} for r in top
            ],
        }
    )


# --------------------------------------------------------------------------- #
# MCP explorer examples
# --------------------------------------------------------------------------- #
def _trim(obj: Any) -> Any:
    """Cap arrays to _LIST_CAP and truncate comment `text` for a readable explorer."""
    if isinstance(obj, list):
        return [_trim(x) for x in obj[:_LIST_CAP]]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "text" and isinstance(v, str) and len(v) > _TEXT_CAP:
                out[k] = v[:_TEXT_CAP] + "…"
            else:
                out[k] = _trim(v)
        return out
    return obj


def _representative_ids(bq: BigQueryClient) -> dict[str, Any]:
    """A movie trailer (with comments) + its movie id, to feed id-taking tools."""
    rows = bq.query(
        f"""
        SELECT c.youtube_video_id, t.movie_tmdb_id
        FROM `{bq.project}.{bq.dataset}.trailer_comments_snapshots` c
        JOIN `{bq.project}.{bq.dataset}.trailers` t USING (youtube_video_id)
        WHERE c.snapshot_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 1000 DAY)
          AND c.snapshot_kind = 'at_discovery'
          AND t.content_kind = 'movie' AND t.movie_tmdb_id IS NOT NULL
        GROUP BY 1, 2
        ORDER BY COUNT(*) DESC
        LIMIT 1
        """
    )
    if rows:
        return {"youtube_video_id": rows[0]["youtube_video_id"], "movie_tmdb_id": rows[0]["movie_tmdb_id"]}
    return {"youtube_video_id": None, "movie_tmdb_id": None}


def _mcp_tool_specs(bq: BigQueryClient, ids: dict[str, Any]) -> list[dict[str, Any]]:
    """Name, signature, description, args, and a thunk per MCP tool (read-only)."""
    vid, mid = ids["youtube_video_id"], ids["movie_tmdb_id"]
    return [
        {
            "name": "search_trailers",
            "signature": "search_trailers(content_kind=None, country=None, tracking_status='active', query_text=None, limit=25)",
            "description": "Find tracked trailers by kind, origin country, status, and title text.",
            "args": {"limit": 5},
            "call": lambda: queries.search_trailers(bq, limit=5),
        },
        {
            "name": "trending",
            "signature": "trending(period_days=7, content_kind=None, country=None, limit=20)",
            "description": "Trailers gaining the most views over a recent window.",
            "args": {"period_days": 7, "limit": 5},
            "call": lambda: queries.trending(bq, period_days=7, limit=5),
        },
        {
            "name": "trailer_metrics",
            "signature": "trailer_metrics(youtube_video_id, days=30)",
            "description": "Daily view/like growth and engagement ratios for one trailer.",
            "args": {"youtube_video_id": vid, "days": 30},
            "call": lambda: queries.trailer_metrics(bq, youtube_video_id=vid, days=30),
        },
        {
            "name": "top_comments",
            "signature": "top_comments(youtube_video_id, snapshot_kind=None, limit=30)",
            "description": "Top captured YouTube comments for a trailer, by relevance rank.",
            "args": {"youtube_video_id": vid, "limit": 5},
            "call": lambda: queries.top_comments(bq, youtube_video_id=vid, limit=5),
        },
        {
            "name": "movie_engagement_trend",
            "signature": "movie_engagement_trend(movie_tmdb_id, days=30)",
            "description": "Score a movie on two independent axes (quality + demand) over time.",
            "args": {"movie_tmdb_id": mid, "days": 30},
            "call": lambda: queries.movie_engagement_trend(bq, movie_tmdb_id=mid, days=30),
        },
        {
            "name": "interesting_findings",
            "signature": "interesting_findings(days=30, top=6)",
            "description": "Surface the most newsworthy movie states, ready to turn into posts.",
            "args": {"days": 30, "top": 5},
            "call": lambda: [f.model_dump() for f in detect_findings(bq, days=30, top=5)],
        },
        {
            "name": "engagement_decay",
            "signature": "engagement_decay(days=45, unit='movie')",
            "description": "How like/view engagement trends over time, per movie or per trailer.",
            "args": {"days": 45, "unit": "movie"},
            "call": lambda: queries.engagement_decay(bq, days=45, unit="movie"),
        },
        {
            "name": "comment_excitement_decay",
            "signature": "comment_excitement_decay(model_version=None)",
            "description": "Does comment excitement decay across a movie's sequential trailers?",
            "args": {},
            "call": lambda: queries.comment_excitement_decay(bq),
        },
        {
            "name": "movie_excitement_trend",
            "signature": "movie_excitement_trend(movie_tmdb_id, model_version=None)",
            "description": "One movie's comment excitement across its trailer sequence.",
            "args": {"movie_tmdb_id": mid},
            "call": lambda: queries.movie_excitement_trend(bq, movie_tmdb_id=mid),
        },
        {
            "name": "prediction_track_record",
            "signature": "prediction_track_record()",
            "description": "The trailer-prediction scoreboard: hits, misses, hit rate, avg lag.",
            "args": {},
            "call": lambda: track_record(bq),
        },
    ]


def build_mcp_examples(bq: BigQueryClient) -> dict[str, Any]:
    """Call each MCP tool once with representative args → real example responses."""
    ids = _representative_ids(bq)
    tools = []
    for spec in _mcp_tool_specs(bq, ids):
        entry = {
            "name": spec["name"],
            "signature": spec["signature"],
            "description": spec["description"],
            "example_args": spec["args"],
        }
        try:
            entry["example_response"] = _trim(_serialize(spec["call"]()))
        except Exception as exc:  # noqa: BLE001 — one bad tool shouldn't drop the rest
            log.warning("site.mcp_example_failed", tool=spec["name"], error=str(exc)[:160])
            entry["example_response"] = None
        tools.append(entry)
    return {"tools": tools}


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def generate_site_data(
    bq: BigQueryClient, settings: Settings, out_dir: str | Path = "docs/data"
) -> dict[str, str]:
    """Build every snapshot and write it to `out_dir`. Returns name→path written.

    Each section is guarded: a failure (e.g. a dataset without excitement scores)
    logs and writes an empty-but-valid shape so the static JS always has a file.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sections: list[tuple[str, Any, Any]] = [
        ("carousel.json", build_carousel, []),
        ("excitement_decay.json", build_excitement_decay, {}),
        ("excitement_examples.json", build_excitement_examples, {"examples": []}),
        ("views_timeseries.json", build_view_timeseries, {"movies": []}),
        ("engagement_decay.json", build_engagement_decay, {}),
        ("coverage.json", build_coverage, {}),
        ("mcp_examples.json", build_mcp_examples, {"tools": []}),
    ]
    written: dict[str, str] = {}
    model_version = None
    for name, fn, empty in sections:
        try:
            payload = fn(bq)
            if name == "excitement_examples.json":
                model_version = payload.get("model_version")
        except Exception as exc:  # noqa: BLE001 — degrade gracefully, never abort the run
            log.warning("site.section_failed", section=name, error=str(exc)[:200])
            payload = empty
        _write_json(out / name, payload)
        written[name] = str(out / name)

    meta = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model_version": model_version,
        "period_days": CAROUSEL_PERIOD_DAYS,
        "dataset": settings.bq_dataset,
    }
    _write_json(out / "meta.json", meta)
    written["meta.json"] = str(out / "meta.json")
    return written
