"""Sacnilk phase: Indian box office from the public per-movie pages, scraped politely.

Sacnilk (sacnilk.com) is the reference for Indian collections and has no public API
(its data is sold as "Custom Reports & APIs"); `robots.txt` allows `/movie/`. So this
phase keeps volume minimal and identifies itself:

- one index request per run to learn `/movie/<slug>` links, then at most
  `sacnilk_max_pages_per_run` movie pages, `sacnilk_min_interval_s` apart, with a
  User-Agent carrying the repo URL and a contact address;
- `robots.txt` is re-checked each run; a 403 / 429 / 503 (Cloudflare challenge) stops the
  run with `sacnilk.blocked` — then run the phase from peach_server instead;
- every page is **verified** before it is trusted: its theatrical release date within ±10
  days of ours and a top-cast / director name from `credits` in its Cast & Crew block
  (same idea as the YouTube-search filter). Verification failures are cached in
  `sacnilk_pages` so a title is retried weekly, not daily.

Rows (`source='sacnilk'`): cumulative `IN/india_net_inr`, `IN/india_gross_inr`,
`IN/overseas_gross_inr`, `WW/worldwide_gross_inr` (period_end = today) and one
non-cumulative `IN/net_inr_daily` row per day of the run.
"""

from __future__ import annotations

import html as html_lib
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx
import structlog

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.config import Settings
from movie_trailers.models import BoxOfficeObservationRow, SacnilkPageRow
from movie_trailers.pipeline.box_office import MERGE_KEYS
from movie_trailers.pipeline.search_trailers import credit_confirms, load_credits, normalize

log = structlog.get_logger()

INDIC_LANGUAGES = ("hi", "ta", "te", "ml", "kn", "mr", "bn", "pa", "gu", "or", "as")
INDEX_PATHS = ("/latest-boxoffice-collection", "/entertainmenttopbar/Recent_Movies")
RELEASE_TOLERANCE_DAYS = 10
DAILY_REFRESH_UNTIL_DAYS = 30  # after this many days past release, refresh weekly
RETRY_FAILED_DAYS = 7
_BO_FIELDS = list(BoxOfficeObservationRow.model_fields.keys())
_PAGE_FIELDS = list(SacnilkPageRow.model_fields.keys())
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class SacnilkBlocked(Exception):
    """403/429/503 or robots.txt disallow — stop the run, don't hammer the site."""


@dataclass
class SacnilkFigures:
    india_net_inr: float | None = None
    india_gross_inr: float | None = None
    overseas_inr: float | None = None
    worldwide_inr: float | None = None
    release_date: date | None = None
    people: list[str] = field(default_factory=list)
    daily_net_inr: list[tuple[date, float]] = field(default_factory=list)


# --- parsing (pure) ------------------------------------------------------------------

def money_inr(num: str, unit: str | None) -> float:
    """'12.5', 'Cr' → 125_000_000; 'Lakh' → ×1e5; 'K' → ×1e3."""
    n = float(num.replace(",", ""))
    u = (unit or "").strip().lower()
    if u in ("cr", "crore", "crores"):
        return n * 1e7
    if u in ("l", "lakh", "lakhs", "lac"):
        return n * 1e5
    if u in ("k", "thousand"):
        return n * 1e3
    return n


_MONEY = r"₹\s*([\d,]+(?:\.\d+)?)\s*(Cr|Crore|Crores|Lakh|Lakhs|L|K)?"


