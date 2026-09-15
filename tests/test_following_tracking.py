import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from modules.database import Database
from modules.growth_candidate_schema import has_managed_fitness_facility_context


NOW = datetime(2026, 9, 15, 21, 15, tzinfo=timezone.utc)
GYM_BIO = "Independent gym in Austin, TX. Classes daily."
OTHER_BIO = "Fitness podcast host and marathon runner."


def profile(user_id, username, description=GYM_BIO):
    return {
        "id": user_id,
        "user_id": user_id,
        "username": username,
        "description": description,
        "protected": False,
        "location": "Austin, TX",
        "created_at": None,
        "followers_count": 100,
        "following_count": 50,
        "tweet_count": 20,
        "listed_count": 1,
        "spam_signals": [],
    }


def capture_followers(db, when, profiles):
    observed_on = when.astimezone(ZoneInfo("Europe/Rome")).date().isoformat()
    assert db.capture_follower_snapshot_batch(
        observed_on, when, [(item, False) for item in profiles], len(profiles),
    ) is not None


def following_row(db, user_id):
    with db._conn() as conn:
        return conn.execute(
            "SELECT * FROM x_following WHERE user_id = ?", (user_id,)
        ).fetchone()


def test_fixture_bios_classify_as_expected():
    assert has_managed_fitness_facility_context(profile("11", "gym_a")) is True
    assert has_managed_fitness_facility_context(
        profile("12", "runner", OTHER_BIO)
    ) is False


def test_complete_sync_inserts_rows_with_gym_flag_and_follow_back(tmp_path):
    db = Database(str(tmp_path / "following.db"))
    capture_followers(db, NOW - timedelta(hours=1), [profile("11", "gym_a")])

    summary = db.sync_following_snapshot(
        NOW,
        [profile("11", "gym_a"), profile("12", "runner", OTHER_BIO)],
        complete=True,
    )

    assert summary == {
        "following_total": 2,
        "new_following": 2,
        "unfollowed": 0,
        "gyms_following": 1,
        "still_followed_after_unfollow": [],
    }
    gym = following_row(db, "11")
    runner = following_row(db, "12")
    assert (gym["is_gym"], gym["follows_back"]) == (1, 1)
    assert gym["first_seen_following_at"] == NOW.isoformat()
    assert gym["follows_back_checked_at"] == (NOW - timedelta(hours=1)).isoformat()
    assert (runner["is_gym"], runner["follows_back"]) == (0, 0)
    assert db.get_following_state() == {
        "11": {"username": "gym_a", "is_gym": True},
        "12": {"username": "runner", "is_gym": False},
    }


def test_incomplete_sync_writes_nothing(tmp_path):
    db = Database(str(tmp_path / "incomplete.db"))

    assert db.sync_following_snapshot(
        NOW, [profile("11", "gym_a")], complete=False,
    ) is None
    with db._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM x_following").fetchone()[0] == 0


def test_follow_back_unchanged_without_complete_follower_run(tmp_path):
    db = Database(str(tmp_path / "no-run.db"))

    db.sync_following_snapshot(NOW, [profile("11", "gym_a")], complete=True)

    row = following_row(db, "11")
    assert (row["follows_back"], row["follows_back_checked_at"]) == (0, None)


def test_missing_account_is_marked_unfollowed_and_refollow_resets(tmp_path):
    db = Database(str(tmp_path / "diff.db"))
    runner = profile("12", "runner", OTHER_BIO)
    db.sync_following_snapshot(
        NOW, [profile("11", "gym_a"), runner], complete=True,
    )

    later = NOW + timedelta(days=1)
    summary = db.sync_following_snapshot(
        later, [profile("11", "gym_a")], complete=True,
    )

    assert summary["unfollowed"] == 1
    assert following_row(db, "12")["unfollowed_at"] == later.isoformat()
    assert set(db.get_following_state()) == {"11"}

    again = NOW + timedelta(days=2)
    summary = db.sync_following_snapshot(
        again, [profile("11", "gym_a"), runner], complete=True,
    )

    assert summary["new_following"] == 1
    row = following_row(db, "12")
    assert row["unfollowed_at"] is None
    assert row["first_seen_following_at"] == again.isoformat()


