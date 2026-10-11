"""ANCINE (Brazil) admissions loader — `mt load-ancine`.

ANCINE's open data publishes theatrical results per title ("Relatório de
bilheteria … de obras informada pelas distribuidoras") as CSV, in two layouts:

- **weekly**: one row per title per exhibition week — `Título original da obra`,
  `Título no Brasil`, `País`, `Data Inicial da Exibição`, `Data Final da Exibição`,
  `Público`, `Renda`, …
- **daily** (`BilheteriaObrasDia.csv`): one row per screen per day — `Data da
  Exibição`, the same titles, `Público`. Rows are summed into Thursday–Wednesday
  film weeks here.

Either way the result is `ancine / BR / admissions` (+ `revenue_brl`) rows in
`box_office_observations`, **not cumulative**, one per (movie, week), keyed on the
TMDB id. Titles are matched to the `movies` table by normalized original title
(release year within reach of the week), then — for what's left — by a TMDB search
on the Brazilian title whose hit's original title must equal the file's. Unmatched
titles are reported, never guessed.

The source is a local path or a URL (the ANCINE host returned HTTP 403 from
outside Brazil during development — run this from peach_server, or download the
file by hand and pass the path).
"""

from __future__ import annotations

import csv
import io
import re
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import structlog

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.clients.tmdb import TMDBClient
from movie_trailers.models import BoxOfficeObservationRow
from movie_trailers.pipeline.box_office import MERGE_KEYS
from movie_trailers.pipeline.search_trailers import normalize

log = structlog.get_logger()

MAX_TMDB_SEARCHES = 300  # per load, for titles the direct match can't place
RELEASE_LOOKBACK_DAYS = 400  # a week can belong to a film released this long ago
RELEASE_LOOKAHEAD_DAYS = 14  # … or to one TMDB dates slightly after its BR opening
_USER_AGENT = "movie-trailers/1.0 (+https://github.com/PeachPredict/movie-trailers)"


@dataclass
class AncineRecord:
    raw_title: str  # Título original da obra
    title_br: str | None
    country: str | None
    period_start: date
    period_end: date
    admissions: float
    revenue_brl: float | None


# --- parsing -------------------------------------------------------------------------

def _key(header: str) -> str:
    return normalize(header)


_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "raw_title": ("titulo original da obra", "titulo original"),
    "title_br": ("titulo no brasil", "titulo brasil", "titulo brasileiro"),
    "country": ("pais", "pais da obra"),
    "period_start": ("data inicial da exibicao", "data inicio exibicao", "data inicial"),
    "period_end": ("data final da exibicao", "data fim exibicao", "data final"),
    "day": ("data da exibicao", "data exibicao", "data"),
    "admissions": ("publico",),
    "revenue": ("renda",),
}


def map_columns(header: list[str]) -> dict[str, int]:
    """Field → column index, by accent-insensitive header names."""
    keys = [_key(h) for h in header]
    out: dict[str, int] = {}
    for field, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in keys:
                out[field] = keys.index(alias)
                break
    missing = [f for f in ("raw_title", "admissions") if f not in out]
    if missing or not ({"period_start", "period_end"} <= out.keys() or "day" in out):
        raise ValueError(f"unrecognised ANCINE layout; header={header[:12]} missing={missing}")
    return out


def parse_number(s: str | None) -> float | None:
    """'1.234.567' → 1234567; '12.345,67' → 12345.67; '' → None."""
    if s is None:
        return None
    t = s.strip().replace("R$", "").replace(" ", "")
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        return None


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    t = s.strip()[:10]
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def film_week(d: date) -> tuple[date, date]:
    """Brazilian film weeks run Thursday → Wednesday."""
    offset = (2 - d.weekday()) % 7  # days until Wednesday
    end = d + timedelta(days=offset)
    return end - timedelta(days=6), end


def _open_text(path: Path) -> io.TextIOBase:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return io.StringIO(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"cannot decode {path}")


