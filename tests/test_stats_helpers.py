from movie_trailers.pipeline.stats import _pick_thumbnail


def test_pick_thumbnail_prefers_maxres():
    thumbs = {
        "default": {"url": "https://i.ytimg.com/vi/x/default.jpg"},
        "medium": {"url": "https://i.ytimg.com/vi/x/medium.jpg"},
        "high": {"url": "https://i.ytimg.com/vi/x/high.jpg"},
        "standard": {"url": "https://i.ytimg.com/vi/x/standard.jpg"},
        "maxres": {"url": "https://i.ytimg.com/vi/x/maxres.jpg"},
    }
    assert _pick_thumbnail(thumbs) == "https://i.ytimg.com/vi/x/maxres.jpg"


def test_pick_thumbnail_falls_back():
    thumbs = {
        "default": {"url": "https://i.ytimg.com/vi/x/default.jpg"},
        "medium": {"url": "https://i.ytimg.com/vi/x/medium.jpg"},
    }
    assert _pick_thumbnail(thumbs) == "https://i.ytimg.com/vi/x/medium.jpg"


def test_pick_thumbnail_none_for_empty():
    assert _pick_thumbnail(None) is None
    assert _pick_thumbnail({}) is None
