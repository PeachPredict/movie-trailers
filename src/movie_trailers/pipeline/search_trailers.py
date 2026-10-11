"""YouTube-search fallback for upcoming movies TMDB lists no trailer for.

TMDB's video list is thin for Arabic (and some Indian) cinema, so titles from
`search_languages` / `search_countries` — plus, since 2026-10, any title popular
enough (`search_min_popularity`) regardless of language — that are releasing within
the checkpoint window and still have zero trailers get a `search.list` call. That
call costs 100 units (100x a `videos.list`), so spend is rationed three ways:

- only titles with no usable trailer are searched — one match stops all searching;
- each title is searched at most once per checkpoint (e.g. 45/21/10/4 days before
  release), recorded in `trailer_search_log`;
- a hard per-run cap (`search_max_units`), spent on the most popular titles first.

Search results are noisy (re-uploads, fan edits, fake "concept" trailers, reviews,
and — for short titles — games, TV seasons and other films with the same name), so a
hit must pass `evaluate_candidate`: name the movie, look like a trailer, avoid junk
words, not tag itself fan-made, carry no off-year, not come from an auto-generated
"- Topic" channel, have a trailer-like duration, and — when the matched title is
short — be confirmed by a cast/director name from `credits` in its title,
description or tags. Every rejection is logged with its reason in
`trailer_search_log.rejected` so the filters can be tuned without re-spending quota.
"""

from __future__ import annotations

import html
import json
import re
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
TOP_CAST = 8  # cast names (by billing order) used for confirmation
# Titles of common words collide with games, TV seasons and other films: "short"
# = ≤2 tokens, or 3 tokens with a stopword ("to the north", "the last delivery").
# "maharashtra epstein files" is 3 tokens but unmistakable.
_STOPWORDS = frozenset(
    ["the", "a", "an", "to", "of", "in", "on", "at", "and", "or", "my", "me", "you", "we", "us", "it", "is", "la", "le", "les", "el", "los", "las", "de", "del", "da", "do", "der", "die", "das", "den", "un", "une", "des", "du", "il", "lo", "gli", "una", "o", "os", "as", "e", "y", "et", "und", "в", "на", "и"]
)

# Arabic letter variants folded together (hamza-carrying alefs, ta marbuta, alef maqsura).
_ARABIC_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _strip_latin_accent(ch: str) -> str:
    """'é' → 'e', 'ñ' → 'n'; other scripts untouched (Arabic 'ئ' must stay composed)."""
    if ch.isascii() or not ("\u00c0" <= ch <= "\u024f" or "\u1e00" <= ch <= "\u1eff"):
        return ch
    base = unicodedata.normalize("NFD", ch)[0]
    return base if base.isascii() and base.isalpha() else ch


def normalize(text: str | None) -> str:
    """Casefold, strip accents/combining marks + punctuation, fold Arabic variants, collapse spaces.

    Latin accents are dropped ("tráiler" → "trailer", "Pokémon" → "pokemon") so
    Spanish/Portuguese/French spellings match; everything else stays NFKC-composed.
    """
    s = unicodedata.normalize("NFKC", html.unescape(text or "")).casefold()
    s = s.translate(_ARABIC_FOLD)
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat == "Mn":
            continue
        out.append(_strip_latin_accent(ch) if cat[0] in "LNM" else " ")
    return " ".join("".join(out).split())


def _norm_all(words: list[str]) -> tuple[str, ...]:
    return tuple(normalize(w) for w in words)


