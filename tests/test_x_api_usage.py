from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest

from modules.database import Database
from modules.x_api_usage import XApiUsageMeter
from modules.twitter_client import (
    TwitterClient,
    XPublicationRejected,
    XPublicationUnknown,
)


NOW = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)


def _meter(tmp_path, *, budget=0, rates=None, name="usage.db"):
    database = Database(str(tmp_path / name))
    meter = XApiUsageMeter(
        database,
        monthly_budget_microusd=budget,
        unit_costs_microusd=rates or {"post_read": 5_000},
        clock=lambda: NOW,
    )
    return database, meter


def _twitter_client(stub, meter):
    client = object.__new__(TwitterClient)
    client._client = stub
    client._api = None
    client._usage_meter = meter
    return client


def test_monthly_budget_reservation_is_atomic_and_inclusive(tmp_path):
    database, meter = _meter(tmp_path, budget=10_000)

    first = meter.reserve("post_read", 1)
    assert first is not None
    assert meter.complete(first, 1) is True
    second = meter.reserve("post_read", 1)
    assert second is not None
    assert meter.complete(second, 1) is True

    assert meter.reserve("post_read", 1) is None
    assert database.get_x_api_usage_summary("2026-09") == {
        "period_key": "2026-09",
        "estimated_cost_microusd": 10_000,
        "states": {
            "reserved": 0,
            "completed": 2,
            "failed": 0,
            "unknown": 0,
        },
        "operations": {
            "post_read": {
                "requests": 2,
                "billable_units": 2,
                "estimated_cost_microusd": 10_000,
            },
        },
    }


def test_completion_reconciles_reserved_units_to_actual_units(tmp_path):
    database, meter = _meter(tmp_path, budget=50_000)

    first = meter.reserve("post_read", 10)
    assert first is not None
    assert meter.complete(first, 2) is True
    second = meter.reserve("post_read", 8)

    assert second is not None
    assert database.get_x_api_usage_summary("2026-09")[
        "estimated_cost_microusd"
    ] == 50_000


def test_failed_request_costs_zero_and_unknown_keeps_reservation(tmp_path):
    database, meter = _meter(tmp_path, budget=15_000)

    failed = meter.reserve("post_read", 2)
    assert failed is not None
    assert meter.fail(failed) is True
    unknown = meter.reserve("post_read", 2)
    assert unknown is not None
    assert meter.unknown(unknown) is True

    summary = database.get_x_api_usage_summary("2026-09")
    assert summary["estimated_cost_microusd"] == 10_000
    assert summary["states"] == {
        "reserved": 0,
        "completed": 0,
        "failed": 1,
        "unknown": 1,
    }
    assert meter.reserve("post_read", 1) is not None


def test_zero_budget_records_usage_without_enforcing_a_cap(tmp_path):
    database, meter = _meter(tmp_path, budget=0)

    claim = meter.reserve("post_read", 1_000_000)

    assert claim is not None
    assert meter.complete(claim, 3) is True
    assert database.get_x_api_usage_summary("2026-09")[
        "estimated_cost_microusd"
    ] == 15_000


def test_usage_ledger_survives_database_restart(tmp_path):
    path = str(tmp_path / "restart.db")
    database, meter = _meter(tmp_path, rates={"content_create": 15_000}, name="restart.db")
    claim = meter.reserve("content_create", 1)
    assert claim is not None
    assert meter.complete(claim, 1) is True

    reopened = Database(path)

    assert reopened.get_x_api_usage_summary("2026-09")[
        "operations"
    ]["content_create"] == {
        "requests": 1,
        "billable_units": 1,
        "estimated_cost_microusd": 15_000,
    }


@pytest.mark.parametrize(
    ("budget", "rates"),
    (
        (-1, {"post_read": 5_000}),
        (True, {"post_read": 5_000}),
        (0, {"post read": 5_000}),
        (0, {"post_read": -1}),
        (0, {"post_read": True}),
    ),
)
def test_usage_meter_rejects_invalid_configuration(tmp_path, budget, rates):
    database = Database(str(tmp_path / "invalid.db"))

    with pytest.raises(ValueError):
        XApiUsageMeter(
            database,
            monthly_budget_microusd=budget,
            unit_costs_microusd=rates,
            clock=lambda: NOW,
        )


def test_successful_post_read_records_returned_resources(tmp_path):
    class SearchStub:
        def search_recent_tweets(self, **_kwargs):
            users = [SimpleNamespace(id="10", username="owner")]
            rows = [
                SimpleNamespace(
                    id=str(index),
                    text=f"post {index}",
                    author_id="10",
                    public_metrics={
                        "like_count": 1,
                        "retweet_count": 2,
                        "reply_count": 3,
                    },
                )
                for index in (101, 102)
            ]
            return SimpleNamespace(data=rows, includes={"users": users})

    database, meter = _meter(tmp_path, rates={"post_read": 5_000})
    client = _twitter_client(SearchStub(), meter)

    assert len(client.search_tweets("gym owner", limit=10)) == 2
    assert database.get_x_api_usage_summary("2026-09")["operations"][
        "post_read"
    ] == {
        "requests": 1,
        "billable_units": 2,
        "estimated_cost_microusd": 10_000,
    }


