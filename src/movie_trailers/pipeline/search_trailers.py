"""YouTube-search fallback for upcoming movies TMDB lists no trailer for.

TMDB's video list is thin for Arabic (and some Indian) cinema, so titles from
`search_languages` / `search_countries` that are releasing within the checkpoint
window and still have zero trailers get a `search.list` call. That call costs 100
units (100x a `videos.list`), so spend is rationed three ways:

- only titles with no usable trailer are searched — one match stops all searching;
- each title is searched at most once per checkpoint (e.g. 45/21/10/4 days before
  release), recorded in `trailer_search_log`;
- a hard per-run cap (`search_max_units`), spent on the most popular titles first.

Search results are noisy (re-uploads, fan edits, fake "concept" trailers, reviews),
so a hit must match the title, look like a trailer, avoid junk words, not tag itself
fan-made in its description/tags, and have a trailer-like duration (all checked
with one 1-unit `videos.list` per search).
"""

from __future__ import annotations

import html
import unicodedata
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.clients.youtube import QuotaExceededError, YouTubeClient
from movie_trailers.config import Settings
from movie_trailers.models import TrailerRow, TrailerSearchLogRow, VideoType
from movie_trailers.pipeline._common import parse_iso8601_duration, parse_published_at
from movie_trailers.pipeline.discover_movies import _upsert_trailers

log = structlog.get_logger()

SEARCH_COST = 100  # units per search.list
MAX_RESULTS = 10
MIN_DURATION_S = 15
MAX_DURATION_S = 360
PUBLISHED_LOOKBACK_DAYS = 365  # teasers can drop ~a year before release

# Arabic letter variants folded together (hamza-carrying alefs, ta marbuta, alef maqsura).
_ARABIC_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})


def normalize(text: str | None) -> str:
    """Casefold, strip combining marks/punctuation, fold Arabic variants, collapse spaces."""
    s = unicodedata.normalize("NFKC", html.unescape(text or "")).casefold()
    s = s.translate(_ARABIC_FOLD)
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat == "Mn":
            continue
        out.append(ch if cat[0] in "LNM" else " ")
    return " ".join("".join(out).split())


def _norm_all(words: list[str]) -> tuple[str, ...]:
    return tuple(normalize(w) for w in words)


# Substring match: Arabic prefixes the article (الإعلان), plurals etc.
_TRAILER_WORDS = _norm_all([
    "trailer", "teaser", "glimpse", "bande annonce", "إعلان", "برومو",
    "ट्रेलर", "टीज़र", "டிரெய்லர்", "ட்ரெய்லர்", "டீசர்", "ట్రైలర్", "టీజర్",
    "ട്രെയിലർ", "ടീസർ", "ಟ್ರೈಲರ್", "ಟೀಸರ್", "ট্রেলার", "ਟ੍ਰੇਲਰ",
])
_TEASER_WORDS = _norm_all(["teaser", "glimpse", "टीज़र", "டீசர்", "టీజర్", "ടീസർ", "ಟೀಸರ್"])
# Whole-word match: signals the video is *about* the trailer, not the trailer.
_JUNK_WORDS = _norm_all([
    "reaction", "reactions", "review", "reviews", "breakdown", "explained", "explanation",
    "fan made", "fanmade", "concept", "recreation", "spoof", "parody", "behind the scenes",
    "making", "box office", "public talk", "full movie", "scene", "scenes", "song", "songs",
    "lyric", "lyrical", "jukebox", "shorts", "status", "edit", "edits", "release date",
    "climax", "hidden", "clues", "mashup", "مراجعة", "ردة فعل", "تحليل", "रिव्यू",
])


# Substring match over description + tags: fan-made "trailers" for hyped films
# rank high and look official by title, but tag/disclaim themselves.
_FAKE_MARKERS = _norm_all([
    "fanmade", "fan made", "fan trailer", "concept trailer", "trailer concept",
    "notion trailer", "section 107", "fair use", "ai generated", "unofficial",
])
# A one-word title ("العربي", "Diya") also occurs as an ordinary word, so it must
# lead the video title ("Diya | bande annonce"), not trail it ("…بالوطن العربي").
SINGLE_WORD_TITLE_MAX_POS = 6


def _has_word(haystack: str, needle: str) -> bool:
    return f" {needle} " in f" {haystack} "


def _names_movie(t: str, name: str) -> bool:
    if " " in name:
        return _has_word(t, name)
    return name in t.split()[:SINGLE_WORD_TITLE_MAX_POS]