# Substring match: Arabic prefixes the article (الإعلان), plurals etc.
_TRAILER_WORDS = _norm_all([
    "trailer", "teaser", "glimpse", "bande annonce", "إعلان", "برومو",
    "ट्रेलर", "टीज़र", "டிரெய்லர்", "ட்ரெய்லர்", "டீசர்", "ట్రైలర్", "టీజర్",
    "ട്രെയിലർ", "ടീസർ", "ಟ್ರೈಲರ್", "ಟೀಸರ್", "ট্রেলার", "ਟ੍ਰੇਲਰ",
    # 2026-10: non-Indic/Arabic markets (popularity-tier eligibility)
    "tráiler", "予告", "特報", "예고편", "티저", "预告", "預告", "fragman", "трейлер",
    "тизер", "zwiastun",
])
_TEASER_WORDS = _norm_all([
    "teaser", "glimpse", "टीज़र", "டீசர்", "టీజర్", "ടീസർ", "ಟೀಸರ್", "特報", "티저", "тизер",
])
# Whole-word match: signals the video is *about* the trailer, not the trailer —
# or is a trailer for something else that shares the name (a game, a TV season,
# a song's "theme", a short film).
_JUNK_WORDS = _norm_all([
    "reaction", "reactions", "review", "reviews", "breakdown", "explained", "explanation",
    "fan made", "fanmade", "concept", "recreation", "spoof", "parody", "behind the scenes",
    "making", "box office", "public talk", "full movie", "scene", "scenes", "song", "songs",
    "lyric", "lyrical", "jukebox", "shorts", "status", "edit", "edits", "release date",
    "climax", "hidden", "clues", "mashup", "مراجعة", "ردة فعل", "تحليل", "रिव्यू",
    "season", "episode", "ep", "web series", "series", "game", "gameplay", "simulator",
    "update", "patch", "dlc", "anniversary", "theme", "ost", "bgm", "short film", "shortfilm",
])
_JUNK_UNLESS_GENRE = {"documentary": "Documentary"}  # junk unless the movie has that genre
# Channel names that mark re-uploaders / editors rather than studios.
_JUNK_CHANNEL_WORDS = _norm_all([
    "edits", "edit", "reaction", "reactions", "review", "reviews", "concept", "fanmade",
    "fan made", "topic",
])

# Substring match over description + tags: fan-made "trailers" for hyped films
# rank high and look official by title, but tag/disclaim themselves.
_FAKE_MARKERS = _norm_all([
    "fanmade", "fan made", "fan trailer", "concept trailer", "trailer concept",
    "notion trailer", "section 107", "fair use", "ai generated", "unofficial",
])
# "Official" in the markets we search: for a non-Latin-script title (which rarely
# collides with a game/TV name) this marker stands in for a credit match, since
# TMDB's credits are transliterated and never appear in an Arabic/Indic upload.
_OFFICIAL_MARKERS = _norm_all([
    "official", "الرسمي", "الرسمى", "आधिकारिक", "ऑफिशियल", "அதிகாரப்பூர்வ", "అధికారిక",
    "ഔദ്യോഗിക", "ಅಧಿಕೃತ", "অফিসিয়াল", "公式", "本予告", "공식", "官方", "oficial", "officiel",
    "officielle", "offizieller", "ufficiale", "официальный",
])
# A one-word title ("العربي", "Diya") also occurs as an ordinary word, so it must
# lead the video title ("Diya | bande annonce"), not trail it ("…بالوطن العربي").
SINGLE_WORD_TITLE_MAX_POS = 6


def is_short_title(name: str) -> bool:
    toks = name.split()
    return len(toks) <= 2 or (len(toks) == 3 and any(t in _STOPWORDS for t in toks))


def _is_latin(name: str) -> bool:
    return any(c.isascii() and c.isalpha() for c in name)


def confirmation_names(movie: dict[str, Any], credits: list[str] | None) -> list[str]:
    """Names that confirm a short title: cast/directors + production companies."""
    companies = [
        c.get("name") for c in (movie.get("production_companies") or []) if isinstance(c, dict)
    ]
    return [n for n in [*(credits or []), *companies] if n]


def _has_word(haystack: str, needle: str) -> bool:
    return f" {needle} " in f" {haystack} "


def _names_movie(t: str, name: str) -> bool:
    if " " in name:
        # "Jailer 2" also appears as the single token "jailer2".
        return _has_word(t, name) or _has_word(t, name.replace(" ", ""))
    return name in t.split()[:SINGLE_WORD_TITLE_MAX_POS]


def _titles(movie: dict[str, Any]) -> set[str]:
    return {normalize(movie.get("title")), normalize(movie.get("original_title"))} - {""}


def matched_title(movie: dict[str, Any], video_title: str) -> str | None:
    """The (normalized) movie title the video title names, longest first, or None."""
    t = normalize(video_title)
    for name in sorted(_titles(movie), key=len, reverse=True):
        if _names_movie(t, name):
            return name
    return None


