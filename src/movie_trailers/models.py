from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

ContentKind = Literal["movie", "tv"]
VideoType = Literal["Teaser", "Trailer", "Official Trailer"]
TrackingStatus = Literal["active", "ended", "unavailable"]
SnapshotKind = Literal["at_discovery", "pre_release"]
RunPhase = Literal[
    "discover_movies", "discover_tv", "stats", "comments", "transcripts", "box_office",
    "predictions", "excitement", "search_trailers", "news", "sacnilk", "wikipedia",
]
TranscriptSource = Literal["yta", "whisper", "failed"]


class Genre(BaseModel):
    id: int
    name: str


class Company(BaseModel):
    id: int | None = None
    name: str | None = None
    logo_path: str | None = None
    origin_country: str | None = None


class SpokenLanguage(BaseModel):
    iso_639_1: str | None = None
    english_name: str | None = None
    name: str | None = None


class MovieRow(BaseModel):
    tmdb_id: int
    imdb_id: str | None = None
    title: str | None = None
    original_title: str | None = None
    original_language: str | None = None
    primary_release_date: date | None = None
    genres: list[Genre] = Field(default_factory=list)
    status: str | None = None
    popularity: float | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None
    overview: str | None = None
    tagline: str | None = None
    homepage: str | None = None
    runtime_minutes: int | None = None
    production_companies: list[Company] = Field(default_factory=list)
    origin_countries: list[str] = Field(default_factory=list)
    spoken_languages: list[SpokenLanguage] = Field(default_factory=list)
    collected_at: datetime


class TvShowRow(BaseModel):
    tmdb_id: int
    name: str | None = None
    original_name: str | None = None
    original_language: str | None = None
    first_air_date: date | None = None
    status: str | None = None
    genres: list[Genre] = Field(default_factory=list)
    vote_average: float | None = None
    vote_count: int | None = None
    popularity: float | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None
    overview: str | None = None
    tagline: str | None = None
    homepage: str | None = None
    production_companies: list[Company] = Field(default_factory=list)
    networks: list[Company] = Field(default_factory=list)
    origin_countries: list[str] = Field(default_factory=list)
    spoken_languages: list[SpokenLanguage] = Field(default_factory=list)
    collected_at: datetime


class TvSeasonRow(BaseModel):
    tv_tmdb_id: int
    season_number: int
    name: str | None = None
    air_date: date | None = None
    episode_count: int | None = None
    overview: str | None = None
    poster_path: str | None = None
    collected_at: datetime


class TrailerRow(BaseModel):
    youtube_video_id: str
    content_kind: ContentKind
    movie_tmdb_id: int | None = None
    tv_tmdb_id: int | None = None
    tv_season_number: int | None = None
    video_type: VideoType
    name: str | None = None
    published_at: datetime | None = None
    channel_id: str | None = None
    channel_title: str | None = None
    official: bool | None = None
    tracking_end_date: date | None = None
    thumbnail_url: str | None = None
    description: str | None = None
    duration_seconds: int | None = None
    definition: str | None = None
    embeddable: bool | None = None
    region_blocked: list[str] = Field(default_factory=list)
    first_seen_at: datetime
    last_collected_at: datetime | None = None
    tracking_status: TrackingStatus = "active"
    comments_disabled: bool = False
    comments_at_discovery_captured_at: datetime | None = None
    comments_pre_release_captured_at: datetime | None = None
    transcript_captured_at: datetime | None = None


class TrailerStatsDailyRow(BaseModel):
    youtube_video_id: str
    collected_date: date
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    favorite_count: int | None = None
    collected_at: datetime


class TrailerCommentSnapshotRow(BaseModel):
    youtube_video_id: str
    snapshot_kind: SnapshotKind
    snapshot_date: date
    comment_id: str
    text: str | None = None
    like_count: int | None = None
    total_reply_count: int | None = None
    author_channel_id: str | None = None
    author_display_name: str | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    rank: int
    collected_at: datetime


class TrailerTranscriptRow(BaseModel):
    youtube_video_id: str
    source: TranscriptSource  # 'yta' | 'whisper' | 'failed'
    track_kind: str | None = None
    language: str | None = None
    text: str | None = None
    word_count: int | None = None
    char_count: int | None = None
    error: str | None = None
    captured_at: datetime
    # Provenance + confidence (2026-10): which model produced it and how sure it was.
    model: str | None = None  # 'yta' | 'whisper-<size>'
    language_probability: float | None = None
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None
    audio_duration_s: float | None = None
    quality: str | None = None  # 'ok' | 'suspect' | 'hallucinated' | 'empty'
    attempts: int = 1  # transient failures retry up to transcripts.MAX_ATTEMPTS


