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