def test_legacy_candidate_becomes_followed_when_sync_sees_it(tmp_path):
    db = Database(str(tmp_path / "legacy.db"))
    db.upsert_growth_candidate({
        "user_id": "11",
        "username": "gym_a",
        "profile": {},
        "latest_post": None,
        "score": 88,
        "score_data": {},
        "discovery_source": "topic_search",
    })

    db.sync_following_snapshot(NOW, [profile("11", "gym_a")], complete=True)

    with db._conn() as conn:
        candidate = conn.execute(
            "SELECT decision, decision_at, manual_followed_at "
            "FROM growth_candidates WHERE user_id = '11'"
        ).fetchone()
    assert tuple(candidate) == (
        "followed_manually", NOW.isoformat(), NOW.isoformat(),
    )


def test_manual_unfollow_still_listed_is_reported_and_cleared(tmp_path):
    db = Database(str(tmp_path / "still-followed.db"))
    runner = profile("12", "runner", OTHER_BIO)
    db.sync_following_snapshot(NOW, [runner], complete=True)
    with db._conn() as conn:
        conn.execute(
            "UPDATE x_following SET unfollow_decision = 'unfollowed_manually', "
            "unfollow_decision_at = ? WHERE user_id = '12'",
            (NOW.isoformat(),),
        )

    summary = db.sync_following_snapshot(
        NOW + timedelta(days=1), [runner], complete=True,
    )

    assert summary["still_followed_after_unfollow"] == ["runner"]
    row = following_row(db, "12")
    assert (row["unfollow_decision"], row["unfollow_decision_at"]) == (None, None)
    assert json.loads(row["profile_json"])["username"] == "runner"


from types import SimpleNamespace

from modules.analytics import PerformanceAnalyzer
from modules.telegram_controller import TelegramController
from tests.fakes import FakeTelegramApi


EMPTY_SYNC = {
    "following_total": 0,
    "new_following": 0,
    "unfollowed": 0,
    "gyms_following": 0,
    "still_followed_after_unfollow": [],
}


class FollowingX:
    def __init__(self, following, complete=True):
        self.following = list(following)
        self.complete = complete
        self.reads = 0
        self.write_calls = []

    def read_following_profiles(self):
        self.reads += 1
        return SimpleNamespace(profiles=tuple(self.following), complete=self.complete)


class NoopNotifier:
    def notify_error(self, operation, error):
        del operation, error


def test_analytics_sync_following_persists_complete_reads_only(tmp_path):
    db = Database(str(tmp_path / "analytics-following.db"))
    complete = FollowingX([profile("11", "gym_a")])

    assert PerformanceAnalyzer(complete, db).sync_following(NOW)["following_total"] == 1
    assert complete.reads == 1

    other = Database(str(tmp_path / "analytics-partial.db"))
    partial = FollowingX([profile("11", "gym_a")], complete=False)
    assert PerformanceAnalyzer(partial, other).sync_following(NOW) == EMPTY_SYNC
    assert other.get_following_state() == {}

    legacy_client = SimpleNamespace()
    assert PerformanceAnalyzer(legacy_client, other).sync_following(NOW) == EMPTY_SYNC


def test_following_notices_list_only_safe_usernames(tmp_path):
    db = Database(str(tmp_path / "notices.db"))
    telegram = FakeTelegramApi(tmp_path / "media")
    controller = TelegramController(
        telegram, db, NoopNotifier(), "42", now_fn=lambda: NOW,
    )

    assert controller.push_following_notices(["runner", "bad name!"]) == (
        "following_notices"
    )
    assert "@runner" in telegram.messages[-1][1]
    assert "bad name" not in telegram.messages[-1][1]
    assert controller.push_following_notices([]) == "following_notices_empty"
    assert len(telegram.messages) == 1


def test_follower_cycle_syncs_following_and_pushes_notices(tmp_path):
    from main import FlexDropinGrowthAgent
    from tests.test_end_to_end_dry_run import NOW as AGENT_NOW, dependency_bundle

    class Analytics:
        def __init__(self):
            self.calls = []

        def capture_follower_snapshot(self, current):
            self.calls.append(("followers", current))
            return {"followers_total": 1}

        def timing_samples(self, current):
            del current
            return []

        def sync_following(self, current):
            self.calls.append(("following", current))
            return {**EMPTY_SYNC, "still_followed_after_unfollow": ["runner"]}

    class Controller:
        def __init__(self):
            self.notices = []

        def push_following_notices(self, usernames):
            self.notices.append(list(usernames))
            return "following_notices"

    analytics, controller = Analytics(), Controller()
    agent = FlexDropinGrowthAgent(dependency_bundle(
        tmp_path, analytics=analytics, telegram_controller=controller,
    ))

    assert agent.follower_snapshot_cycle(now=AGENT_NOW) == {"followers_total": 1}
    assert analytics.calls == [("followers", AGENT_NOW), ("following", AGENT_NOW)]
    assert controller.notices == [["runner"]]


