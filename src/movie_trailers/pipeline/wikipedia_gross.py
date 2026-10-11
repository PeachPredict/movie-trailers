"""Wikipedia phase: infobox `gross` / `budget` for every tracked film after release.

Wikidata rarely carries box-office claims for recent films, but English-Wikipedia
infoboxes do (`{{INR|143.60}} crore`, `$12.3 million`). The article is found from the
IMDb id via Wikidata's sitelinks (cached in `wikipedia_pages`), then one `parse` call
per film reads the wikitext. Rows land in `box_office_observations` as
`wikipedia / WW / gross_inr` or `revenue_usd` (currency-native, cumulative).

Candidates: tracked movies released 7..`box_office_max_age_days` days ago, re-read every
`box_office_refresh_days`, most popular first, capped by `wikipedia_max_per_run`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import structlog

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.clients.wikipedia import (
    WikidataClient,
    WikipediaClient,
    parse_infobox_money,
)
from movie_trailers.config import Settings
from movie_trailers.models import BoxOfficeObservationRow, WikipediaPageRow
from movie_trailers.pipeline.box_office import MERGE_KEYS

log = structlog.get_logger()

_BO_FIELDS = list(BoxOfficeObservationRow.model_fields.keys())
_PAGE_FIELDS = list(WikipediaPageRow.model_fields.keys())
RETRY_NO_ARTICLE_DAYS = 30


def run_wikipedia_gross(
    *,
    bq: BigQueryClient,
    settings: Settings,
    wikidata: WikidataClient | None = None,
    wikipedia: WikipediaClient | None = None,
    today: date | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """Returns (articles_read, observations_written)."""
    today = today or datetime.now(UTC).date()
    cap = settings.wikipedia_max_per_run if limit is None else min(limit, settings.wikipedia_max_per_run)
    cands = _select_candidates(bq, settings, today, cap=cap)
    if not cands:
        log.info("wikipedia.no_candidates")
        return 0, 0

    own_wd = wikidata is None
    own_wp = wikipedia is None
    wikidata = wikidata or WikidataClient(settings.contact_email)
    wikipedia = wikipedia or WikipediaClient(settings.contact_email)
    now = datetime.now(UTC)
    pages: list[WikipediaPageRow] = []
    rows: list[BoxOfficeObservationRow] = []
    read = 0
    try:
        unresolved = [c for c in cands if not c.get("enwiki_title")]
        titles = wikidata.enwiki_titles([str(c["imdb_id"]) for c in unresolved]) if unresolved else {}
        for c in unresolved:
            c["enwiki_title"] = titles.get(str(c["imdb_id"]))
            if not c["enwiki_title"]:
                pages.append(WikipediaPageRow(tmdb_id=int(c["tmdb_id"]), imdb_id=c["imdb_id"],
                                              enwiki_title=None, status="no_article", resolved_at=now))

        for c in cands:
            title = c.get("enwiki_title")
            if not title:
                continue
            tmdb_id = int(c["tmdb_id"])
            try:
                text = wikipedia.wikitext(title)
            except Exception as exc:  # noqa: BLE001
                log.warning("wikipedia.fetch_failed", title=title, error=str(exc))
                continue
            read += 1
            if text is None:
                pages.append(WikipediaPageRow(tmdb_id=tmdb_id, imdb_id=c["imdb_id"], enwiki_title=title,
                                              status="missing", resolved_at=now))
                continue
            gross = parse_infobox_money(text, "gross")
            budget = parse_infobox_money(text, "budget")
            status = "ok" if gross else "no_gross"
            pages.append(WikipediaPageRow(tmdb_id=tmdb_id, imdb_id=c["imdb_id"], enwiki_title=title,
                                          status=status, resolved_at=now))
            if gross:
                amount, currency = gross
                rows.append(BoxOfficeObservationRow(
                    tmdb_id=tmdb_id, source="wikipedia", market="WW",
                    metric="gross_inr" if currency == "INR" else "revenue_usd",
                    value=amount, cumulative=True, period_start=None, period_end=today,
                    budget_usd=int(budget[0]) if budget and budget[1] == "USD" else None,
                    raw_title=title, match_method="wikidata_sitelink", observed_at=now,
                ))
    finally:
        if own_wd:
            wikidata.close()
        if own_wp:
            wikipedia.close()

    if pages:
        bq.merge_rows(table="wikipedia_pages", rows=pages, merge_keys=["tmdb_id"],
                      update_fields=[f for f in _PAGE_FIELDS if f != "tmdb_id"], insert_fields=_PAGE_FIELDS)
    if rows:
        bq.merge_rows(table="box_office_observations", rows=rows, merge_keys=MERGE_KEYS,
                      update_fields=[c for c in _BO_FIELDS if c not in MERGE_KEYS], insert_fields=_BO_FIELDS)
    log.info("wikipedia.done", candidates=len(cands), read=read, observations=len(rows),
             no_article=sum(1 for p in pages if p.status == "no_article"))
    return read, len(rows)


def _select_candidates(bq: BigQueryClient, settings: Settings, today: date, *, cap: int) -> list[dict[str, Any]]:
    sql = f"""
    SELECT m.tmdb_id, m.imdb_id, m.title, m.popularity, w.enwiki_title, w.status, w.resolved_at
    FROM `{bq.project}.{bq.dataset}.movies` m
    LEFT JOIN `{bq.project}.{bq.dataset}.wikipedia_pages` w ON w.tmdb_id = m.tmdb_id
    LEFT JOIN (
      SELECT tmdb_id, MAX(observed_at) AS last_observed
      FROM `{bq.project}.{bq.dataset}.box_office_observations` WHERE source = 'wikipedia'
      GROUP BY tmdb_id
    ) o ON o.tmdb_id = m.tmdb_id
    WHERE m.imdb_id IS NOT NULL
      AND m.primary_release_date BETWEEN DATE_SUB(@today, INTERVAL @max_age DAY)
                                    AND DATE_SUB(@today, INTERVAL 7 DAY)
      AND EXISTS (SELECT 1 FROM `{bq.project}.{bq.dataset}.trailers` t
                  WHERE t.content_kind = 'movie' AND t.movie_tmdb_id = m.tmdb_id)
      AND (o.last_observed IS NULL
           OR o.last_observed < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @refresh DAY))
      AND (w.status IS NULL OR w.status = 'ok'
           OR (w.status = 'no_gross' AND w.resolved_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @refresh DAY))
           OR w.resolved_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @retry DAY))
    ORDER BY m.popularity DESC
    LIMIT @cap
    """
    return bq.query(sql, {
        "today": today, "max_age": settings.box_office_max_age_days,
        "refresh": settings.box_office_refresh_days, "retry": RETRY_NO_ARTICLE_DAYS, "cap": cap,
    })