def looks_like_trailer(movie: dict[str, Any], video_title: str) -> bool:
    """Title names the movie (either title) + a trailer word + no junk word."""
    t = normalize(video_title)
    titles = {normalize(movie.get("title")), normalize(movie.get("original_title"))} - {""}
    if not any(_names_movie(t, name) for name in titles):
        return False
    if not any(w in t for w in _TRAILER_WORDS):
        return False
    return not any(_has_word(t, w) for w in _JUNK_WORDS)


def looks_fan_made(video: dict[str, Any]) -> bool:
    """videos.list resource self-identifies as fan-made/concept/fair-use."""
    snippet = video.get("snippet") or {}
    text = normalize(" ".join([snippet.get("description") or "", *(snippet.get("tags") or [])]))
    return any(m in text for m in _FAKE_MARKERS)


def classify_title(video_title: str) -> VideoType:
    t = normalize(video_title)
    if any(w in t for w in _TEASER_WORDS):
        return "Teaser"
    if "official trailer" in t:
        return "Official Trailer"
    return "Trailer"


def pick_trailers(
    movie: dict[str, Any],
    items: list[dict[str, Any]],
    details: dict[str, dict[str, Any]],
    *,
    known: set[str],
    max_matches: int,
) -> list[dict[str, Any]]:
    """Filter search.list `items` (relevance order) down to plausible trailers.

    `details` maps videoId → videos.list resource (for duration); a result missing
    from it is unavailable and dropped. `known` holds already-tracked video IDs.
    """
    # Fake-trailer channels upload in bulk and only some uploads carry the
    # fan-made marker, so one flagged video condemns its whole channel.
    fake_channels = {
        (d.get("snippet") or {}).get("channelId") for d in details.values() if looks_fan_made(d)
    }
    picked: list[dict[str, Any]] = []
    for it in items:
        vid = (it.get("id") or {}).get("videoId")
        snippet = it.get("snippet") or {}
        title = html.unescape(snippet.get("title") or "")
        if not vid or vid in known or vid not in details:
            continue
        if snippet.get("channelId") in fake_channels:
            continue
        if not looks_like_trailer(movie, title) or looks_fan_made(details[vid]):
            continue
        duration = parse_iso8601_duration(
            (details[vid].get("contentDetails") or {}).get("duration")
        )
        if duration is None or not MIN_DURATION_S <= duration <= MAX_DURATION_S:
            continue
        picked.append(
            {
                "youtube_video_id": vid,
                "name": title,
                "published_at": snippet.get("publishedAt"),
                "channel_id": snippet.get("channelId"),
                "channel_title": html.unescape(snippet.get("channelTitle") or "") or None,
                "video_type": classify_title(title),
            }
        )
        if len(picked) >= max_matches:
            break
    return picked


def is_due(
    last_searched: date | None, release: date, today: date, checkpoints: list[int]
) -> bool:
    """Search once on entering the window, then once per checkpoint crossed since."""
    if last_searched is None:
        return True
    days_then = (release - last_searched).days
    days_now = (release - today).days
    return any(days_now <= cp < days_then for cp in checkpoints)


def build_query(movie: dict[str, Any]) -> str:
    names = [movie.get("original_title"), movie.get("title")]
    uniq = list(dict.fromkeys(n for n in names if n))
    if len(uniq) == 2 and normalize(uniq[0]) == normalize(uniq[1]):
        uniq = uniq[:1]
    return " ".join([*uniq, "trailer"])