import pytest


METRICS = {
    "followers_count": 100, "following_count": 50,
    "tweet_count": 20, "listed_count": 1,
}


def seed_suggestion(db, kind, object_id, username, payload, reasons, *,
                    observed_on="2026-09-15", suggested_at=NOW):
    with db._conn() as conn:
        conn.execute(
            """
            INSERT INTO growth_suggestions (
                observed_on, kind, object_id, username, payload_json, score,
                reason_codes_json, suggested_at, cooldown_until, rank_position
            ) VALUES (?, ?, ?, ?, ?, 50, ?, ?, ?, 0)
            """,
            (
                observed_on, kind, object_id, username, json.dumps(payload),
                json.dumps(reasons), suggested_at.isoformat(),
                (suggested_at + timedelta(days=30)).isoformat(),
            ),
        )
        counts = {"account": 0, "post": 0, "reevaluate": 0}
        counts[kind] = 1
        conn.execute(
            "INSERT INTO growth_digest_runs VALUES (?, ?, ?)",
            (
                observed_on, suggested_at.isoformat(),
                json.dumps({"observed_on": observed_on, "counts": counts}),
            ),
        )
    return db.get_growth_digest(observed_on)


def unfollow_payload(user_id="12", username="runner"):
    reasons = ["no_follow_back_after_30_days"]
    return {
        "user_id": user_id,
        "username": username,
        "public_metrics": dict(METRICS),
        "followed_since": (NOW - timedelta(days=31)).isoformat(),
        "reason_codes": reasons,
    }, reasons


def post_payload(post_id="7001", author_id="200", username="gym_a",
                 reasons=("followed_gym",), created_at=None):
    reasons = list(reasons)
    return {
        "id": post_id,
        "author_id": author_id,
        "author_username": username,
        "excerpt": "New class schedule at our gym.",
        "created_at": (created_at or NOW - timedelta(hours=3)).isoformat(),
        "public_metrics": {
            "like_count": 1, "retweet_count": 0, "reply_count": 0,
            "quote_count": 0, "impression_count": 10,
        },
        "reason_codes": reasons,
    }, reasons


def account_payload(user_id="101", username="studio_owner"):
    reasons = ["primary_operator_role", "active_within_7_days"]
    return {
        "user_id": user_id,
        "username": username,
        "public_metrics": dict(METRICS),
        "latest_activity_id": "9001",
        "latest_activity_at": (NOW - timedelta(hours=3)).isoformat(),
        "segment": "primary",
        "reason_codes": reasons,
    }, reasons


def follow_for_maturity(db, profiles, followed_at=NOW - timedelta(days=31),
                        followers=()):
    db.sync_following_snapshot(followed_at, profiles, complete=True)
    capture_followers(db, NOW - timedelta(hours=1), list(followers))
    db.sync_following_snapshot(NOW, profiles, complete=True)


def test_unfollow_proposal_requires_maturity_non_gym_and_later_follower_run(tmp_path):
    db = Database(str(tmp_path / "proposals.db"))
    gym = profile("11", "gym_a")
    runner = profile("12", "runner", OTHER_BIO)
    newbie = profile("13", "newbie", OTHER_BIO)
    db.sync_following_snapshot(NOW - timedelta(days=31), [gym, runner], complete=True)
    db.sync_following_snapshot(
        NOW - timedelta(days=10), [gym, runner, newbie], complete=True,
    )
    capture_followers(db, NOW - timedelta(hours=1), [])
    db.sync_following_snapshot(NOW, [gym, runner, newbie], complete=True)

    assert db.get_unfollow_proposals(NOW) == [{
        "user_id": "12",
        "username": "runner",
        "public_metrics": METRICS,
        "followed_since": (NOW - timedelta(days=31)).isoformat(),
        "days_following": 31,
    }]
    assert db.get_unfollow_proposals(NOW, review_days=14) == []


def test_follow_back_excludes_unfollow_proposal(tmp_path):
    db = Database(str(tmp_path / "follow-back.db"))
    runner = profile("12", "runner", OTHER_BIO)
    follow_for_maturity(db, [runner], followers=[runner])

    assert db.get_unfollow_proposals(NOW) == []