def parse_ancine_csv(path: Path) -> Iterator[AncineRecord]:
    """Yield one record per (title, week) — daily layouts are aggregated here."""
    text = _open_text(path)
    sample = text.read(8192)
    text.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"  # type: ignore[misc]
    reader = csv.reader(text, dialect)
    header = next(reader)
    cols = map_columns(header)

    def cell(row: list[str], field: str) -> str | None:
        i = cols.get(field)
        return row[i] if i is not None and i < len(row) else None

    if "day" in cols and not ({"period_start", "period_end"} <= cols.keys()):
        agg: dict[tuple[str, str | None, str | None, date, date], list[float]] = defaultdict(
            lambda: [0.0, 0.0, 0.0]
        )
        for row in reader:
            d = parse_date(cell(row, "day"))
            adm = parse_number(cell(row, "admissions"))
            title = (cell(row, "raw_title") or "").strip()
            if not d or adm is None or not title:
                continue
            start, end = film_week(d)
            k = (title, (cell(row, "title_br") or None), (cell(row, "country") or None), start, end)
            rev = parse_number(cell(row, "revenue"))
            agg[k][0] += adm
            if rev is not None:
                agg[k][1] += rev
                agg[k][2] = 1.0
        for (title, br, country, start, end), (adm, rev, has_rev) in agg.items():
            yield AncineRecord(title, br, country, start, end, adm, rev if has_rev else None)
        return

    for row in reader:
        title = (cell(row, "raw_title") or "").strip()
        start, end = parse_date(cell(row, "period_start")), parse_date(cell(row, "period_end"))
        adm = parse_number(cell(row, "admissions"))
        if not title or not end or adm is None:
            continue
        yield AncineRecord(
            title, (cell(row, "title_br") or None), (cell(row, "country") or None),
            start or end, end, adm, parse_number(cell(row, "revenue")),
        )


# --- matching ------------------------------------------------------------------------

class TitleIndex:
    """movies rows indexed by normalized original/English title for ANCINE matching."""

    def __init__(self, movies: Iterable[dict[str, Any]]):
        self.by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.by_id: dict[int, dict[str, Any]] = {}
        for m in movies:
            self.by_id[int(m["tmdb_id"])] = m
            for name in {normalize(m.get("original_title")), normalize(m.get("title"))} - {""}:
                self.by_title[name].append(m)

    def match(self, raw_title: str, period_end: date) -> dict[str, Any] | None:
        """The candidate whose release sits closest before (or just after) the week."""
        best, best_gap = None, None
        for m in self.by_title.get(normalize(raw_title), []):
            rel = m.get("primary_release_date")
            if not isinstance(rel, date):
                continue
            gap = (period_end - rel).days
            if -RELEASE_LOOKAHEAD_DAYS <= gap <= RELEASE_LOOKBACK_DAYS and (
                best_gap is None or abs(gap) < abs(best_gap)
            ):
                best, best_gap = m, gap
        return best


def _load_movies(bq: BigQueryClient, start: date, end: date) -> list[dict[str, Any]]:
    return bq.query(
        f"""
        SELECT tmdb_id, title, original_title, primary_release_date
        FROM `{bq.project}.{bq.dataset}.movies`
        WHERE primary_release_date BETWEEN DATE_SUB(@start, INTERVAL @back DAY)
                                      AND DATE_ADD(@end, INTERVAL @ahead DAY)
        """,
        {"start": start, "end": end, "back": RELEASE_LOOKBACK_DAYS, "ahead": RELEASE_LOOKAHEAD_DAYS},
    )


