import httpx
import respx

from movie_trailers.clients.tmdb import TMDBClient


@respx.mock
def test_discover_movies_paginates_until_exhausted():
    respx.get("https://api.themoviedb.org/3/discover/movie").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"results": [{"id": 1}, {"id": 2}], "total_pages": 2, "page": 1},
            ),
            httpx.Response(
                200,
                json={"results": [{"id": 3}], "total_pages": 2, "page": 2},
            ),
        ]
    )
    with TMDBClient("k") as tmdb:
        movies = list(
            tmdb.discover_movies("2026-05-11", "2026-11-11", region="US")
        )
    assert [m["id"] for m in movies] == [1, 2, 3]


@respx.mock
def test_movie_videos_returns_results_list():
    respx.get("https://api.themoviedb.org/3/movie/42/videos").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"key": "abc", "site": "YouTube", "type": "Trailer", "name": "Trailer"}
                ]
            },
        )
    )
    with TMDBClient("k") as tmdb:
        videos = tmdb.movie_videos(42)
    assert videos[0]["key"] == "abc"


@respx.mock
def test_tv_season_videos_calls_correct_path():
    route = respx.get(
        "https://api.themoviedb.org/3/tv/100/season/2/videos"
    ).mock(return_value=httpx.Response(200, json={"results": []}))
    with TMDBClient("k") as tmdb:
        videos = tmdb.tv_season_videos(100, 2)
    assert route.called
    assert videos == []