def _flatten(html: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>|<nav.*?</nav>|<header.*?</header>", " ", html, flags=re.S | re.I)
    t = html_lib.unescape(re.sub(r"<[^>]+>", " | ", t))
    return re.sub(r"(\s*\|\s*)+", " | ", t)


def _label_money(text: str, label: str) -> float | None:
    """First '₹X Cr' after `label` (within a few tokens) in the flattened text."""
    m = re.search(re.escape(label) + r"\s*:?\s*\|?\s*(?:\|\s*)?" + _MONEY, text)
    return money_inr(m.group(1), m.group(2)) if m else None


def parse_day(s: str, *, year_hint: int | None) -> date | None:
    m = re.match(r"\s*(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?(?:\s+(\d{4}))?", s)
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    year = int(m.group(3)) if m.group(3) else year_hint
    if year is None:
        return None
    try:
        return date(year, _MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None


def parse_sacnilk_page(html: str, *, year_hint: int | None = None) -> SacnilkFigures:
    """Figures from a sacnilk.com/movie/<slug> page. Unknown/absent values stay None."""
    text = _flatten(html)
    f = SacnilkFigures(
        india_net_inr=_label_money(text, "India Net"),
        india_gross_inr=_label_money(text, "India Gross") or _label_money(text, "Total Gross"),
        overseas_inr=_label_money(text, "Overseas"),
        worldwide_inr=_label_money(text, "Worldwide"),
    )
    m = re.search(r"Theatrical Release\s*:?\s*\|?\s*(\d{1,2}\s+[A-Za-z]{3}[a-z]*\.?\s+\d{4})", text)
    if m:
        f.release_date = parse_day(m.group(1), year_hint=year_hint)
    # Cast & Crew: names are /tag/<Name_With_Underscores> links inside that section.
    i = html.find("Cast &amp; Crew")
    i = i if i >= 0 else html.find("Cast & Crew")
    if i >= 0:
        block = html[i : i + 20000]
        f.people = list(dict.fromkeys(
            html_lib.unescape(n).replace("_", " ")
            for n in re.findall(r'href="(?:https?://www\.sacnilk\.com)?/tag/([^"/?#]+)"', block)
        ))
    # Day-wise net: <span class="dw-d">Day 1</span><span class="dw-dt">9 Oct 2026</span><span class="dw-net">₹0.01 Cr</span>
    year = (f.release_date.year if f.release_date else year_hint)
    last: date | None = None
    for _d, when, num, unit in re.findall(
        r'class="dw-d">\s*Day\s+(\d+)\s*<.*?class="dw-dt">\s*([^<]+?)\s*<.*?class="dw-net">\s*' + _MONEY,
        html, flags=re.S,
    ):
        day = parse_day(when, year_hint=(last.year if last else year))
        if day and last and day < last:  # "10 Oct" after "31 Dec": year rolled over
            day = day.replace(year=day.year + 1)
        if day:
            f.daily_net_inr.append((day, money_inr(num, unit)))
            last = day
    return f


def slug_guess(title: str, year: int) -> str:
    """'Drishyam 3: The Conclusion', 2026 → 'Drishyam_3_The_Conclusion_2026'."""
    words = re.findall(r"[A-Za-z0-9]+", html_lib.unescape(title))
    return "_".join(words) + f"_{year}"


def discover_slugs(index_html: str) -> dict[str, str]:
    """normalized title → slug, from `/movie/<slug>` links on an index page
    (link text like 'Comrade Kalyan (Tollywood)')."""
    out: dict[str, str] = {}
    for slug, inner in re.findall(r'<a[^>]*href="(?:https?://www\.sacnilk\.com)?/movie/([^"?#]+)"[^>]*>(.*?)</a>',
                                  index_html, flags=re.S):
        text = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", inner))).strip()
        text = re.sub(r"\s*\([^)]*\)\s*$", "", text)  # drop '(Tollywood)'
        key = normalize(text)
        if key and key != "details" and key not in out:
            out[key] = slug
    return out


def verify(movie: dict[str, Any], figures: SacnilkFigures, credits: list[str] | None) -> str | None:
    """None if the page is this movie; else the reason it isn't."""
    rel = movie.get("primary_release_date")
    if (
        figures.release_date and isinstance(rel, date)
        and abs((figures.release_date - rel).days) > RELEASE_TOLERANCE_DAYS
    ):
        return f"release_mismatch:{figures.release_date}"
    if credits:
        confirmed = bool(figures.people) and credit_confirms(credits, " ".join(figures.people))
        return None if confirmed else "no_credit_match"
    if figures.release_date is None:
        return "unverifiable"
    return None


# --- HTTP ------------------------------------------------------------------------------

class SacnilkClient:
    def __init__(self, settings: Settings, http_client: httpx.Client | None = None):
        self.base = settings.sacnilk_base_url.rstrip("/")
        self.ua = f"movie-trailers/1.0 (+https://github.com/PeachPredict/movie-trailers; {settings.contact_email})"
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True,
            headers={"User-Agent": self.ua, "Accept": "text/html"},
        )
        self._interval = settings.sacnilk_min_interval_s
        self._last = 0.0
        self.requests = 0

    def close(self) -> None:
        self._http.close()

    def allowed(self) -> bool:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = self._http.get(f"{self.base}/robots.txt")
            if r.status_code >= 400:
                return True  # no robots → nothing forbids it
            rp.parse(r.text.splitlines())
        except httpx.HTTPError:
            return True
        return rp.can_fetch(self.ua, f"{self.base}/movie/x")

    def get(self, path: str) -> str | None:
        """Page HTML, None on 404; SacnilkBlocked on 403/429/503."""
        wait = self._interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        r = self._http.get(f"{self.base}{path}")
        self._last = time.monotonic()
        self.requests += 1
        if r.status_code == 404:
            return None
        if r.status_code in (403, 429, 503):
            raise SacnilkBlocked(f"HTTP {r.status_code} on {path}")
        r.raise_for_status()
        return r.text


# --- phase -----------------------------------------------------------------------------

def run_sacnilk(
    *,
    bq: BigQueryClient,
    settings: Settings,
    client: SacnilkClient | None = None,
    today: date | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """Returns (pages_fetched, observations_written)."""
    today = today or datetime.now(UTC).date()
    cap = settings.sacnilk_max_pages_per_run if limit is None else min(limit, settings.sacnilk_max_pages_per_run)
    cands = _select_candidates(bq, settings, today, cap=cap)
    if not cands:
        log.info("sacnilk.no_candidates")
        return 0, 0
    own = client is None
    client = client or SacnilkClient(settings)
    now = datetime.now(UTC)
    pages: list[SacnilkPageRow] = []
    rows: list[BoxOfficeObservationRow] = []
    fetched = 0
    try:
        if not client.allowed():
            raise SacnilkBlocked("robots.txt disallows /movie/")
        index: dict[str, str] = {}
        for path in INDEX_PATHS:
            try:
                page = client.get(path)
            except SacnilkBlocked:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("sacnilk.index_failed", path=path, error=str(exc))
                continue
            if page:
                index.update(discover_slugs(page))
        credits = load_credits(bq, [int(c["tmdb_id"]) for c in cands])

        for c in cands:
            tmdb_id = int(c["tmdb_id"])
            rel: date = c["primary_release_date"]
            slug = c.get("slug") or _find_slug(c, index) or slug_guess(c["title"] or c["original_title"] or "", rel.year)
            page = client.get(f"/movie/{slug}")
            fetched += 1
            if page is None:
                pages.append(SacnilkPageRow(tmdb_id=tmdb_id, slug=slug, status="not_found",
                                            last_fetched_at=now, note=None))
                continue
            fig = parse_sacnilk_page(page, year_hint=rel.year)
            reason = verify(c, fig, credits.get(tmdb_id))
            if reason:
                pages.append(SacnilkPageRow(tmdb_id=tmdb_id, slug=slug, status="unmatched",
                                            last_fetched_at=now, note=reason))
                log.info("sacnilk.unmatched", tmdb_id=tmdb_id, slug=slug, reason=reason)
                continue
            pages.append(SacnilkPageRow(tmdb_id=tmdb_id, slug=slug, status="matched",
                                        last_fetched_at=now, last_changed_at=now, note=None))
            rows.extend(_rows_for(tmdb_id, fig, today=today, now=now, raw_title=slug))
    except SacnilkBlocked as exc:
        log.warning("sacnilk.blocked", error=str(exc), fetched=fetched,
                    hint="run this phase from peach_server (residential IP) if a GCP IP is challenged")
    finally:
        if own:
            client.close()

    if pages:
        bq.merge_rows(table="sacnilk_pages", rows=pages, merge_keys=["tmdb_id"],
                      update_fields=[f for f in _PAGE_FIELDS if f != "tmdb_id"], insert_fields=_PAGE_FIELDS)
    if rows:
        bq.merge_rows(table="box_office_observations", rows=rows, merge_keys=MERGE_KEYS,
                      update_fields=[c for c in _BO_FIELDS if c not in MERGE_KEYS], insert_fields=_BO_FIELDS)
    log.info("sacnilk.done", candidates=len(cands), fetched=fetched, observations=len(rows),
             matched=sum(1 for p in pages if p.status == "matched"))
    return fetched, len(rows)


def _find_slug(movie: dict[str, Any], index: dict[str, str]) -> str | None:
    for name in (movie.get("title"), movie.get("original_title")):
        key = normalize(name)
        if key and key in index:
            return index[key]
    return None


def _rows_for(tmdb_id: int, fig: SacnilkFigures, *, today: date, now: datetime, raw_title: str) -> list[BoxOfficeObservationRow]:
    def row(market: str, metric: str, value: float, *, cumulative: bool, start: date | None, end: date):
        return BoxOfficeObservationRow(
            tmdb_id=tmdb_id, source="sacnilk", market=market, metric=metric, value=value,
            cumulative=cumulative, period_start=start, period_end=end, raw_title=raw_title,
            match_method="sacnilk_page", observed_at=now,
        )
    out: list[BoxOfficeObservationRow] = []
    for metric, market, v in (
        ("india_net_inr", "IN", fig.india_net_inr), ("india_gross_inr", "IN", fig.india_gross_inr),
        ("overseas_gross_inr", "IN", fig.overseas_inr), ("worldwide_gross_inr", "WW", fig.worldwide_inr),
    ):
        if v is not None and v > 0:
            out.append(row(market, metric, v, cumulative=True, start=None, end=today))
    for day, v in fig.daily_net_inr:
        out.append(row("IN", "net_inr_daily", v, cumulative=False, start=day, end=day))
    return out


def _select_candidates(bq: BigQueryClient, settings: Settings, today: date, *, cap: int) -> list[dict[str, Any]]:
    """Indian releases 1..max_age days ago with a trailer, due for a (re)fetch."""
    sql = f"""
    SELECT m.tmdb_id, m.title, m.original_title, m.primary_release_date, m.popularity,
           p.slug, p.status, p.last_fetched_at
    FROM `{bq.project}.{bq.dataset}.movies` m
    LEFT JOIN `{bq.project}.{bq.dataset}.sacnilk_pages` p ON p.tmdb_id = m.tmdb_id
    WHERE (m.original_language IN UNNEST(@langs) OR 'IN' IN UNNEST(m.origin_countries))
      AND m.primary_release_date BETWEEN DATE_SUB(@today, INTERVAL @max_age DAY)
                                    AND DATE_SUB(@today, INTERVAL 1 DAY)
      AND EXISTS (SELECT 1 FROM `{bq.project}.{bq.dataset}.trailers` t
                  WHERE t.content_kind = 'movie' AND t.movie_tmdb_id = m.tmdb_id)
      AND (
        p.tmdb_id IS NULL
        OR (p.status = 'matched' AND p.last_fetched_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL
              IF(DATE_DIFF(@today, m.primary_release_date, DAY) <= @daily_until, 1, 7) DAY))
        OR (p.status != 'matched' AND p.last_fetched_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @retry DAY))
      )
    ORDER BY m.popularity DESC
    LIMIT @cap
    """
    return bq.query(sql, {
        "langs": list(INDIC_LANGUAGES), "today": today, "max_age": settings.sacnilk_max_age_days,
        "daily_until": DAILY_REFRESH_UNTIL_DAYS, "retry": RETRY_FAILED_DAYS, "cap": cap,
    })

