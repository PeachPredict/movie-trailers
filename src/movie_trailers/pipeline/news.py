"""News phase: daily article volume + tone per tracked movie from GDELT's GKG.

The GDELT Global Knowledge Graph is a BigQuery public dataset
(`gdelt-bq.gdeltv2.gkg_partitioned`, ~300k records/day, day-partitioned). Each record
lists the proper names it mentions (`AllNames`, "Name,offset;…") and the people
(`V2Persons`). Each run builds the target list — every tracked movie's normalized
title(s) plus its top cast / directors — and passes it as **query parameters** to one
query per day that **equi-joins** the exploded GKG names against it (~150 MB scanned,
well under a cent). The query runs in BigQuery's US multi-region, where GDELT lives:
it must not reference our (us-central1) dataset, so results come back to Python and
are merged from here.

Per (movie, day): `articles` (title named), `confirmed_articles` (title named AND a cast /
director named in the same record), `sources`, `avg_tone`, `top_sources`. Short titles
("Hope", "The Train") collide with ordinary news, so `articles_used` is the confirmed count
for them and the plain count otherwise. This is a pre-release *signal* (see
`findings.media_surge`), not an outcome.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.config import Settings
from movie_trailers.models import MovieNewsDailyRow
from movie_trailers.pipeline.search_trailers import TOP_CAST, is_short_title

log = structlog.get_logger()

WINDOW_BEFORE_RELEASE_DAYS = 150
WINDOW_AFTER_RELEASE_DAYS = 30
MAX_DAYS_BACK = 60
_FIELDS = list(MovieNewsDailyRow.model_fields.keys())

def gkg_norm(name: str | None) -> str:
    """Mirror of the SQL normalisation in _DAY_SQL: NFKC + casefold, keep letters /
    digits / combining marks (Indic scripts need them), everything else → space."""
    s = unicodedata.normalize("NFKC", name or "").casefold()
    out = [ch if unicodedata.category(ch)[0] in "LNM" else " " for ch in s]
    return " ".join("".join(out).split())


GDELT_LOCATION = "US"

_DAY_SQL = """
WITH targets AS (
  SELECT SAFE_CAST(id AS INT64) AS tmdb_id, name, 'title' AS kind
  FROM UNNEST(@title_ids) AS id WITH OFFSET i
  JOIN UNNEST(@title_names) AS name WITH OFFSET j ON i = j
  UNION ALL
  SELECT SAFE_CAST(id AS INT64), name, 'person'
  FROM UNNEST(@person_ids) AS id WITH OFFSET i
  JOIN UNNEST(@person_names) AS name WITH OFFSET j ON i = j
),
g AS (
  SELECT GKGRECORDID, SourceCommonName, AllNames, V2Persons,
         SAFE_CAST(SPLIT(V2Tone, ',')[SAFE_OFFSET(0)] AS FLOAT64) AS tone
  FROM `{gdelt}`
  WHERE _PARTITIONTIME = TIMESTAMP(@day) AND AllNames IS NOT NULL
),
names AS (
  SELECT g.GKGRECORDID,
         TRIM(REGEXP_REPLACE(LOWER(NORMALIZE(SPLIT(nm, ',')[SAFE_OFFSET(0)], NFKC)), r'[^\\p{{L}}\\p{{N}}\\p{{M}}]+', ' ')) AS name
  FROM g, UNNEST(SPLIT(g.AllNames, ';')) AS nm
  WHERE nm != ''
),
persons AS (
  SELECT g.GKGRECORDID,
         TRIM(REGEXP_REPLACE(LOWER(NORMALIZE(SPLIT(p, ',')[SAFE_OFFSET(0)], NFKC)), r'[^\\p{{L}}\\p{{N}}\\p{{M}}]+', ' ')) AS name
  FROM g, UNNEST(SPLIT(IFNULL(g.V2Persons, ''), ';')) AS p
  WHERE p != ''
),
hits AS (
  SELECT DISTINCT t.tmdb_id, n.GKGRECORDID
  FROM names n JOIN targets t ON t.kind = 'title' AND t.name = n.name
),
confirmed AS (
  SELECT DISTINCT h.tmdb_id, h.GKGRECORDID
  FROM hits h
  JOIN persons p USING (GKGRECORDID)
  JOIN targets t ON t.kind = 'person' AND t.tmdb_id = h.tmdb_id AND t.name = p.name
)
SELECT h.tmdb_id,
       COUNT(DISTINCT h.GKGRECORDID) AS articles,
       COUNT(DISTINCT c.GKGRECORDID) AS confirmed_articles,
       COUNT(DISTINCT g.SourceCommonName) AS sources,
       AVG(g.tone) AS avg_tone,
       ARRAY_AGG(DISTINCT g.SourceCommonName IGNORE NULLS LIMIT 5) AS top_sources
