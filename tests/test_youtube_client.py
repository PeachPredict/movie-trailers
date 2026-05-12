import httpx
import pytest
import respx

from movie_trailers.clients.youtube import (
    CommentsDisabledError,
    QuotaExceededError,
    YouTubeClient,
)


@respx.mock
def test_videos_list_counts_quota_and_parses_items():
    respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "abc",
                        "statistics": {"viewCount": "12345", "likeCount": "67"},
                        "snippet": {"channelId": "UCxxx", "channelTitle": "Marvel"},
                    }
                ]
            },
        )
    )
    yt = YouTubeClient(api_key="k")
    items = yt.videos_list(["abc"])
    assert items[0]["id"] == "abc"
    assert yt.quota_units_used == 1


@respx.mock
def test_videos_list_rejects_oversized_batch():
    yt = YouTubeClient(api_key="k")
    with pytest.raises(ValueError):
        yt.videos_list([f"id{i}" for i in range(51)])
    assert yt.quota_units_used == 0


@respx.mock
def test_comment_threads_top_raises_on_comments_disabled():
    respx.get("https://www.googleapis.com/youtube/v3/commentThreads").mock(
        return_value=httpx.Response(
            403,
            json={
                "error": {
                    "errors": [{"reason": "commentsDisabled"}],
                    "code": 403,
                    "message": "...",
                }
            },
        )
    )
    yt = YouTubeClient(api_key="k")
    with pytest.raises(CommentsDisabledError):
        yt.comment_threads_top("vid123", max_results=30)
    # Quota was still consumed on the call.
    assert yt.quota_units_used == 1


@respx.mock
def test_comment_threads_top_raises_on_quota_exceeded():
    respx.get("https://www.googleapis.com/youtube/v3/commentThreads").mock(
        return_value=httpx.Response(
            403,
            json={"error": {"errors": [{"reason": "quotaExceeded"}], "code": 403}},
        )
    )
    yt = YouTubeClient(api_key="k")
    with pytest.raises(QuotaExceededError):
        yt.comment_threads_top("vid123")


@respx.mock
def test_comment_threads_returns_items():
    respx.get("https://www.googleapis.com/youtube/v3/commentThreads").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "c1",
                        "snippet": {
                            "totalReplyCount": 2,
                            "topLevelComment": {
                                "snippet": {
                                    "textDisplay": "Hyped!",
                                    "likeCount": 5,
                                    "authorChannelId": {"value": "UCauthor"},
                                    "authorDisplayName": "fan",
                                    "publishedAt": "2026-04-01T00:00:00Z",
                                    "updatedAt": "2026-04-01T00:00:00Z",
                                }
                            },
                        },
                    }
                ]
            },
        )
    )
    yt = YouTubeClient(api_key="k")
    result = yt.comment_threads_top("vid123", max_results=30)
    assert len(result.items) == 1
    assert yt.quota_units_used == 1