def looks_like_trailer(movie: dict[str, Any], video_title: str) -> bool:
    """Title names the movie (either title) + a trailer word + no junk word."""
    return _title_reason(movie, video_title) is None


def _title_reason(movie: dict[str, Any], video_title: str) -> str | None:
    t = normalize(video_title)
    if matched_title(movie, video_title) is None:
        return "title_mismatch"
    if not any(w in t for w in _TRAILER_WORDS):
        return "no_trailer_word"
    junk = next((w for w in _JUNK_WORDS if _has_word(t, w)), None)
    if junk:
        return f"junk_word:{junk}"
    genres = {g.get("name") for g in (movie.get("genres") or []) if isinstance(g, dict)}
    for word, genre in _JUNK_UNLESS_GENRE.items():
        if _has_word(t, word) and genre not in genres:
            return f"junk_word:{word}"
    return None


def looks_fan_made(video: dict[str, Any]) -> bool:
    """videos.list resource self-identifies as fan-made/concept/fair-use."""
    snippet = video.get("snippet") or {}
    text = normalize(" ".join([snippet.get("description") or "", *(snippet.get("tags") or [])]))
    return any(m in text for m in _FAKE_MARKERS)


def _off_year(movie: dict[str, Any], video_title: str) -> str | None:
    """A year in the video title that isn't the movie's (±1) and isn't part of its name."""
    release = movie.get("primary_release_date")
    if not release:
        return None
    year = release.year if isinstance(release, date) else int(str(release)[:4])
    own = {y for name in (movie.get("title"), movie.get("original_title")) if name
           for y in _YEAR_RE.findall(name)}
    for y in _YEAR_RE.findall(video_title):
        if y not in own and abs(int(y) - year) > 1:
            return y
    return None


def credit_confirms(credits: list[str], *texts: str | None) -> bool:
    """Any cast/director name appears (whole-word phrase) in the given texts."""
    hay = normalize(" ".join(x for x in texts if x))
    return any(_has_word(hay, n) for n in (normalize(c) for c in credits) if n)


