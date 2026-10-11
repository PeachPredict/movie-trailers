"""Wikipedia infobox money parsing + the phase's row shaping."""
from datetime import date
from unittest.mock import MagicMock

from movie_trailers.clients.wikipedia import infobox_field, parse_infobox_money, parse_money
from movie_trailers.config import Settings
from movie_trailers.pipeline.wikipedia_gross import run_wikipedia_gross

PEDDI = """{{Infobox film
| name           = Peddi
| budget         = {{INR|300-350}} crore{{efn|Attributed to multiple sources<ref>{{cite web |title=x |url=https://e.com}}</ref>}}
| gross          = {{INR|330–400}} crore{{efn|name=peddi|''Peddi''{{'}}s reported worldwide grosses vary}}<ref name=a/>
}}
"""


def test_parse_money_indian_formats():
    assert parse_money("{{INR|330–400}} crore") == (3.65e9, "INR")
    assert parse_money("{{INR|143.60}} crore<ref name=\"BO\">{{Cite web |title=x}}</ref>") == (1.436e9, "INR")
    assert parse_money("{{estimation}} ₹320–325 crore{{efn|name=j|varies}}") == (3.225e9, "INR")
    assert parse_money("{{INRConvert|143.60|c}}") == (1.436e9, "INR")
    assert parse_money("{{INRConvert|50|l}}") == (5e6, "INR")
    assert parse_money("Rs. 12 lakh") == (1.2e6, "INR")


def test_parse_money_dollar_formats():
    assert parse_money("{{US$|12.3 million}}") == (1.23e7, "USD")
    assert parse_money("$389.8 million<ref>x</ref>") == (3.898e8, "USD")
    assert parse_money("US$1.2 billion") == (1.2e9, "USD")
    assert parse_money("{{USD|45,000,000}}") == (4.5e7, "USD")
    assert parse_money("[[Box Office Mojo|$1.5 million]]") == (1.5e6, "USD")


def test_parse_money_rejects_unknown():
    assert parse_money("TBA") is None
    assert parse_money("") is None


def test_infobox_field_and_full_parse():
    assert infobox_field(PEDDI, "name") == "Peddi"
    assert parse_infobox_money(PEDDI, "gross") == (3.65e9, "INR")
    assert parse_infobox_money(PEDDI, "budget") == (3.25e9, "INR")
    assert parse_infobox_money(PEDDI, "runtime") is None


def test_run_wikipedia_gross_resolves_and_writes():
    bq = MagicMock()
    bq.query.return_value = [
        {"tmdb_id": 1, "imdb_id": "tt1", "title": "Peddi", "popularity": 9, "enwiki_title": None},
        {"tmdb_id": 2, "imdb_id": "tt2", "title": "Nope", "popularity": 1, "enwiki_title": None},
        {"tmdb_id": 3, "imdb_id": "tt3", "title": "Gone", "popularity": 1, "enwiki_title": "Gone (film)"},
    ]
    wikidata = MagicMock()
    wikidata.enwiki_titles.return_value = {"tt1": "Peddi"}  # tt2 has no article
    wikipedia = MagicMock()
    wikipedia.wikitext.side_effect = lambda t: {"Peddi": PEDDI, "Gone (film)": None}[t]
    read, written = run_wikipedia_gross(
        bq=bq, settings=Settings(GCP_PROJECT="p"), wikidata=wikidata, wikipedia=wikipedia,  # type: ignore[call-arg]
        today=date(2026, 10, 11),
    )
    assert (read, written) == (2, 1)
    pages = {r.tmdb_id: r.status for r in bq.merge_rows.call_args_list[0].kwargs["rows"]}
    assert pages == {2: "no_article", 1: "ok", 3: "missing"}
    rows = bq.merge_rows.call_args_list[1].kwargs["rows"]
    assert rows[0].source == "wikipedia" and rows[0].metric == "gross_inr" and rows[0].value == 3.65e9
    assert rows[0].budget_usd is None  # INR budget is not stored as budget_usd