def _tmdb_search_match(
    tmdb: TMDBClient, index: TitleIndex, rec: AncineRecord
) -> dict[str, Any] | None:
    """Resolve by Brazilian title; the hit must be a tracked movie whose original
    title equals the file's — a cheap, strict check against false positives."""
    if not rec.title_br:
        return None
    want = normalize(rec.raw_title)
    try:
        results = tmdb.search_movie(rec.title_br, language="pt-BR", region="BR")
    except Exception as exc:  # noqa: BLE001
        log.warning("ancine.tmdb_search_failed", title=rec.title_br, error=str(exc))
        return None
    for r in results:
        if normalize(r.get("original_title")) == want and int(r.get("id", 0)) in index.by_id:
            return index.by_id[int(r["id"])]
    return None


# --- entrypoint ----------------------------------------------------------------------

def fetch_source(source: str, workdir: Path) -> Path:
    """A local path as-is; a URL downloaded into `workdir`."""
    if not source.lower().startswith(("http://", "https://")):
        return Path(source)
    dest = workdir / "ancine.csv"
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0), follow_redirects=True,
                      headers={"User-Agent": _USER_AGENT}) as http, http.stream("GET", source) as r:
        r.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in r.iter_bytes():
                fh.write(chunk)
    return dest


def load_ancine(
    *,
    bq: BigQueryClient,
    tmdb: TMDBClient | None,
    source: str,
    cumulative: bool = False,
    dry_run: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Parse an ANCINE box-office CSV and MERGE admissions rows for tracked movies.

    Returns a summary: records read, distinct titles, matched/unmatched titles (with
    the unmatched names), and observations written.
    """
    today = today or datetime.now(UTC).date()
    with tempfile.TemporaryDirectory(prefix="mt-ancine-") as tmp:
        path = fetch_source(source, Path(tmp))
        records = list(parse_ancine_csv(path))
    if not records:
        return {"records": 0, "titles": 0, "matched": 0, "unmatched": [], "observations": 0}

    start = min(r.period_start for r in records)
    end = max(r.period_end for r in records)
    index = TitleIndex(_load_movies(bq, start, end))

    matches: dict[str, tuple[dict[str, Any], str] | None] = {}
    searches = 0
    for rec in records:
        if rec.raw_title in matches:
            continue
        m = index.match(rec.raw_title, rec.period_end)
        if m:
            matches[rec.raw_title] = (m, "original_title")
            continue
        m = None
        if tmdb is not None and searches < MAX_TMDB_SEARCHES:
            searches += 1
            m = _tmdb_search_match(tmdb, index, rec)
        matches[rec.raw_title] = (m, "tmdb_search") if m else None

    now = datetime.now(UTC)
    rows: list[BoxOfficeObservationRow] = []
    for rec in records:
        hit = matches.get(rec.raw_title)
        if not hit:
            continue
        m, method = hit
        common = {
            "tmdb_id": int(m["tmdb_id"]), "source": "ancine", "market": "BR",
            "cumulative": cumulative, "period_start": rec.period_start,
            "period_end": rec.period_end, "raw_title": rec.raw_title,
            "match_method": method, "observed_at": now,
        }
        rows.append(BoxOfficeObservationRow(metric="admissions", value=rec.admissions, **common))
        if rec.revenue_brl is not None:
            rows.append(BoxOfficeObservationRow(metric="revenue_brl", value=rec.revenue_brl, **common))

    unmatched = sorted(t for t, hit in matches.items() if hit is None)
    if rows and not dry_run:
        fields = list(BoxOfficeObservationRow.model_fields.keys())
        bq.merge_rows(
            table="box_office_observations", rows=rows, merge_keys=MERGE_KEYS,
            update_fields=[c for c in fields if c not in MERGE_KEYS], insert_fields=fields,
        )
    summary = {
        "records": len(records), "titles": len(matches),
        "matched": sum(1 for h in matches.values() if h), "unmatched": unmatched,
        "observations": len(rows), "tmdb_searches": searches, "dry_run": dry_run,
    }
    log.info("ancine.done", **{k: v for k, v in summary.items() if k != "unmatched"},
             unmatched=len(unmatched))
    return summary
