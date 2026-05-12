from movie_trailers.pipeline._common import classify_video_type, is_trailer_video


def test_is_trailer_video_keeps_teaser_and_trailer():
    assert is_trailer_video({"site": "YouTube", "type": "Trailer"})
    assert is_trailer_video({"site": "YouTube", "type": "Teaser"})


def test_is_trailer_video_skips_clips_and_featurettes():
    assert not is_trailer_video({"site": "YouTube", "type": "Clip"})
    assert not is_trailer_video({"site": "YouTube", "type": "Featurette"})
    assert not is_trailer_video({"site": "YouTube", "type": "Behind the Scenes"})


def test_is_trailer_video_skips_non_youtube():
    assert not is_trailer_video({"site": "Vimeo", "type": "Trailer"})


def test_classify_video_type_promotes_official_trailer_by_name():
    assert (
        classify_video_type({"type": "Trailer", "name": "Avatar 3 | Official Trailer (HD)"})
        == "Official Trailer"
    )


def test_classify_video_type_passthrough():
    assert classify_video_type({"type": "Teaser", "name": "Sneak Peek"}) == "Teaser"
    assert classify_video_type({"type": "Trailer", "name": "First Look"}) == "Trailer"