def test_failed_post_read_releases_reserved_estimate(tmp_path):
    class FailingSearchStub:
        def search_recent_tweets(self, **_kwargs):
            raise ValueError("rejected before data")

    database, meter = _meter(tmp_path, rates={"post_read": 5_000})
    client = _twitter_client(FailingSearchStub(), meter)

    assert client.search_tweets("gym owner", limit=10) == []
    summary = database.get_x_api_usage_summary("2026-09")
    assert summary["estimated_cost_microusd"] == 0
    assert summary["states"]["failed"] == 1


def test_successful_post_write_is_metered(tmp_path):
    class WriteStub:
        calls = 0

        def create_tweet(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(data={"id": "8101"})

    database, meter = _meter(tmp_path, rates={"content_create": 15_000})
    stub = WriteStub()
    client = _twitter_client(stub, meter)

    response = client.post_tweet("approved post")

    assert response.data == {"id": "8101"}
    assert stub.calls == 1
    assert database.get_x_api_usage_summary("2026-09")["operations"][
        "content_create"
    ]["estimated_cost_microusd"] == 15_000


def test_post_write_over_budget_is_rejected_before_network(tmp_path):
    class WriteStub:
        calls = 0

        def create_tweet(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(data={"id": "8201"})

    database, meter = _meter(
        tmp_path, budget=10_000, rates={"content_create": 15_000},
    )
    stub = WriteStub()
    client = _twitter_client(stub, meter)

    with pytest.raises(XPublicationRejected, match="budget"):
        client.post_tweet("blocked post")

    assert stub.calls == 0
    assert database.get_x_api_usage_summary("2026-09")["states"] == {
        "reserved": 0,
        "completed": 0,
        "failed": 0,
        "unknown": 0,
    }


def test_ambiguous_post_write_keeps_reserved_estimate_unknown(tmp_path):
    class TimeoutWriteStub:
        def create_tweet(self, **_kwargs):
            raise TimeoutError("response lost")

    database, meter = _meter(tmp_path, rates={"content_create": 15_000})
    client = _twitter_client(TimeoutWriteStub(), meter)

    with pytest.raises(XPublicationUnknown):
        client.post_tweet("possibly accepted post")

    summary = database.get_x_api_usage_summary("2026-09")
    assert summary["estimated_cost_microusd"] == 15_000
    assert summary["states"]["unknown"] == 1


def test_owned_metrics_read_records_only_returned_posts(tmp_path):
    class MetricsStub:
        def get_tweets(self, **_kwargs):
            return SimpleNamespace(data=[
                SimpleNamespace(
                    id="8301",
                    public_metrics={"like_count": 2},
                    non_public_metrics={"impression_count": 20},
                ),
            ])

    database, meter = _meter(tmp_path, rates={"owned_read": 1_000})
    client = _twitter_client(MetricsStub(), meter)

    assert client.get_tweet_metrics(["8301", "8302"]) == {
        "8301": {"like_count": 2, "impression_count": 20},
    }
    assert database.get_x_api_usage_summary("2026-09")["operations"][
        "owned_read"
    ] == {
        "requests": 1,
        "billable_units": 1,
        "estimated_cost_microusd": 1_000,
    }


def test_user_lookup_records_one_returned_user(tmp_path):
    class UserStub:
        def get_user(self, **_kwargs):
            return SimpleNamespace(data=SimpleNamespace(
                id="8401",
                username="gymowner",
                public_metrics={"followers_count": 15},
                verified=False,
            ))

    database, meter = _meter(tmp_path, rates={"user_read": 10_000})
    client = _twitter_client(UserStub(), meter)

    assert client.get_user_info("gymowner")["id"] == "8401"
    assert database.get_x_api_usage_summary("2026-09")["operations"][
        "user_read"
    ]["estimated_cost_microusd"] == 10_000


def test_link_write_budget_is_checked_before_media_upload(tmp_path):
    class MediaStub:
        calls = 0

        def media_upload(self, *_args, **_kwargs):
            self.calls += 1
            return SimpleNamespace(media_id_string="media-1")

    class WriteStub:
        calls = 0

        def create_tweet(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(data={"id": "8501"})

    database, meter = _meter(
        tmp_path,
        budget=100_000,
        rates={"content_create_with_url": 200_000, "media_upload": 5_000},
    )
    media_stub = MediaStub()
    write_stub = WriteStub()
    client = _twitter_client(write_stub, meter)
    client._api = media_stub

    with pytest.raises(XPublicationRejected, match="budget"):
        client.post_tweet(
            "Read https://flexdropin.com/article",
            BytesIO(b"image"),
            "image",
            media_filename="cover.jpg",
        )

    assert media_stub.calls == 0
    assert write_stub.calls == 0
    assert database.get_x_api_usage_summary("2026-09")["operations"] == {}


def test_successful_media_post_records_media_and_content_requests(tmp_path):
    class MediaStub:
        def media_upload(self, *_args, **_kwargs):
            return SimpleNamespace(media_id_string="media-2")

    class WriteStub:
        def create_tweet(self, **_kwargs):
            return SimpleNamespace(data={"id": "8601"})

    database, meter = _meter(
        tmp_path,
        rates={"content_create": 15_000, "media_upload": 5_000},
    )
    client = _twitter_client(WriteStub(), meter)
    client._api = MediaStub()

    client.post_tweet(
        "Approved image post",
        BytesIO(b"image"),
        "image",
        media_filename="cover.jpg",
    )

    operations = database.get_x_api_usage_summary("2026-09")["operations"]
    assert operations["content_create"]["estimated_cost_microusd"] == 15_000
    assert operations["media_upload"]["estimated_cost_microusd"] == 5_000