def test_weekly_cap_counts_reevaluate_rows_since_rome_monday(tmp_path):
    db = Database(str(tmp_path / "weekly-cap.db"))
    follow_for_maturity(db, [profile("12", "runner", OTHER_BIO)])
    with db._conn() as conn:
        for index in range(5):
            conn.execute(
                """
                INSERT INTO growth_suggestions (
                    observed_on, kind, object_id, username, payload_json,
                    score, reason_codes_json, suggested_at
                ) VALUES ('2026-09-14', 'reevaluate', ?, 'old', '{}', 0, '[]', ?)
                """,
                (str(501 + index), (NOW - timedelta(days=1)).isoformat()),
            )

    assert db.get_unfollow_proposals(NOW) == []
    assert db.get_unfollow_proposals(NOW + timedelta(days=7))[0]["user_id"] == "12"


@pytest.mark.parametrize(
    ("decision", "column", "expected"),
    [
        ("unfollowed_manually", "unfollow_decision", "unfollowed_manually"),
        ("keep", "unfollow_decision", "keep"),
        ("marked_gym", "gym_override", "gym"),
    ],
)
def test_unfollow_decisions_update_following_state(tmp_path, decision, column, expected):
    db = Database(str(tmp_path / f"{decision}.db"))
    follow_for_maturity(db, [profile("12", "runner", OTHER_BIO)])
    payload, reasons = unfollow_payload()
    digest = seed_suggestion(db, "reevaluate", "12", "runner", payload, reasons)
    suggestion = digest["reevaluate"][0]

    assert db.mark_growth_suggestion_decision(
        suggestion["id"], suggestion["revision"], decision, decided_at=NOW,
    ) == "updated"

    row = following_row(db, "12")
    assert row[column] == expected
    later = NOW + timedelta(days=8)
    assert db.get_unfollow_proposals(later) == []
    if decision == "keep":
        assert row["keep_until"] == (NOW + timedelta(days=90)).isoformat()
        assert db.get_unfollow_proposals(NOW + timedelta(days=91))[0]["user_id"] == "12"
    if decision == "marked_gym":
        assert db.get_following_state()["12"]["is_gym"] is True


def test_account_not_relevant_rejects_candidate_for_30_days(tmp_path):
    db = Database(str(tmp_path / "not-relevant.db"))
    db.upsert_growth_candidate({
        "user_id": "101", "username": "studio_owner", "profile": {},
        "latest_post": None, "score": 88, "score_data": {},
        "discovery_source": "topic_search",
    })
    payload, reasons = account_payload()
    suggestion = seed_suggestion(
        db, "account", "101", "studio_owner", payload, reasons,
    )["accounts"][0]

    assert db.mark_growth_suggestion_decision(
        suggestion["id"], suggestion["revision"], "not_relevant", decided_at=NOW,
    ) == "updated"
    with db._conn() as conn:
        candidate = conn.execute(
            "SELECT decision, rejection_reason, suppressed_until "
            "FROM growth_candidates WHERE user_id = '101'"
        ).fetchone()
    assert tuple(candidate) == (
        "rejected", "not_relevant", (NOW + timedelta(days=30)).isoformat(),
    )


@pytest.mark.parametrize("decision", ["liked_manually", "skipped"])
def test_like_decisions_are_local_and_idempotent(tmp_path, decision):
    db = Database(str(tmp_path / f"{decision}.db"))
    payload, reasons = post_payload()
    suggestion = seed_suggestion(db, "post", "7001", "gym_a", payload, reasons)["posts"][0]

    assert db.mark_growth_suggestion_decision(
        suggestion["id"], suggestion["revision"], decision, decided_at=NOW,
    ) == "updated"
    assert db.mark_growth_suggestion_decision(
        suggestion["id"], suggestion["revision"], decision, decided_at=NOW,
    ) == "duplicate"
    assert db.get_growth_digest("2026-09-15")["posts"][0]["decision"] == decision


def test_recent_post_authors_respect_the_since_boundary(tmp_path):
    db = Database(str(tmp_path / "authors.db"))
    suggested_at = NOW - timedelta(days=2)
    payload, reasons = post_payload(created_at=suggested_at - timedelta(hours=3))
    seed_suggestion(
        db, "post", "7001", "gym_a", payload, reasons,
        observed_on="2026-09-13", suggested_at=suggested_at,
    )

    assert db.get_recent_growth_post_authors(NOW - timedelta(days=3)) == {"200"}
    assert db.get_recent_growth_post_authors(NOW - timedelta(days=1)) == set()