FROM hits h
JOIN g USING (GKGRECORDID)
LEFT JOIN confirmed c ON c.tmdb_id = h.tmdb_id AND c.GKGRECORDID = h.GKGRECORDID
GROUP BY h.tmdb_id
"""


def run_news(
    *,
    bq: BigQueryClient,
    settings: Settings,
    today: date | None = None,
    days_back: int | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """Scan the last `days_back` GKG partitions (default settings.news_days_back, max 60).

    Returns (days_scanned, rows_written).
    """
    today = today or datetime.now(UTC).date()
    n_days = min(days_back if days_back is not None else settings.news_days_back, MAX_DAYS_BACK)
    targets, short = build_targets(bq, today=today, limit=limit)
    if not targets:
        log.info("news.no_targets")
        return 0, 0
    params = {
        "title_ids": [str(t.tmdb_id) for t in targets if t.kind == "title"],
        "title_names": [t.name for t in targets if t.kind == "title"],
        "person_ids": [str(t.tmdb_id) for t in targets if t.kind == "person"],
        "person_names": [t.name for t in targets if t.kind == "person"],
    }

    now = datetime.now(UTC)
    rows: list[MovieNewsDailyRow] = []
    days = [today - timedelta(days=i) for i in range(1, n_days + 1)]
    for day in days:
        hits = bq.query(
            _DAY_SQL.format(gdelt=settings.gdelt_table), {**params, "day": day},
            location=GDELT_LOCATION,
        )
        for h in hits:
            tmdb_id = int(h["tmdb_id"])
            articles, confirmed = int(h["articles"] or 0), int(h["confirmed_articles"] or 0)
            rows.append(MovieNewsDailyRow(
                tmdb_id=tmdb_id, date=day, articles=articles, confirmed_articles=confirmed,
                articles_used=confirmed if short.get(tmdb_id) else articles,
                sources=int(h["sources"] or 0), avg_tone=h["avg_tone"],
                top_sources=list(h["top_sources"] or []), created_at=now,
            ))
        log.info("news.day", day=day.isoformat(), movies=len(hits))
    if rows:
        bq.merge_rows(table="movie_news_daily", rows=rows, merge_keys=["tmdb_id", "date"],
                      update_fields=[f for f in _FIELDS if f not in ("tmdb_id", "date")],
                      insert_fields=_FIELDS,
                      partition_filter=f"T.date BETWEEN DATE('{days[-1]}') AND DATE('{days[0]}')")
    log.info("news.done", days=len(days), rows=len(rows), targets=len(targets))
    return len(days), len(rows)


@dataclass(frozen=True)
class NewsTarget:
    tmdb_id: int
    name: str  # normalized (gkg_norm)
    kind: str  # 'title' | 'person'


def build_targets(
    bq: BigQueryClient, *, today: date, limit: int | None = None
) -> tuple[list[NewsTarget], dict[int, bool]]:
    """(targets, {tmdb_id: short_title}) for movies in the tracking window."""
    movies = bq.query(
        f"""
        SELECT m.tmdb_id, m.title, m.original_title
        FROM `{bq.project}.{bq.dataset}.movies` m
        WHERE m.primary_release_date BETWEEN DATE_SUB(@today, INTERVAL @after DAY)
                                        AND DATE_ADD(@today, INTERVAL @before DAY)
          AND EXISTS (SELECT 1 FROM `{bq.project}.{bq.dataset}.trailers` t
                      WHERE t.content_kind = 'movie' AND t.movie_tmdb_id = m.tmdb_id)
        ORDER BY m.popularity DESC
        LIMIT @cap
        """,
        {"today": today, "after": WINDOW_AFTER_RELEASE_DAYS, "before": WINDOW_BEFORE_RELEASE_DAYS,
         "cap": limit or 100000},
    )
    if not movies:
        return [], {}
    ids = ", ".join(str(int(m["tmdb_id"])) for m in movies)
    people = bq.query(
        f"""
        SELECT tmdb_id, name FROM `{bq.project}.{bq.dataset}.credits`
        WHERE content_kind = 'movie' AND tmdb_id IN ({ids}) AND name IS NOT NULL
          AND ((credit_kind = 'cast' AND order_index < {TOP_CAST})
               OR (credit_kind = 'crew' AND job LIKE '%Director%'))
        """
    )
    return targets_from(movies, people)


def targets_from(
    movies: list[dict[str, Any]], people: list[dict[str, Any]]
) -> tuple[list[NewsTarget], dict[int, bool]]:
    rows: list[NewsTarget] = []
    short: dict[int, bool] = {}
    seen: set[tuple[int, str, str]] = set()
    for m in movies:
        tmdb_id = int(m["tmdb_id"])
        titles = {gkg_norm(m.get("title")), gkg_norm(m.get("original_title"))} - {""}
        titles = {t for t in titles if len(t) >= 2}
        if not titles:
            continue
        # "Short-titled" follows the English title (what English-language news uses);
        # a non-Latin original title rarely collides. Fall back to any name if no title.
        primary = gkg_norm(m.get("title"))
        short[tmdb_id] = is_short_title(primary) if primary else any(is_short_title(t) for t in titles)
        for t in titles:
            if (tmdb_id, "title", t) not in seen:
                seen.add((tmdb_id, "title", t))
                rows.append(NewsTarget(tmdb_id=tmdb_id, name=t, kind="title"))
    for p in people:
        tmdb_id = int(p["tmdb_id"])
        if tmdb_id not in short:
            continue
        name = gkg_norm(p.get("name"))
        if name and (tmdb_id, "person", name) not in seen:
            seen.add((tmdb_id, "person", name))
            rows.append(NewsTarget(tmdb_id=tmdb_id, name=name, kind="person"))
    return rows, short
