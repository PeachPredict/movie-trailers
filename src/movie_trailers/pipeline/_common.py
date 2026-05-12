from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import structlog

from movie_trailers.models import (
    Company,
    ContentKind,
    CreditRow,
    Genre,
    SpokenLanguage,
    WatchProviderRow,
)

log = structlog.get_logger()

TRACKED_VIDEO_TYPES = {"Teaser", "Trailer"}
TOP_CAST_N = 10
KEY_CREW_JOBS = {"Director", "Writer", "Screenplay", "Producer", "Executive Producer", "Creator"}
WATCH_PROVIDER_KINDS: tuple[Literal["flatrate", "rent", "buy", "free", "ads"], ...] = (
    "flatrate", "rent", "buy", "free", "ads",
)


def parse_genres(items: list[dict[str, Any]] | None) -> list[Genre]:
    return [Genre(id=int(g["id"]), name=str(g.get("name", ""))) for g in (items or [])]


def parse_companies(items: list[dict[str, Any]] | None) -> list[Company]:
    return [
        Company(
            id=c.get("id"),
            name=c.get("name"),
            logo_path=c.get("logo_path"),
            origin_country=c.get("origin_country"),
        )
        for c in (items or [])
    ]


def parse_languages(items: list[dict[str, Any]] | None) -> list[SpokenLanguage]:
    return [
        SpokenLanguage(
            iso_639_1=lang.get("iso_639_1"),
            english_name=lang.get("english_name"),
            name=lang.get("name"),
        )
        for lang in (items or [])
    ]


def parse_origin_countries(details: dict[str, Any]) -> list[str]:
    # TV uses `origin_country` (list of ISO codes); movies use `production_countries` (list of dicts).
    if "origin_country" in details and isinstance(details["origin_country"], list):
        return [str(c) for c in details["origin_country"] if c]
    return [
        str(c["iso_3166_1"]) for c in (details.get("production_countries") or [])
        if c.get("iso_3166_1")
    ]


def parse_published_at(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_iso8601_duration(s: str | None) -> int | None:
    """Parse YouTube's ISO 8601 PT#H#M#S duration into seconds. Returns None on failure."""
    if not s or not s.startswith("PT"):
        return None
    import re

    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    if not m:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return hours * 3600 + minutes * 60 + seconds


def watch_providers_from_details(
    details: dict[str, Any],
    *,
    tmdb_id: int,
    content_kind: ContentKind,
    now: datetime,
) -> list[WatchProviderRow]:
    """Flatten TMDB watch/providers append_to_response into rows."""
    block = (details.get("watch/providers") or {}).get("results") or {}
    rows: list[WatchProviderRow] = []
    for region, region_block in block.items():
        if not isinstance(region_block, dict):
            continue
        link = region_block.get("link")
        for kind in WATCH_PROVIDER_KINDS:
            providers = region_block.get(kind) or []
            for p in providers:
                if not p.get("provider_id"):
                    continue
                rows.append(
                    WatchProviderRow(
                        tmdb_id=tmdb_id,
                        content_kind=content_kind,
                        region=region,
                        kind=kind,
                        provider_id=int(p["provider_id"]),
                        provider_name=p.get("provider_name"),
                        logo_path=p.get("logo_path"),
                        display_priority=p.get("display_priority"),
                        link=link,
                        collected_at=now,
                    )
                )
    return rows


def credits_from_details(
    details: dict[str, Any],
    *,
    tmdb_id: int,
    content_kind: ContentKind,
    now: datetime,
) -> list[CreditRow]:
    credits = details.get("credits") or {}
    rows: list[CreditRow] = []

    # Top cast by `order` (TMDB lists in this order, but be defensive).
    cast = sorted(
        (c for c in (credits.get("cast") or []) if c.get("id")),
        key=lambda c: c.get("order", 99999),
    )[:TOP_CAST_N]
    for c in cast:
        rows.append(
            CreditRow(
                tmdb_id=tmdb_id,
                content_kind=content_kind,
                credit_kind="cast",
                person_id=int(c["id"]),
                name=c.get("name"),
                character=c.get("character"),
                department="Acting",
                profile_path=c.get("profile_path"),
                order_index=c.get("order"),
                collected_at=now,
            )
        )

    # Key crew, deduped by person — a person can hold multiple key jobs (e.g. Director + Writer).
    crew_by_person: dict[int, dict[str, Any]] = {}
    for c in credits.get("crew") or []:
        if not c.get("id"):
            continue
        if c.get("job") not in KEY_CREW_JOBS:
            continue
        pid = int(c["id"])
        if pid not in crew_by_person:
            crew_by_person[pid] = {
                "name": c.get("name"),
                "jobs": [],
                "department": c.get("department"),
                "profile_path": c.get("profile_path"),
            }
        crew_by_person[pid]["jobs"].append(c.get("job"))
    for pid, agg in crew_by_person.items():
        rows.append(
            CreditRow(
                tmdb_id=tmdb_id,
                content_kind=content_kind,
                credit_kind="crew",
                person_id=pid,
                name=agg["name"],
                job=", ".join(sorted(set(agg["jobs"]))),
                department=agg["department"],
                profile_path=agg["profile_path"],
                order_index=0,
                collected_at=now,
            )
        )
    return rows


def is_trailer_video(video: dict[str, Any]) -> bool:
    """Plan filter: Teaser + Trailer + Official Trailer (skip clips/featurettes).

    TMDB `type` is one of: Trailer, Teaser, Clip, Featurette, Behind the Scenes,
    Bloopers, Opening Credits. We keep Teaser/Trailer; the "Official Trailer"
    requirement is treated as a name match within type=Trailer.
    """
    if str(video.get("site", "")).lower() != "youtube":
        return False
    return video.get("type") in TRACKED_VIDEO_TYPES


def classify_video_type(video: dict[str, Any]) -> str:
    """Return 'Official Trailer' if name contains it, else the TMDB type."""
    name = (video.get("name") or "")
    if "official trailer" in name.lower():
        return "Official Trailer"
    return str(video["type"])  # 'Trailer' or 'Teaser'
