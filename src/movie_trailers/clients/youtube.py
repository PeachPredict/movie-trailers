from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class CommentsDisabledError(Exception):
    """Raised when commentThreads.list returns 403 commentsDisabled."""


class QuotaExceededError(Exception):
    """Raised when the API responds with a quotaExceeded error."""


@dataclass
class CommentsResult:
    items: list[dict[str, Any]]


class YouTubeClient:
    """YouTube Data API v3 client with a per-process quota counter.

    - videos.list: 1 unit per call, up to 50 IDs per call.
    - commentThreads.list: 1 unit per call, up to 100 results per page.
    - search.list: 100 units per call — budget it (see pipeline/search_trailers.py).
    Quota is reset by YouTube at midnight Pacific Time.
    """

    BASE_URL = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key: str, http_client: httpx.Client | None = None):
        self._api_key = api_key
        self._http = http_client or httpx.Client(
            base_url=self.BASE_URL,
            timeout=httpx.Timeout(20.0, connect=5.0),
            headers={"Accept": "application/json"},
        )
        self.quota_units_used = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> YouTubeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @retry(
        reraise=True,
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    )
    def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        p = dict(params)
        p["key"] = self._api_key
        resp = self._http.get(path, params=p)
        # Retry on transient. Surface 4xx (except 403 with reason we handle) to caller.
        if resp.status_code in {500, 502, 503, 504}:
            resp.raise_for_status()
        return resp

    @staticmethod
    def _parse_error_reasons(resp: httpx.Response) -> list[str]:
        try:
            body = resp.json()
        except Exception:
            return []
        errors = body.get("error", {}).get("errors", []) or []
        return [e.get("reason", "") for e in errors]

    def videos_list(self, video_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Batch-fetch up to 50 video resources. Caller chunks larger lists.

        Costs 1 unit per call. Missing IDs (deleted/private/region-blocked) are
        silently omitted by the API — caller must diff requested vs returned.
        """
        ids = list(video_ids)
        if not ids:
            return []
        if len(ids) > 50:
            raise ValueError("videos_list accepts at most 50 IDs per call")
        resp = self._get(
            "/videos",
            {
                "part": "statistics,snippet,contentDetails,status",
                "id": ",".join(ids),
                "maxResults": 50,
            },
        )
        self.quota_units_used += 1
        if resp.status_code >= 400:
            if "quotaExceeded" in self._parse_error_reasons(resp):
                raise QuotaExceededError("YouTube videos.list quota exceeded")
            resp.raise_for_status()
        return resp.json().get("items", [])

    def comment_threads_top(self, video_id: str, max_results: int = 30) -> CommentsResult:
        """Fetch top-`max_results` (≤100) comment threads ordered by relevance.

        Costs 1 unit. Raises CommentsDisabledError on 403 commentsDisabled.
        """
        if not 1 <= max_results <= 100:
            raise ValueError("max_results must be 1..100")
        resp = self._get(
            "/commentThreads",
            {
                "part": "snippet",
                "order": "relevance",
                "maxResults": max_results,
                "videoId": video_id,
                "textFormat": "plainText",
            },
        )
        self.quota_units_used += 1
        if resp.status_code == 403:
            reasons = self._parse_error_reasons(resp)
            if "commentsDisabled" in reasons:
                raise CommentsDisabledError(video_id)
            if "quotaExceeded" in reasons:
                raise QuotaExceededError("YouTube commentThreads.list quota exceeded")
        if resp.status_code >= 400:
            resp.raise_for_status()
        return CommentsResult(items=resp.json().get("items", []))

    def search_videos(
        self,
        query: str,
        *,
        published_after: str | None = None,
        relevance_language: str | None = None,
        region_code: str | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """search.list for videos by relevance. Costs 100 units — 100x videos.list.

        `published_after` is RFC 3339 (e.g. "2026-01-01T00:00:00Z").
        """
        params: dict[str, Any] = {
            "part": "snippet",
            "type": "video",
            "order": "relevance",
            "q": query,
            "maxResults": max_results,
        }
        if published_after:
            params["publishedAfter"] = published_after
        if relevance_language:
            params["relevanceLanguage"] = relevance_language
        if region_code:
            params["regionCode"] = region_code
        resp = self._get("/search", params)
        self.quota_units_used += 100
        if resp.status_code >= 400:
            if "quotaExceeded" in self._parse_error_reasons(resp):
                raise QuotaExceededError("YouTube search.list quota exceeded")
            resp.raise_for_status()
        return resp.json().get("items", [])