def run_search_trailers(
    *,
    youtube: YouTubeClient,
    bq: BigQueryClient,
    settings: Settings,
    today: date | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """Search YouTube for trailerless upcoming movies. Returns (searches, trailers_added)."""
    today = today or datetime.now(UTC).date()
    checkpoints = sorted(settings.search_checkpoints_days, reverse=True)
    candidates = _select_candidates(bq, settings, today, window_days=checkpoints[0])
    due = [
        c for c in candidates
        if is_due(c["last_searched_date"], c["primary_release_date"], today, checkpoints)
    ]
    due.sort(key=lambda c: -(c["popularity"] or 0))
    max_searches = settings.search_max_units // SEARCH_COST
    if limit is not None:
        max_searches = min(max_searches, limit)
    log.info(
        "search_trailers.targets",
        trailerless=len(candidates), due=len(due), budget_searches=max_searches,
    )
    if not due or max_searches <= 0:
        return 0, 0

    known = {
        r["youtube_video_id"]
        for r in bq.query(f"SELECT youtube_video_id FROM `{bq.project}.{bq.dataset}.trailers`")
    }
    trailer_rows: list[TrailerRow] = []
    log_rows: list[TrailerSearchLogRow] = []
    for movie in due[:max_searches]:
        release: date = movie["primary_release_date"]
        query = build_query(movie)
        units_before = youtube.quota_units_used
        try:
            items = youtube.search_videos(
                query,
                published_after=(release - timedelta(days=PUBLISHED_LOOKBACK_DAYS)).isoformat()
                + "T00:00:00Z",
                relevance_language=movie["original_language"],
                region_code=_region(movie, settings),
                max_results=MAX_RESULTS,
            )
            ids = [(it.get("id") or {}).get("videoId") for it in items]
            ids = [v for v in ids if v and v not in known]
            details = {v["id"]: v for v in youtube.videos_list(ids)} if ids else {}
        except QuotaExceededError:
            log.warning("search_trailers.quota_exhausted", searched=len(log_rows))
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("search_trailers.search_failed", tmdb_id=movie["tmdb_id"], error=str(exc))
            continue

        picks = pick_trailers(
            movie, items, details, known=known, max_matches=settings.search_max_matches
        )
        now = datetime.now(UTC)
        for p in picks:
            known.add(p["youtube_video_id"])
            trailer_rows.append(
                TrailerRow(
                    youtube_video_id=p["youtube_video_id"],
                    content_kind="movie",
                    movie_tmdb_id=int(movie["tmdb_id"]),
                    video_type=p["video_type"],
                    name=p["name"],
                    published_at=parse_published_at(p["published_at"]),
                    channel_id=p["channel_id"],
                    channel_title=p["channel_title"],
                    official=None,  # unknown: found by search, not curated by TMDB
                    tracking_end_date=release,
                    first_seen_at=now,
                    last_collected_at=now,
                )
            )
        log_rows.append(
            TrailerSearchLogRow(
                movie_tmdb_id=int(movie["tmdb_id"]),
                searched_date=today,
                searched_at=now,
                query=query,
                results_returned=len(items),
                matched_video_ids=[p["youtube_video_id"] for p in picks],
                quota_units=youtube.quota_units_used - units_before,
            )
        )
        log.info(
            "search_trailers.searched",
            tmdb_id=movie["tmdb_id"], query=query, results=len(items), matched=len(picks),
        )

    _upsert_trailers(bq, trailer_rows)
    _upsert_search_log(bq, log_rows)
    log.info(
        "search_trailers.done",
        searches=len(log_rows), trailers_added=len(trailer_rows),
        movies_matched=sum(1 for r in log_rows if r.matched_video_ids),
    )
    return len(log_rows), len(trailer_rows)


def _region(movie: dict[str, Any], settings: Settings) -> str | None:
    for c in movie.get("origin_countries") or []:
        if c in settings.search_countries:
            return str(c)
    return None


def _select_candidates(
    bq: BigQueryClient, settings: Settings, today: date, *, window_days: int
) -> list[dict[str, Any]]:
    """Upcoming in-scope movies with no usable trailer, plus when last searched."""
    sql = f"""
    SELECT m.tmdb_id, m.title, m.original_title, m.original_language, m.origin_countries,
           m.primary_release_date, m.popularity, s.last_searched_date
    FROM `{bq.project}.{bq.dataset}.movies` m
    LEFT JOIN (
      SELECT movie_tmdb_id, MAX(searched_date) AS last_searched_date
      FROM `{bq.project}.{bq.dataset}.trailer_search_log`
      GROUP BY movie_tmdb_id
    ) s ON s.movie_tmdb_id = m.tmdb_id
    WHERE m.primary_release_date BETWEEN @today AND DATE_ADD(@today, INTERVAL @window DAY)
      AND (
        m.original_language IN UNNEST(@langs)
        OR EXISTS (SELECT 1 FROM UNNEST(m.origin_countries) c WHERE c IN UNNEST(@countries))
      )
      AND NOT EXISTS (
        SELECT 1 FROM `{bq.project}.{bq.dataset}.trailers` t
        WHERE t.content_kind = 'movie' AND t.movie_tmdb_id = m.tmdb_id
          AND t.tracking_status != 'unavailable'
      )
    """
    return bq.query(
        sql,
        {
            "today": today,
            "window": window_days,
            "langs": list(settings.search_languages),
            "countries": list(settings.search_countries),
        },
    )


def _upsert_search_log(bq: BigQueryClient, rows: list[TrailerSearchLogRow]) -> None:
    if not rows:
        return
    fields = [
        "movie_tmdb_id", "searched_date", "searched_at", "query",
        "results_returned", "matched_video_ids", "quota_units",
    ]
    bq.merge_rows(
        table="trailer_search_log",
        rows=rows,
        merge_keys=["movie_tmdb_id", "searched_date"],
        update_fields=[c for c in fields if c not in {"movie_tmdb_id", "searched_date"}],
        insert_fields=fields,
    )
