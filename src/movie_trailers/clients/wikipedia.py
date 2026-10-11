"""Wikipedia + Wikidata read clients (box-office infobox values).

- `WikidataClient.enwiki_titles(imdb_ids)` — SPARQL, 50 IMDb ids per query, returns the
  English-Wikipedia article title per id (Wikidata's own box-office claims are empty for
  recent films, so we read the infobox instead).
- `WikipediaClient.wikitext(title)` — `action=parse&prop=wikitext`, ~1 request/s.
- `parse_infobox_money(wikitext, field)` — the infobox `gross` / `budget` value as
  `(amount, currency)` for the formats Indian and Hollywood film articles use.

Both clients send a descriptive User-Agent with a contact address, as Wikimedia's policy asks.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

_UNITS = {
    "crore": 1e7, "cr": 1e7, "lakh": 1e5, "lakhs": 1e5, "thousand": 1e3,
    "million": 1e6, "billion": 1e9, "m": 1e6, "bn": 1e9,
}
_RANGE_SEP = r"(?:[-–—]|\s+to\s+)"
_NUM = r"\d[\d,]*(?:\.\d+)?"


def _user_agent(contact: str) -> str:
    return f"movie-trailers/1.0 (https://github.com/PeachPredict/movie-trailers; {contact})"


class WikidataClient:
    SPARQL = "https://query.wikidata.org/sparql"

    def __init__(self, contact: str, http_client: httpx.Client | None = None):
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"User-Agent": _user_agent(contact), "Accept": "application/sparql-results+json"},
        )

    def close(self) -> None:
        self._http.close()

    @retry(reraise=True, retry=retry_if_exception_type(httpx.TransportError),
           stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def enwiki_titles(self, imdb_ids: list[str]) -> dict[str, str]:
        """IMDb id → English Wikipedia article title (ids without an article are absent)."""
        out: dict[str, str] = {}
        for i in range(0, len(imdb_ids), 50):
            chunk = [x for x in imdb_ids[i : i + 50] if re.fullmatch(r"tt\d+", x)]
            if not chunk:
                continue
            values = " ".join(f'"{x}"' for x in chunk)
            query = (
                "SELECT ?imdb ?article WHERE { VALUES ?imdb { " + values + " } "
                "?item wdt:P345 ?imdb . ?article schema:about ?item ; "
                "schema:isPartOf <https://en.wikipedia.org/> . }"
            )
            resp = self._http.get(self.SPARQL, params={"query": query, "format": "json"})
            resp.raise_for_status()
            for b in resp.json().get("results", {}).get("bindings", []):
                url = b.get("article", {}).get("value", "")
                title = httpx.URL(url).path.rsplit("/", 1)[-1]
                if title:
                    out[b["imdb"]["value"]] = title.replace("_", " ")
            if i + 50 < len(imdb_ids):
                time.sleep(1.0)
        return out


class WikipediaClient:
    API = "https://en.wikipedia.org/w/api.php"

    def __init__(self, contact: str, http_client: httpx.Client | None = None,
                 min_interval_s: float = 1.0):
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": _user_agent(contact), "Accept": "application/json"},
        )
        self._min_interval = min_interval_s
        self._last = 0.0

    def close(self) -> None:
        self._http.close()

    @retry(reraise=True, retry=retry_if_exception_type(httpx.TransportError),
           stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def wikitext(self, title: str) -> str | None:
        """The article's wikitext (redirects followed), or None if there is no such page."""
        wait = self._min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        resp = self._http.get(self.API, params={
            "action": "parse", "page": title, "prop": "wikitext", "redirects": 1, "format": "json",
        })
        self._last = time.monotonic()
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        if "error" in data:
            return None
        return (data.get("parse", {}).get("wikitext") or {}).get("*")


# --- infobox money parsing -----------------------------------------------------------

