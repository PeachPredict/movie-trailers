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


# --- 2026-10 precision audit: every example below was accepted by the old filter ------

from movie_trailers.pipeline.search_trailers import evaluate_candidate, matched_title  # noqa: E402

REL = date(2026, 10, 30)


def _movie(title, original=None, lang="hi", **kw):
    return {"title": title, "original_title": original or title, "original_language": lang,
            "primary_release_date": REL, **kw}


def _ok(movie, title, **kw):
    kw.setdefault("duration_s", 120)
    return evaluate_candidate(movie, title=title, **kw)


def test_normalize_strips_latin_accents_and_keeps_hangul():
    assert normalize("Pokémon Tráiler oficial") == "pokemon trailer oficial"
    assert normalize("예고편 公開") == "예고편 公開"


def test_title_match_accepts_compact_spelling():
    assert matched_title({"title": "Jailer 2", "original_title": "ஜெயிலர் 2"}, "Jailer2 Trailer") == "jailer 2"


def test_rejects_tv_seasons_games_and_updates():
    assert _ok(_movie("XO", lang="mr"), "XO, Kitty: Season 3 | Official Trailer | Netflix").startswith("junk_word:season")
    assert _ok(_movie("Little Things", lang="ar"), "LITTLE THINGS SEASON 2 | Official Trailer | Kobby Studios").startswith("junk_word")
    assert _ok(_movie("Party Animals"), "Party Animals - 3 Year Anniversary Trailer") == "junk_word:anniversary"
    assert _ok(_movie("Party Animals"), "Party Animals - Summer Update Trailer") == "junk_word:update"
    assert _ok(_movie("Black Gold", lang="ta"), "Black Gold: Oil Drilling Simulator – Master Deep Drilling Mechanics Trailer") == "junk_word:simulator"
    assert _ok(_movie("GOR"), "Marvel Rivals Season 10 Trailer GOR The God Butcher") == "junk_word:season"


def test_rejects_off_year_and_topic_channels():
    # A year one off is tolerated (trailers drop the year before); two off is not.
    assert _ok(_movie("Little Things", lang="ar"),
               '"The Little Things That Run the World" (2025) Trailer #2',
               credits=["Someone Real"]) == "short_title_unconfirmed"
    assert _ok(_movie("Little Things", lang="ar"),
               '"The Little Things That Run the World" (2024) Trailer #2') == "off_year:2024"
    assert _ok(_movie("The Train", lang="en"), "THE TRAIN (2024) | Official Trailer") == "off_year:2024"
    # A year that is part of the movie's own name is not an off-year.
    assert _ok(_movie("Blade Runner 2049", lang="en"), "Blade Runner 2049 Official Trailer") is None
    assert _ok(_movie("My Dear Stranger", lang="te"), "My Dear Stranger (Trailer Theme)",
               channel_title="Rishi M - Topic") == "junk_word:theme"
    assert _ok(_movie("My Dear Stranger", lang="te"), "My Dear Stranger (Trailer)",
               channel_title="Rishi M - Topic") == "junk_channel:topic"
    assert _ok(_movie("Sathyavan Savithiri", lang="ta"), "Sathyavan Savithiri Trailer Tamil | Keerthy Suresh",
               channel_title="Bolly Edits") == "junk_channel:edits"


def test_short_titles_need_a_credit_or_a_year():
    cast = ["Jackie Shroff", "Sharad Kelkar", "Neha Sargam"]
    assert _ok(_movie("Kaali Kheti"), "Kaali Kheti - Trailer | Jackie Shroff | Sharad Kelkar | 2026",
               credits=cast) is None
    # Same-name film from elsewhere: no credit matches → rejected.
    assert _ok(_movie("Jenny", lang="ml"), "JENNY - EN HISTORIE OM LYDMOR (Trailer) - Biografpremiere 16. april 2026",
               credits=["Naslen", "Amal Neerad"]) == "short_title_unconfirmed"
    assert _ok(_movie("To the North", lang="ar"), "Arknights: Endfield Preview Trailer | Back to the North",
               credits=["Someone Real"]) == "short_title_unconfirmed"
    # Credit in the description counts.
    assert _ok(_movie("The Train", lang="en"), "THE TRAIN (2026) | Official Trailer",
               description="Starring Dennis Dewall and Madalina Bellariu Ion",
               credits=["Dennis Dewall"]) is None
    # No credits known: the year or "official" in the title stands in.
    assert _ok(_movie("Diya", lang="ar"), "DIYA | bande annonce") == "short_title_no_credits"
    assert _ok(_movie("Diya", lang="ar"), "DIYA | bande annonce 2026") is None
    assert _ok(_movie("Diya", lang="ar"), "DIYA | Official Trailer") is None
    # Long titles never need confirmation.
    assert _ok(_movie("Bachelor Party D'eux", lang="ml"), "Bachelor Party D'eux - Trailer | Amal Neerad | Naslen") is None
    # Three tokens without a stopword is not "short".
    assert _ok(_movie("Maharashtra Epstein Files", lang="mr"), "Maharashtra Epstein Files Trailer | चित्रपट",
               credits=["Deepali Sayed"]) is None
    # A production company in the channel or description confirms too.
    assert _ok(_movie("Only Me", production_companies=[{"id": 1, "name": "Lionsgate Play"}]),
               "Only Me (Official Trailer)", channel_title="Lionsgate Play", credits=["Nobody Here"]) is None
    # Same title, different film: credits exist but none match → rejected.
    assert _ok(_movie("The Train", lang="ta"), "THE TRAIN (2026) | Official Trailer | Dennis Dewall",
               credits=["Vikash Gopi", "Nithya Lakshmi"]) == "short_title_unconfirmed"


