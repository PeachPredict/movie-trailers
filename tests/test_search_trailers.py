from datetime import date

from movie_trailers.pipeline.search_trailers import (
    build_query,
    classify_title,
    is_due,
    looks_fan_made,
    looks_like_trailer,
    normalize,
    pick_trailers,
)

DRISHYAM = {"title": "Drishyam 3", "original_title": "ദൃശ്യം 3"}
FAMILY_WANTED = {"title": "Family Wanted", "original_title": "مطلوب عائليا"}
THE_ARAB = {"title": "The Arab", "original_title": "العربي"}


def _item(vid, title, published="2026-05-09T00:00:00Z"):
    return {
        "id": {"videoId": vid},
        "snippet": {
            "title": title,
            "publishedAt": published,
            "channelId": "UC1",
            "channelTitle": "Chan",
        },
    }


def _details(*vids, duration="PT2M30S"):
    return {v: {"id": v, "contentDetails": {"duration": duration}} for v in vids}


def test_normalize_folds_arabic_marks_and_punctuation():
    assert normalize("#مطلوب_عائلياً") == "مطلوب عائليا"
    assert normalize("الإعلان") == "الاعلان"
    assert normalize("Drishyam 3 - Official Trailer | Mohanlal") == (
        "drishyam 3 official trailer mohanlal"
    )
    assert normalize("&quot;فيلم مطلوب عائليا&quot;") == "فيلم مطلوب عائليا"


def test_looks_like_trailer_accepts_official_uploads():
    assert looks_like_trailer(
        DRISHYAM, "Drishyam 3 - Official Trailer | Mohanlal | Jeethu Joseph | Meena"
    )
    assert looks_like_trailer(FAMILY_WANTED, 'الإعلان الرسمي "فيلم مطلوب عائليا" 7 أكتوبر')
    assert looks_like_trailer(FAMILY_WANTED, "الإعلان الرسمي لفيلم #مطلوب_عائلياً يوم 7 أكتوبر")
    assert looks_like_trailer(THE_ARAB, "The Arab | Trailer | SFF26")


def test_looks_like_trailer_rejects_other_movies_and_junk():
    # Sequel/remake with a different title.
    assert not looks_like_trailer(DRISHYAM, "Drishyam: The Conclusion | Official Trailer")
    # About the trailer, not the trailer.
    assert not looks_like_trailer(DRISHYAM, "DRISHYAM 3 Trailer Breakdown: 10 Hidden Clues")
    assert not looks_like_trailer(
        DRISHYAM, "Drishyam 3 - Climax | Drishyam 3 Official Trailer, Release Date &"
    )
    # Title word inside another title must not match ("the arab" vs "arabic").
    assert not looks_like_trailer(THE_ARAB, "Toy Story 5 | Official Trailer (ARABIC DUBBING)")
    # No trailer word.
    assert not looks_like_trailer(DRISHYAM, "Drishyam 3 Pack Up Video")
    # One-word title only as an ordinary word late in another film's title.
    assert not looks_like_trailer(
        THE_ARAB,
        "الإعلان الرسمي لفيلم #مطلوب_عائلياً يوم 7 أكتوبر بجميع دور العرض في مصر و 8 أكتوبر بالوطن العربي",
    )
    assert looks_like_trailer({"title": "Diya", "original_title": "Diya"}, "DIYA | bande annonce")


def test_looks_fan_made_from_description_or_tags():
    assert looks_fan_made(
        {"snippet": {"description": "Jailer 2 – Powerful Trailer Concept | Rajinikanth"}}
    )
    assert looks_fan_made({"snippet": {"description": "#Jailer2 #NotionTrailer #FanMadeTrailer"}})
    assert looks_fan_made(
        {"snippet": {"description": "Copyright Disclaimer Under section 107 ... FAIR USE"}}
    )
    assert looks_fan_made({"snippet": {"description": "", "tags": ["Jailer 2 Fan Trailer"]}})
    assert not looks_fan_made(
        {"snippet": {"description": "UDTA TEER - TRAILER OUT NOW. In cinemas 9th October"}}
    )


def test_pick_trailers_filters_known_unavailable_and_duration_and_caps():
    items = [
        _item("known", "Drishyam 3 - Official Trailer"),
        _item("gone", "Drishyam 3 - Official Trailer (reupload)"),
        _item("long", "Drishyam 3 Trailer"),
        _item("ok1", "Drishyam 3 - Official Teaser"),
        _item("ok2", "Drishyam 3 - Official Trailer | Panorama"),
        _item("ok3", "Drishyam 3 - Trailer"),
    ]
    items.insert(3, _item("fake", "Drishyam 3 - Official Trailer | Mohanlal"))
    details = {
        **_details("known", "ok1", "ok2", "ok3"),
        **_details("long", duration="PT1H2M"),
        "fake": {
            "contentDetails": {"duration": "PT1M1S"},
            "snippet": {"description": "#Drishyam3 #FanMadeTrailer"},
        },
    }
    picks = pick_trailers(DRISHYAM, items, details, known={"known"}, max_matches=2)
    assert [p["youtube_video_id"] for p in picks] == ["ok1", "ok2"]
    assert [p["video_type"] for p in picks] == ["Teaser", "Official Trailer"]


def test_classify_title():
    assert classify_title("Karuppu (Tamil) - Teaser | Suriya") == "Teaser"
    assert classify_title("Cocktail 2 Official Trailer") == "Official Trailer"
    assert classify_title("الإعلان الرسمي لفيلم ريد فلاج") == "Trailer"


def test_is_due_searches_once_per_checkpoint():
    release = date(2026, 11, 1)
    cps = [45, 21, 10, 4]
    assert is_due(None, release, date(2026, 10, 1), cps)
    # Searched 40 days out; no checkpoint crossed until 21 days out.
    assert not is_due(date(2026, 9, 22), release, date(2026, 10, 1), cps)
    assert is_due(date(2026, 9, 22), release, date(2026, 10, 11), cps)
    # Already searched after the 21-day checkpoint → wait for 10.
    assert not is_due(date(2026, 10, 12), release, date(2026, 10, 20), cps)
    assert is_due(date(2026, 10, 12), release, date(2026, 10, 22), cps)
    # Same-day rerun never re-searches.
    assert not is_due(date(2026, 10, 22), release, date(2026, 10, 22), cps)


def test_build_query_dedupes_identical_titles():
    assert build_query({"title": "City Exterminator", "original_title": "City Exterminator"}) == (
        "City Exterminator trailer"
    )
    assert build_query(FAMILY_WANTED) == "مطلوب عائليا Family Wanted trailer"


def test_pick_trailers_rejects_whole_channel_of_a_flagged_fake():
    fake_ch = {"channelId": "UCfake"}
    items = [
        {"id": {"videoId": "clean"}, "snippet": {**fake_ch, "title": "Jailer 2 Release Trailer"}},
        {"id": {"videoId": "flag"}, "snippet": {**fake_ch, "title": "Jailer 2 Trailer 4K"}},
        _item("real", "Jailer 2 - Official Trailer | Sun Pictures"),
    ]
    details = {
        "clean": {"contentDetails": {"duration": "PT2M"}, "snippet": {**fake_ch}},
        "flag": {
            "contentDetails": {"duration": "PT3M"},
            "snippet": {**fake_ch, "description": "Jailer 2 – Powerful Trailer Concept"},
        },
        **_details("real"),
    }
    movie = {"title": "Jailer 2", "original_title": "ஜெயிலர் 2"}
    picks = pick_trailers(movie, items, details, known=set(), max_matches=2)
    assert [p["youtube_video_id"] for p in picks] == ["real"]
