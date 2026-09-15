from types import SimpleNamespace

from modules.twitter_client import TwitterClient


def _client(backend, self_id="1"):
    client = TwitterClient.__new__(TwitterClient)
    client._client = backend
    client._cached_self_id = self_id
    return client


def _user(user_id, username, description="Independent gym in Austin, TX."):
    return SimpleNamespace(
        id=user_id,
        username=username,
        description=description,
        protected=False,
        location="Austin, TX",
        created_at=None,
        public_metrics={
            "followers_count": 10,
            "following_count": 5,
            "tweet_count": 3,
            "listed_count": 0,
        },
    )


class GraphBackend:
    def __init__(self, pages, fail_on=None):
        self.pages = pages
        self.fail_on = fail_on
        self.calls = []

    def get_users_following(self, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls) - 1
        if index == self.fail_on:
            raise RuntimeError("private transport detail")
        users, token = self.pages[index]
        meta = {} if token is None else {"next_token": token}
        return SimpleNamespace(data=users, meta=meta)


def test_following_read_pages_until_the_end_and_reports_complete():
    backend = GraphBackend([
        ([_user(11, "gym_a")], "t1"),
        ([_user("12", "gym_b"), _user(11, "gym_a")], None),
    ])

    result = _client(backend).read_following_profiles()

    assert result.complete is True
    assert [profile["user_id"] for profile in result.profiles] == ["11", "12"]
    assert backend.calls[0] == {
        "id": "1",
        "max_results": 1000,
        "user_fields": [
            "username", "description", "protected", "location",
            "created_at", "public_metrics",
        ],
    }
    assert backend.calls[1]["pagination_token"] == "t1"


def test_following_read_failure_is_incomplete_and_keeps_partial_rows():
    backend = GraphBackend([([_user(11, "gym_a")], "t1")], fail_on=1)

    result = _client(backend).read_following_profiles()

    assert result.complete is False
    assert [profile["user_id"] for profile in result.profiles] == ["11"]


def test_following_read_without_self_id_reads_nothing():
    backend = GraphBackend([])

    result = _client(backend, self_id=None).read_following_profiles()

    assert result.complete is False
    assert backend.calls == []


def test_latest_original_post_keeps_bounded_public_metrics_only_when_complete():
    metrics = {
        "like_count": 3, "retweet_count": 0, "reply_count": 1,
        "quote_count": 0, "impression_count": 90,
    }

    class Backend:
        def __init__(self, public_metrics):
            self.public_metrics = public_metrics

        def get_users_tweets(self, **_kwargs):
            return SimpleNamespace(data=[SimpleNamespace(
                id=501,
                text="New class schedule at our gym",
                created_at="2026-09-14T10:00:00+00:00",
                lang="en",
                public_metrics=self.public_metrics,
            )])

    with_metrics = _client(Backend(metrics)).get_latest_original_post("11")
    without_metrics = _client(Backend({"like_count": 3})).get_latest_original_post("11")

    assert with_metrics["public_metrics"] == metrics
    assert "public_metrics" not in without_metrics
    assert without_metrics["id"] == "501"
