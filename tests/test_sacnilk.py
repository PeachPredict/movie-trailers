"""Sacnilk page parsing, slug discovery, verification, and the phase's politeness rules."""
from datetime import date
from unittest.mock import MagicMock

import pytest

from movie_trailers.config import Settings
from movie_trailers.pipeline import sacnilk as sk

PAGE = """<html><head><title>Dhanushkodiyilekku Box Office</title><script>var x=1;</script></head><body>
<nav>Box Office | India Net | ₹999 Cr nav junk</nav>
<div>📅 Release Information</div><span>Theatrical Release:</span> <span>09 Oct 2026</span>
<h3>🎭 Cast &amp; Crew</h3><h4>Cast</h4>
<a href="https://www.sacnilk.com/tag/Nisthar_Ahammed">Nisthar Ahammed</a>
<a href="/tag/Sudheer_Karamana">Sudheer Karamana</a>
<h4>Crew</h4> 🎬 Director: <a href="/tag/Jahangir_Ummar">Jahangir Ummar</a>
<h3>⚡ Quick Stats</h3>
<div><span class="text-gray-600">Total Gross:</span> <span class="font-bold">₹0.02 Cr</span></div>
<div><span>India Net:</span> <span>₹1.25 Cr</span></div>
<div><span>Overseas:</span> <span>₹15 Lakh</span></div>
<div><span>Budget:</span> <span>N/A</span></div>
<h3>💰 Total Collections Summary</h3>
<div>India Gross</div><div>₹1.60 Cr</div><div>Worldwide</div><div>₹1.75 Cr</div>
<ol class="dw-days">
<li><div class="dw-day" data-day="1"><span class="dw-d">Day 1</span><span class="dw-dt">30 Dec 2026</span><span class="dw-net">₹0.80 Cr</span><span class="dw-v">📉</span></div></li>
<li><div class="dw-day" data-day="2"><span class="dw-d">Day 2</span><span class="dw-dt">31 Dec</span><span class="dw-net">₹0.30 Cr</span></div></li>
<li><div class="dw-day" data-day="3"><span class="dw-d">Day 3</span><span class="dw-dt">1 Jan</span><span class="dw-net">₹15 Lakh</span></div></li>
</ol></body></html>"""

INDEX = """<a href="/movie/Comrade_Kalyan_2026"></a>
<a href="/movie/Comrade_Kalyan_2026"><span>Comrade Kalyan (Tollywood)</span></a>
<a href="/movie/Comrade_Kalyan_2026">Details</a>
<a href="https://www.sacnilk.com/movie/Other_Mommy_2026">Other Mommy (Hollywood)</a>"""


def test_parse_page_figures_and_people():
    f = sk.parse_sacnilk_page(PAGE, year_hint=2026)
    assert f.india_net_inr == 1.25e7
    assert f.india_gross_inr == 1.6e7  # 'India Gross' wins over 'Total Gross'
    assert f.overseas_inr == 1.5e6
    assert f.worldwide_inr == 1.75e7
    assert f.release_date == date(2026, 10, 9)
    assert f.people == ["Nisthar Ahammed", "Sudheer Karamana", "Jahangir Ummar"]


def test_parse_page_daywise_rolls_the_year():
    f = sk.parse_sacnilk_page(PAGE, year_hint=2026)
    assert f.daily_net_inr == [
        (date(2026, 12, 30), 8e6), (date(2026, 12, 31), 3e6), (date(2027, 1, 1), 1.5e6),
    ]


def test_money_and_slug_helpers():
    assert sk.money_inr("12.5", "Cr") == 1.25e8
    assert sk.money_inr("1,200", "K") == 1.2e6
    assert sk.slug_guess("Drishyam 3: The Conclusion", 2026) == "Drishyam_3_The_Conclusion_2026"
    assert sk.discover_slugs(INDEX) == {"comrade kalyan": "Comrade_Kalyan_2026", "other mommy": "Other_Mommy_2026"}


def test_verify_release_and_credits():
    f = sk.parse_sacnilk_page(PAGE, year_hint=2026)
    movie = {"primary_release_date": date(2026, 10, 9)}
    assert sk.verify(movie, f, ["Nisthar Ahammed", "Someone"]) is None
    assert sk.verify(movie, f, ["Mohanlal"]) == "no_credit_match"
    assert sk.verify({"primary_release_date": date(2026, 3, 1)}, f, ["Nisthar Ahammed"]).startswith("release_mismatch")
    assert sk.verify(movie, f, None) is None  # no credits known: the date alone decides
    assert sk.verify(movie, sk.SacnilkFigures(), None) == "unverifiable"