def evaluate_candidate(
    movie: dict[str, Any],
    *,
    title: str,
    description: str | None = None,
    tags: list[str] | None = None,
    channel_title: str | None = None,
    duration_s: int | None,
    credits: list[str] | None = None,
) -> str | None:
    """Why this search result is NOT the movie's trailer, or None if it passes.

    `credits` is the movie's confirmation list (top cast + directors); production
    companies from the movie row are added to it. It only matters for short titles
    (`is_short_title`): a Latin-script one must be confirmed by a name in the video's
    title/description/tags/channel; a non-Latin one may instead carry an "official"
    marker. With no names known at all, the release year or "official" stands in.
    """
    reason = _title_reason(movie, title)
    if reason:
        return reason
    if channel_title:
        ch = normalize(channel_title)
        junk = next((w for w in _JUNK_CHANNEL_WORDS if _has_word(ch, w)), None)
        if junk:
            return f"junk_channel:{junk}"
    year = _off_year(movie, title)
    if year:
        return f"off_year:{year}"
    if looks_fan_made({"snippet": {"description": description, "tags": tags or []}}):
        return "fan_made_marker"
    if duration_s is None or not MIN_DURATION_S <= duration_s <= MAX_DURATION_S:
        return f"duration:{duration_s}"
    name = matched_title(movie, title) or ""
    if is_short_title(name):
        names = confirmation_names(movie, credits)
        t = normalize(title)
        release = movie.get("primary_release_date")
        year_s = str(release)[:4] if release else None
        official = any(_has_word(t, m) for m in _OFFICIAL_MARKERS)
        if names:
            confirmed = credit_confirms(names, title, description, " ".join(tags or []), channel_title)
            if not confirmed and not (not _is_latin(name) and official):
                return "short_title_unconfirmed"
        elif not ((year_s and _has_word(t, year_s)) or official):
            return "short_title_no_credits"
    return None


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
    credits: list[str] | None = None,
    rejected: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Filter search.list `items` (relevance order) down to plausible trailers.

    `details` maps videoId → videos.list resource (for duration); a result missing
    from it is unavailable and dropped. `known` holds already-tracked video IDs.
    `credits` confirms short titles (see `evaluate_candidate`); when more than
    `max_matches` candidates pass, credit-confirmed ones win. `rejected`, if given,
    collects {video_id, reason} for every dropped result.
    """
    # Fake-trailer channels upload in bulk and only some uploads carry the
    # fan-made marker, so one flagged video condemns its whole channel.
    fake_channels = {
        (d.get("snippet") or {}).get("channelId") for d in details.values() if looks_fan_made(d)
    }
    rejected = rejected if rejected is not None else []

    def reject(vid: str, reason: str) -> None:
        rejected.append({"video_id": vid, "reason": reason})

    passed: list[tuple[bool, int, dict[str, Any]]] = []
    for pos, it in enumerate(items):
        vid = (it.get("id") or {}).get("videoId")
        snippet = it.get("snippet") or {}
        title = html.unescape(snippet.get("title") or "")
        if not vid or vid in known:
            continue
        if vid not in details:
            reject(vid, "unavailable")
            continue
        if snippet.get("channelId") in fake_channels:
            reject(vid, "fake_channel")
            continue
        d_snip = details[vid].get("snippet") or {}
        description = d_snip.get("description")
        tags = list(d_snip.get("tags") or [])
        channel_title = html.unescape(snippet.get("channelTitle") or "") or None
        duration = parse_iso8601_duration(
            (details[vid].get("contentDetails") or {}).get("duration")
        )
        reason = evaluate_candidate(
            movie, title=title, description=description, tags=tags,
            channel_title=channel_title, duration_s=duration, credits=credits,
        )
        if reason:
            reject(vid, reason)
            continue
        confirmed = credit_confirms(
            confirmation_names(movie, credits), title, description, " ".join(tags), channel_title
        )
        passed.append((
            confirmed, pos,
            {
                "youtube_video_id": vid,
                "name": title,
                "published_at": snippet.get("publishedAt"),
                "channel_id": snippet.get("channelId"),
                "channel_title": channel_title,
                "video_type": classify_title(title),
            },
        ))
    passed.sort(key=lambda x: (not x[0], x[1]))  # confirmed first, then relevance order
    picks = [p for _, _, p in passed[:max_matches]]
    for _, _, p in passed[max_matches:]:
        reject(p["youtube_video_id"], "over_max_matches")
    return picks


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
    credits = load_credits(bq, [int(m["tmdb_id"]) for m in due[:max_searches]])
    trailer_rows: list[TrailerRow] = []
    log_rows: list[TrailerSearchLogRow] = []
    for movie in due[:max_searches]:
        release: date = movie["primary_release_date"]
        query = build_query(movie)
        region = _region(movie)
        units_before = youtube.quota_units_used
        try:
            items = youtube.search_videos(
                query,
                published_after=(release - timedelta(days=PUBLISHED_LOOKBACK_DAYS)).isoformat()
                + "T00:00:00Z",
                relevance_language=movie["original_language"],
                region_code=region,
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

        rejected: list[dict[str, str]] = []
        picks = pick_trailers(
            movie, items, details, known=known, max_matches=settings.search_max_matches,
            credits=credits.get(int(movie["tmdb_id"])), rejected=rejected,
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
                region_code=region,
                relevance_language=movie["original_language"],
                rejected=json.dumps(rejected, ensure_ascii=False) if rejected else None,
            )
        )
        log.info(
            "search_trailers.searched",
            tmdb_id=movie["tmdb_id"], query=query, results=len(items), matched=len(picks),
            rejected=[r["reason"] for r in rejected],
        )

    _upsert_trailers(bq, trailer_rows)
    _upsert_search_log(bq, log_rows)
    log.info(
        "search_trailers.done",
        searches=len(log_rows), trailers_added=len(trailer_rows),
        movies_matched=sum(1 for r in log_rows if r.matched_video_ids),
    )
    return len(log_rows), len(trailer_rows)


def _region(movie: dict[str, Any]) -> str | None:
    """regionCode for search.list: the title's first production country."""
    for c in movie.get("origin_countries") or []:
        if c:
            return str(c)
    return None


