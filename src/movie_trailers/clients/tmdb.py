from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class TMDBClient:
    """Minimal TMDB v3 client. Accepts either a v3 API key or a v4 read token.

    - v3 API key: 32-char hex, sent as ?api_key=… query param.
    - v4 read token: JWT (starts with "eyJ"), sent as Authorization: Bearer … header.
    The TMDB API has no daily cap; rate limit ~50 req/s. Retries on 429/5xx.
    """

    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str, http_client: httpx.Client | None = None):
        self._use_bearer = api_key.startswith("eyJ")
        self._api_key = api_key
        headers = {"Accept": "application/json"}
        if self._use_bearer:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = http_client or httpx.Client(
            base_url=self.BASE_URL,
            timeout=httpx.Timeout(20.0, connect=5.0),
            headers=headers,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TMDBClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @retry(
        reraise=True,
        retry=retry_if_exception_type((httpx.HTTPError,)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    )
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        p = dict(params or {})
        if not self._use_bearer:
            p["api_key"] = self._api_key
        resp = self._http.get(path, params=p)
        if resp.status_code in {429, 500, 502, 503, 504}:
            resp.raise_for_status()
        resp.raise_for_status()
        return resp.json()

    def _paginate(
        self, path: str, params: dict[str, Any], max_pages: int = 500
    ) -> Iterator[dict[str, Any]]:
        page = 1
        while page <= max_pages:
            payload = self._get(path, {**params, "page": page})
            results = payload.get("results", [])
            yield from results
            total_pages = payload.get("total_pages", page)
            if page >= total_pages or not results:
                return
            page += 1

    # --- Movies ---

    def discover_movies(
        self,
        release_date_gte: str,
        release_date_lte: str,
        region: str | None = None,
        sort_by: str = "popularity.desc",
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "primary_release_date.gte": release_date_gte,
            "primary_release_date.lte": release_date_lte,
            "sort_by": sort_by,
            "include_adult": "false",
            "include_video": "false",
        }
        if region:
            params["region"] = region
        yield from self._paginate("/discover/movie", params)

    def movie_videos(self, tmdb_id: int) -> list[dict[str, Any]]:
        return self._get(f"/movie/{tmdb_id}/videos").get("results", [])

    def movie_details(self, tmdb_id: int) -> dict[str, Any]:
        return self._get(
            f"/movie/{tmdb_id}",
            {"append_to_response": "external_ids,credits,watch/providers"},
        )

    # --- TV ---

    def discover_tv(
        self,
        first_air_date_gte: str,
        first_air_date_lte: str,
        sort_by: str = "popularity.desc",
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "first_air_date.gte": first_air_date_gte,
            "first_air_date.lte": first_air_date_lte,
            "sort_by": sort_by,
            "include_adult": "false",
        }
        yield from self._paginate("/discover/tv", params)

    def tv_details(self, tmdb_id: int) -> dict[str, Any]:
        return self._get(
            f"/tv/{tmdb_id}",
            {"append_to_response": "external_ids,credits,watch/providers"},
        )

    def tv_videos(self, tmdb_id: int) -> list[dict[str, Any]]:
        return self._get(f"/tv/{tmdb_id}/videos").get("results", [])

    def tv_season_videos(self, tmdb_id: int, season_number: int) -> list[dict[str, Any]]:
        return self._get(f"/tv/{tmdb_id}/season/{season_number}/videos").get("results", [])