def _strip_templates(text: str, names: tuple[str, ...]) -> str:
    """Remove `{{name|…}}` templates (balanced braces) for the given names."""
    out, i = [], 0
    low = text.lower()
    while i < len(text):
        if text.startswith("{{", i):
            j = i + 2
            name = re.match(r"\s*([A-Za-z$ _-]+)", low[j:])
            if name and name.group(1).strip() in names:
                depth, k = 0, i
                while k < len(text):
                    if text.startswith("{{", k):
                        depth += 1
                        k += 2
                        continue
                    if text.startswith("}}", k):
                        depth -= 1
                        k += 2
                        if depth == 0:
                            break
                        continue
                    k += 1
                i = k
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def infobox_field(wikitext: str, field: str) -> str | None:
    """Raw value of `| field = …` in the first infobox (first line only)."""
    m = re.search(r"^\|\s*" + re.escape(field) + r"\s*=\s*(.*)$", wikitext, flags=re.M)
    return m.group(1).strip() if m else None


def _to_amount(num: str, unit: str | None) -> float:
    return float(num.replace(",", "")) * (_UNITS.get((unit or "").lower(), 1.0))


def parse_money(value: str) -> tuple[float, str] | None:
    """'{{INR|330–400}} crore' → (3.65e9, 'INR'); '$12.3 million' → (1.23e7, 'USD').

    Handles INR / INRConvert / US$ / USD templates, ₹ and $ prefixes, crore/lakh/
    million/billion words, and ranges (midpoint). Returns None when nothing parses.
    """
    v = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", " ", value, flags=re.S)
    v = _strip_templates(v, ("efn", "cite web", "cite news", "sfn", "refn", "citation", "cite"))
    v = re.sub(r"\{\{\s*(estimation|circa|c\.|approx|est\.?)\s*\}\}", " ", v, flags=re.I)
    v = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", v)  # [[a|b]] → b
    v = v.replace("''", "").replace("&nbsp;", " ")

    currency: str | None = None
    # {{INRConvert|143.60|c}} / {{INRConvert|330-400|c}}
    m = re.search(r"\{\{\s*INRConvert\s*\|\s*(" + _NUM + r"(?:\s*" + _RANGE_SEP + r"\s*" + _NUM + r")?)\s*\|\s*([a-z]+)", v, flags=re.I)
    if m:
        unit = {"c": "crore", "l": "lakh", "k": "thousand", "m": "million", "b": "billion"}.get(m.group(2).lower(), m.group(2))
        return _range_amount(m.group(1), unit), "INR"
    # {{INR|330–400}} crore  /  {{US$|12.3 million}}  /  {{USD|1.2 billion}}
    m = re.search(r"\{\{\s*(INR|US\$|USD)\s*\|\s*([^}|]+)", v, flags=re.I)
    if m:
        currency = "INR" if m.group(1).upper() == "INR" else "USD"
        inner = m.group(2)
        rest = v[m.end():]
        amount = _amount_with_unit(inner, rest)
        return (amount, currency) if amount is not None else None
    # ₹320–325 crore  /  Rs. 50 crore  /  US$1.2 billion  /  $12.3 million
    m = re.search(r"(₹|Rs\.?|INR|US\$|\$|USD)\s*(" + _NUM + r"(?:\s*" + _RANGE_SEP + r"\s*" + _NUM + r")?)", v)
    if m:
        currency = "INR" if m.group(1) in ("₹", "Rs", "Rs.", "INR") else "USD"
        amount = _amount_with_unit(m.group(2), v[m.end():])
        return (amount, currency) if amount is not None else None
    return None


def _amount_with_unit(number_part: str, rest: str) -> float | None:
    m = re.match(r"\s*(" + _NUM + r"(?:\s*" + _RANGE_SEP + r"\s*" + _NUM + r")?)\s*([A-Za-z]+)?", number_part)
    if not m:
        return None
    unit = m.group(2)
    if not unit or unit.lower() not in _UNITS:
        u = re.match(r"[\s}]*([A-Za-z]+)", rest)  # the unit may follow the closing braces
        unit = u.group(1) if u and u.group(1).lower() in _UNITS else None
    return _range_amount(m.group(1), unit)


def _range_amount(num: str, unit: str | None) -> float:
    parts = re.split(_RANGE_SEP, num)
    vals = [_to_amount(p.strip(), unit) for p in parts if p.strip()]
    return sum(vals) / len(vals)


def parse_infobox_money(wikitext: str, field: str) -> tuple[float, str] | None:
    raw = infobox_field(wikitext, field)
    return parse_money(raw) if raw else None