def load_credits(bq: BigQueryClient, tmdb_ids: list[int]) -> dict[int, list[str]]:
    """Per movie: top-billed cast + directors — the names that confirm a short title."""
    if not tmdb_ids:
        return {}
    ids_sql = ", ".join(str(int(i)) for i in set(tmdb_ids))
    rows = bq.query(
        f"""
        SELECT tmdb_id, name
        FROM `{bq.project}.{bq.dataset}.credits`
        WHERE content_kind = 'movie' AND tmdb_id IN ({ids_sql}) AND name IS NOT NULL
          AND (
            (credit_kind = 'cast' AND order_index < {TOP_CAST})
            OR (credit_kind = 'crew' AND job LIKE '%Director%')
          )
        """
    )
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(int(r["tmdb_id"]), []).append(r["name"])
    return out


def _select_candidates(
    bq: BigQueryClient, settings: Settings, today: date, *, window_days: int
) -> list[dict[str, Any]]:
    """Upcoming in-scope movies with no usable trailer, plus when last searched.

    In scope = Indic/Arabic by language or country (TMDB's known blind spot), or any
    title at/above `search_min_popularity` (TMDB's popularity scale since 2025 puts
    the upcoming median near 1; 3 is roughly the top 5%).
    """
    sql = f"""
    SELECT m.tmdb_id, m.title, m.original_title, m.original_language, m.origin_countries,
           m.primary_release_date, m.popularity, m.genres, m.production_companies,
           s.last_searched_date
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
        OR m.popularity >= @min_pop
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
            "min_pop": float(settings.search_min_popularity),
        },
    )


def _upsert_search_log(bq: BigQueryClient, rows: list[TrailerSearchLogRow]) -> None:
    if not rows:
        return
    fields = list(TrailerSearchLogRow.model_fields.keys())
    bq.merge_rows(
        table="trailer_search_log",
        rows=rows,
        merge_keys=["movie_tmdb_id", "searched_date"],
        update_fields=[c for c in fields if c not in {"movie_tmdb_id", "searched_date"}],
        insert_fields=fields,
    )


# --- one-off recheck of search-found trailers -----------------------------------------

def recheck_search_trailers(
    bq: BigQueryClient, *, dry_run: bool = False
) -> list[dict[str, Any]]:
    """Re-apply `evaluate_candidate` to every search-found trailer (official IS NULL).

    Failures are flipped to tracking_status='unavailable' (history kept, stats spend
    stopped). Returns [{youtube_video_id, movie_tmdb_id, name, reason}] for the rows
    that fail; with dry_run nothing is written.
    """
    rows = bq.query(
        f"""
        SELECT t.youtube_video_id, t.name, t.description, t.channel_title, t.duration_seconds,
               m.tmdb_id, m.title, m.original_title, m.primary_release_date, m.genres,
               m.production_companies
        FROM `{bq.project}.{bq.dataset}.trailers` t
        JOIN `{bq.project}.{bq.dataset}.movies` m ON m.tmdb_id = t.movie_tmdb_id
        WHERE t.content_kind = 'movie' AND t.official IS NULL
          AND t.tracking_status != 'unavailable'
        ORDER BY t.first_seen_at DESC
        """
    )
    credits = load_credits(bq, [int(r["tmdb_id"]) for r in rows])
    bad: list[dict[str, Any]] = []
    for r in rows:
        reason = evaluate_candidate(
            r, title=r["name"] or "", description=r["description"],
            channel_title=r["channel_title"], duration_s=r["duration_seconds"],
            credits=credits.get(int(r["tmdb_id"])),
        )
        if reason:
            bad.append({
                "youtube_video_id": r["youtube_video_id"], "movie_tmdb_id": r["tmdb_id"],
                "movie": r["title"], "name": r["name"], "reason": reason,
            })
    if bad and not dry_run:
        now = datetime.now(UTC)
        bq.update_from_dicts(
            table="trailers",
            rows=[{"youtube_video_id": b["youtube_video_id"], "when": now} for b in bad],
            merge_keys=["youtube_video_id"],
            update_clause_sql="T.tracking_status = 'unavailable', T.last_collected_at = S.when",
        )
    return bad