class FakeClient:
    def __init__(self, pages: dict[str, str | None], allowed=True, block_after=None):
        self.pages, self._allowed, self.block_after = pages, allowed, block_after
        self.requests: list[str] = []

    def allowed(self):
        return self._allowed

    def get(self, path):
        self.requests.append(path)
        if self.block_after is not None and len(self.requests) > self.block_after:
            raise sk.SacnilkBlocked("HTTP 403")
        return self.pages.get(path)

    def close(self):
        pass


def _bq(cands, credits_rows):
    bq = MagicMock()
    bq.query.side_effect = [cands, credits_rows]
    return bq


def test_run_sacnilk_matches_and_writes_rows():
    cands = [{"tmdb_id": 1, "title": "Dhanushkodiyilekku", "original_title": "ധനുഷ്കോടിയിലേക്ക്",
              "primary_release_date": date(2026, 10, 9), "popularity": 5, "slug": None, "status": None}]
    bq = _bq(cands, [{"tmdb_id": 1, "name": "Jahangir Ummar"}])
    client = FakeClient({"/latest-boxoffice-collection": INDEX, "/entertainmenttopbar/Recent_Movies": None,
                         "/movie/Dhanushkodiyilekku_2026": PAGE})
    fetched, written = sk.run_sacnilk(bq=bq, settings=Settings(GCP_PROJECT="p"), client=client, today=date(2026, 10, 11))  # type: ignore[call-arg]
    assert (fetched, written) == (1, 7)  # 4 cumulative + 3 daily
    pages = bq.merge_rows.call_args_list[0].kwargs["rows"]
    assert pages[0].status == "matched" and pages[0].slug == "Dhanushkodiyilekku_2026"
    rows = bq.merge_rows.call_args_list[1].kwargs["rows"]
    assert {(r.market, r.metric, r.cumulative) for r in rows} >= {
        ("IN", "india_net_inr", True), ("WW", "worldwide_gross_inr", True), ("IN", "net_inr_daily", False)}
    assert client.requests[:2] == ["/latest-boxoffice-collection", "/entertainmenttopbar/Recent_Movies"]


def test_run_sacnilk_unmatched_and_not_found_are_cached_not_written():
    cands = [{"tmdb_id": 1, "title": "Dhanushkodiyilekku", "original_title": None,
              "primary_release_date": date(2026, 10, 9), "popularity": 5, "slug": None, "status": None},
             {"tmdb_id": 2, "title": "Ghost Film", "original_title": None,
              "primary_release_date": date(2026, 10, 1), "popularity": 4, "slug": None, "status": None}]
    bq = _bq(cands, [{"tmdb_id": 1, "name": "Mohanlal"}])  # wrong star → unmatched
    client = FakeClient({"/latest-boxoffice-collection": INDEX, "/movie/Dhanushkodiyilekku_2026": PAGE})
    fetched, written = sk.run_sacnilk(bq=bq, settings=Settings(GCP_PROJECT="p"), client=client, today=date(2026, 10, 11))  # type: ignore[call-arg]
    assert (fetched, written) == (2, 0)
    pages = {r.tmdb_id: (r.status, r.note) for r in bq.merge_rows.call_args.kwargs["rows"]}
    assert pages == {1: ("unmatched", "no_credit_match"), 2: ("not_found", None)}


def test_run_sacnilk_stops_when_blocked_or_disallowed():
    cands = [{"tmdb_id": i, "title": f"Film {i}", "original_title": None, "primary_release_date": date(2026, 10, 1),
              "popularity": 1, "slug": None, "status": None} for i in range(3)]
    bq = _bq(cands, [])
    client = FakeClient({"/latest-boxoffice-collection": INDEX}, block_after=2)
    fetched, written = sk.run_sacnilk(bq=bq, settings=Settings(GCP_PROJECT="p"), client=client, today=date(2026, 10, 11))  # type: ignore[call-arg]
    assert written == 0 and len(client.requests) == 3  # index ×2 + first page → blocked, no hammering
    bq2 = _bq(cands, [])
    client2 = FakeClient({}, allowed=False)
    assert sk.run_sacnilk(bq=bq2, settings=Settings(GCP_PROJECT="p"), client=client2, today=date(2026, 10, 11)) == (0, 0)  # type: ignore[call-arg]
    assert client2.requests == []


def test_epoch_release_date_is_treated_as_unknown():
    page = PAGE.replace("09 Oct 2026", "01 Jan 1970")
    f = sk.parse_sacnilk_page(page, year_hint=2026)
    assert f.release_date is None
    assert sk.verify({"primary_release_date": date(2026, 10, 9)}, f, ["Nisthar Ahammed"]) is None


@pytest.mark.parametrize("s,expected", [("9 Oct 2026", date(2026, 10, 9)), ("10 Oct", date(2026, 10, 10)), ("nope", None)])
def test_parse_day(s, expected):
    assert sk.parse_day(s, year_hint=2026) == expected