def test_non_latin_short_titles_accept_an_official_marker():
    fw = _movie("Family Wanted", original="مطلوب عائليا", lang="ar")
    cast = ["Mostafa Khater", "Sara Barakah"]  # transliterated: never in an Arabic upload
    assert _ok(fw, 'الإعلان الرسمي "فيلم مطلوب عائليا" 7 أكتوبر في السينمات', credits=cast) is None
    assert _ok(fw, "matloub 3a2eleyain / مطلوب عائليا | Official Trailer", credits=cast) is None
    assert _ok(fw, "مطلوب عائليا | إعلان", credits=cast) == "short_title_unconfirmed"
    # The Latin title of the same film gets no such relaxation.
    assert _ok(fw, "Family Wanted | Official Trailer", credits=cast) == "short_title_unconfirmed"


def test_documentary_junk_only_for_non_documentaries():
    assert _ok(_movie("Litmus", lang="el"), "Litmus | Documentary Trailer 2026") == "junk_word:documentary"
    doc = _movie("Litmus", lang="el", genres=[{"id": 99, "name": "Documentary"}])
    assert _ok(doc, "Litmus | Documentary Trailer 2026") is None


def test_pick_trailers_prefers_confirmed_and_logs_rejections():
    movie = _movie("Bachelor Party D'eux", lang="ml")  # long title: no confirmation needed
    items = [
        _item("unconf", "Bachelor Party D'eux | Official Trailer | 2026"),
        _item("conf", "Bachelor Party D'eux - Trailer | Amal Neerad | Naslen"),
        _item("junk", "Bachelor Party D'eux Trailer Reaction"),
    ]
    details = _details("unconf", "conf", "junk")
    rejected = []
    picks = pick_trailers(movie, items, details, known=set(), max_matches=1,
                          credits=["Naslen"], rejected=rejected)
    assert [p["youtube_video_id"] for p in picks] == ["conf"]
    assert {r["video_id"]: r["reason"] for r in rejected} == {
        "unconf": "over_max_matches", "junk": "junk_word:reaction",
    }


def test_trusted_channel_confirms_short_titles():
    trusted = ["prime video", "netflix"]
    drive = _movie("Drive - The Pretenders", lang="de")
    assert _ok(drive, "Drive - The Pretenders Official Teaser | Prime Video India",
               channel_title="Prime Video India", credits=["Nobody Here"]) == "short_title_unconfirmed"
    assert _ok(drive, "Drive - The Pretenders Official Teaser | Prime Video India",
               channel_title="Prime Video India", credits=["Nobody Here"], trusted_channels=trusted) is None
    # No credits known either: a trusted channel stands in for the year/official rule too.
    assert _ok(_movie("Dark Horse", lang="pt"), "Veja o trailer do filme Dark Horse",
               channel_title="Netflix Brasil", trusted_channels=trusted) is None
    # The season rule still catches the XO Kitty case even from Netflix.
    assert _ok(_movie("XO", lang="mr"), "XO, Kitty: Season 3 | Official Trailer | Netflix",
               channel_title="Netflix", trusted_channels=trusted) == "junk_word:season"
    # An unknown channel gets no credit.
    assert _ok(drive, "Drive - The Pretenders Official Teaser", channel_title="Random Uploads",
               credits=["Nobody Here"], trusted_channels=trusted) == "short_title_unconfirmed"