class WatchProviderRow(BaseModel):
    tmdb_id: int
    content_kind: ContentKind
    region: str
    kind: Literal["flatrate", "rent", "buy", "free", "ads"]
    provider_id: int
    provider_name: str | None = None
    logo_path: str | None = None
    display_priority: int | None = None
    link: str | None = None
    collected_at: datetime


class CreditRow(BaseModel):
    tmdb_id: int
    content_kind: ContentKind
    credit_kind: Literal["cast", "crew"]
    person_id: int
    name: str | None = None
    character: str | None = None
    job: str | None = None
    department: str | None = None
    profile_path: str | None = None
    order_index: int | None = None
    collected_at: datetime


class BoxOfficeObservationRow(BaseModel):
    """One box-office / admissions observation (see box_office_observations DDL)."""

    tmdb_id: int
    source: str  # 'tmdb' | 'omdb' | 'ancine' | 'sacnilk' | 'wikipedia'
    market: str  # 'WW' | 'US' | 'BR' | 'IN'
    metric: str  # see the box_office_observations DDL comment for the enum
    value: float | None = None
    cumulative: bool
    period_start: date | None = None
    period_end: date
    budget_usd: int | None = None
    raw_title: str | None = None
    match_method: str | None = None
    observed_at: datetime


class TrailerSearchLogRow(BaseModel):
    """One YouTube search.list call made to find a movie's missing trailers."""

    movie_tmdb_id: int
    searched_date: date
    searched_at: datetime
    query: str
    results_returned: int
    matched_video_ids: list[str] = Field(default_factory=list)
    quota_units: int
    region_code: str | None = None
    relevance_language: str | None = None
    rejected: str | None = None  # JSON [{video_id, reason}] for every dropped result


class DailyRunLogRow(BaseModel):
    run_id: str
    phase: RunPhase
    started_at: datetime
    finished_at: datetime | None = None
    trailers_processed: int | None = None
    quota_units_used: int | None = None
    errors: int | None = None
    notes: str | None = None


class ContentPredictionRow(BaseModel):
    prediction_id: str  # '{movie_tmdb_id}-{predicted_at}' ('{id}-{date}-opening' for opening_tier)
    kind: str  # 'trailer_due' | 'opening_tier'
    movie_tmdb_id: int
    title: str | None = None
    predicted_at: date
    horizon_days: int
    days_to_release_at_prediction: int | None = None
    vs_peak_at_prediction: float | None = None
    basis: str | None = None
    status: str = "open"  # 'open' | 'hit' | 'miss'
    resolved_at: date | None = None
    resolved_youtube_video_id: str | None = None
    resolved_lag_days: int | None = None
    created_at: datetime
    updated_at: datetime
    # opening_tier (2026-10): what was called and what the box office said.
    predicted_value: str | None = None  # 'high' | 'mid' | 'low'
    signal_value: float | None = None  # pre-release trailer views behind the call
    observed_value: float | None = None
    observed_metric: str | None = None  # 'admissions' | 'domestic_gross_usd' | 'revenue_usd' | 'none'
    observed_market: str | None = None  # 'BR' | 'US' | 'WW'


class TrailerCommentExcitementRow(BaseModel):
    youtube_video_id: str
    model_version: str  # distilled student version, e.g. 'mlv1-minilm-l6-20260627'
    scored_date: date
    excitement: float | None = None  # 0..100
    snapshot_kind: SnapshotKind = "at_discovery"
    n_comments: int | None = None
    mean_like_count: float | None = None
    source_snapshot_date: date | None = None
    created_at: datetime


class SacnilkPageRow(BaseModel):
    """Per-movie cache of the Sacnilk page lookup (so unmatched titles aren't re-fetched daily)."""

    tmdb_id: int
    slug: str | None = None
    status: str  # 'matched' | 'unmatched' | 'not_found'
    last_fetched_at: datetime
    last_changed_at: datetime | None = None
    note: str | None = None


class WikipediaPageRow(BaseModel):
    tmdb_id: int
    imdb_id: str | None = None
    enwiki_title: str | None = None
    status: str  # 'ok' | 'no_gross' | 'no_article' | 'missing'
    resolved_at: datetime


class MovieNewsDailyRow(BaseModel):
    tmdb_id: int
    date: date
    articles: int
    confirmed_articles: int  # title AND a cast/director name in the same article
    articles_used: int  # confirmed for short titles, else articles
    sources: int
    avg_tone: float | None = None
    top_sources: list[str] = Field(default_factory=list)
    created_at: datetime
