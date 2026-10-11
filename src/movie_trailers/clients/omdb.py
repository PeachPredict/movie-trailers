"""OMDb client — the one field we want is `BoxOffice` (US domestic gross, sourced
from Box Office Mojo), looked up by IMDb id.

Free tier: 1,000 requests/day per key; the limit and a bad key both come back as
HTTP 401 with an `Error` message, which raises `OMDbAuthError` so the caller stops
instead of burning the run. A title OMDb doesn't know returns `Response: "False"`.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

_MONEY_RE = re.compile(r"[^0-9]")


class OMDbAuthError(Exception):
    """Invalid key or daily request limit reached — stop calling for this run."""


def parse_box_office(value: Any) -> int | None:
    """'$389,804,217' → 389804217; 'N/A' / '' / None → None."""
    if not value or not isinstance(value, str):
        return None
    digits = _MONEY_RE.sub("", value)
    return int(digits) if digits else None


class OMDbClient:
    BASE_URL = "https://www.omdbapi.com"

    def __init__(self, api_key: str, http_client: httpx.Client | None = None):
        self._api_key = api_key
        self._http = http_client or httpx.Client(
            base_url=self.BASE_URL,
            timeout=httpx.Timeout(20.0, connect=5.0),
            headers={"Accept": "application/json"},
        )
        self.calls = 0

    def close(self) -> None:
        self._http.close()

    @retry(
        reraise=True,
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    )
    def by_imdb_id(self, imdb_id: str) -> dict[str, Any] | None:
        """The OMDb record for an IMDb id, or None when OMDb has no such title."""
        resp = self._http.get("/", params={"apikey": self._api_key, "i": imdb_id, "r": "json"})
        self.calls += 1
        if resp.status_code == 401:
            msg = (resp.json().get("Error") if resp.headers.get("content-type", "").startswith(
                "application/json") else resp.text)
            raise OMDbAuthError(str(msg))
        resp.raise_for_status()
        data = resp.json()
        if str(data.get("Response", "")).lower() != "true":
            return None
        return data

    def domestic_gross(self, imdb_id: str) -> int | None:
        """US domestic gross in USD, or None when unknown."""
        data = self.by_imdb_id(imdb_id)
        return parse_box_office(data.get("BoxOffice")) if data else None
