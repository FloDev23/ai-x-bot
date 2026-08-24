import json
import threading
from datetime import datetime, timedelta, timezone

from modules.adaptive_timing import AdaptiveTimingPolicy, TimingSample
from modules.analytics import PerformanceAnalyzer
from modules.database import Database
from modules.media_processor import MediaProcessor


NOW = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)


def _policy():
    return AdaptiveTimingPolicy(
        audience_timezone="America/New_York",
        morning_window="08:30-11:30",
        evening_window="16:30-20:30",
        minimum_gap_hours=6,
        timing_min_posts=30,
        weekday_min_posts=90,
    )


def _insert_post_and_metrics(
    db,
    *,
    tweet_id="1001",
    posted_at=NOW - timedelta(hours=48),
    measured_at=NOW - timedelta(hours=24),
    impressions=100,
    likes=5,
    retweets=2,
    replies=1,
    bookmarks=1,
):
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO posted_tweets "
            "(tweet_id,text,category,has_link,created_at) VALUES (?,?,?,?,?)",
            (tweet_id, "private tweet body", "gym_strategy", 0, posted_at.isoformat()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO tweet_metrics "
            "(tweet_id,impressions,likes,retweets,replies,bookmarks,checked_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                tweet_id,
                impressions,
                likes,
                retweets,
                replies,
                bookmarks,
                measured_at.isoformat(),
            ),
        )


def test_timing_samples_project_only_mature_allowlisted_metrics(tmp_path):
    db = Database(str(tmp_path / "timing.db"))
    _insert_post_and_metrics(db)

    samples = db.get_publication_timing_samples(NOW, min_age_hours=24)

    assert samples == [TimingSample(
        scheduled_for=NOW - timedelta(hours=48),
        measured_at=NOW - timedelta(hours=24),
        impressions=100,
        engagements=9,
    )]
    assert not hasattr(samples[0], "text")


def test_timing_samples_reject_boundary_corruption_and_duplicates(tmp_path):
    db = Database(str(tmp_path / "timing-invalid.db"))
    cases = (
        ("", NOW - timedelta(hours=48), NOW - timedelta(hours=24), 100, 1),
        ("01", NOW - timedelta(hours=48), NOW - timedelta(hours=24), 100, 1),
        ("1002", NOW - timedelta(hours=47), NOW - timedelta(hours=24), 1, 2),
        ("1003", NOW - timedelta(hours=48), NOW + timedelta(seconds=1), 100, 1),
        ("1004", NOW - timedelta(hours=48), NOW - timedelta(hours=25), -1, 0),
    )
    for tweet_id, posted, measured, impressions, likes in cases:
        _insert_post_and_metrics(
            db,
            tweet_id=tweet_id,
            posted_at=posted,
            measured_at=measured,
            impressions=impressions,
            likes=likes,
            retweets=0,
            replies=0,
            bookmarks=0,
        )
    _insert_post_and_metrics(db, tweet_id="2000")
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO posted_tweets "
            "(tweet_id,text,category,has_link,created_at) VALUES (?,?,?,?,?)",
            (
                "2000",
                "duplicate",
                "proof",
                0,
                (NOW - timedelta(hours=48)).isoformat(),
            ),
        )

    assert db.get_publication_timing_samples(NOW, min_age_hours=24) == []
    assert db.get_publication_timing_samples(NOW, min_age_hours=True) == []


def test_performance_analyzer_exposes_same_timing_samples(tmp_path):
    db = Database(str(tmp_path / "analyzer-timing.db"))
    _insert_post_and_metrics(db)
    analyzer = PerformanceAnalyzer(object(), db)

    assert analyzer.timing_samples(NOW) == db.get_publication_timing_samples(NOW)


def test_saved_owned_metrics_use_aware_clock_and_reject_bool_values(tmp_path):
    db = Database(str(tmp_path / "saved-metrics.db"))
    current = datetime.now(timezone.utc)
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO posted_tweets "
            "(tweet_id,text,category,has_link,created_at) VALUES (?,?,?,?,?)",
            (
                "3000",
                "private body",
                "proof",
                0,
                (current - timedelta(hours=48)).isoformat(),
            ),
        )

    assert db.save_tweet_metrics("3000", 100, 5, 2, 1, 1) is True
    assert db.save_tweet_metrics("3001", True, 0, 0, 0, 0) is False
    samples = db.get_publication_timing_samples(
        current + timedelta(seconds=1),
    )
    assert len(samples) == 1
    assert samples[0].engagements == 9


def _approved_queue_draft(
    db,
    *,
    text,
    category,
    score,
    expiry=None,
    media_id=None,
):
    source_id = db.add_content_source(
        "evergreen_idea",
        f"Grounded source for {category}",
    )
    if expiry is not None:
        with db._conn() as conn:
            conn.execute(
                "UPDATE content_sources SET expires_at = ? WHERE id = ?",
                (expiry.isoformat(), source_id),
            )
    draft_id = db.create_post_draft(
        text,
        category,
        [source_id],
        {"total": score},
        (NOW + timedelta(days=30, microseconds=draft_id_seed(text))).isoformat(),
        f"adaptive:{text}",
    )
    queued = db.ensure_editorial_queue(draft_id)
    if media_id is not None:
        assert db.attach_media_to_draft(media_id, draft_id)
        queued = db.get_queue_draft(draft_id)
    assert db.save_review_translation(
        draft_id,
        queued["revision"],
        f"Traduzione italiana privata {draft_id}",
    )
    ready = db.get_queue_draft(draft_id)
    assert db.approve_queued_draft_atomic(
        draft_id,
        ready["revision"],
        ready["queue_revision"],
        "floriano",
        NOW.isoformat(),
    )
    return draft_id


def draft_id_seed(text):
    return sum(text.encode("utf-8")) % 1_000_000


def _planner(db, *, now=NOW, dry_run=True, installation_id="install-1"):
    from modules.publication_queue import PublicationPlanner

    return PublicationPlanner(
        db=db,
        timing_policy=_policy(),
        timing_sample_provider=lambda current: db.get_publication_timing_samples(
            current
        ),
        now_fn=lambda: now,
        audience_timezone="America/New_York",
        installation_id_provider=lambda: installation_id,
        source_expiry_safety_margin=timedelta(hours=2),
        max_links_per_week=1,
        dry_run=dry_run,
    )


def test_ensure_day_is_restart_stable_and_concurrent(tmp_path):
    path = str(tmp_path / "plans.db")
    Database(path)
    barrier = threading.Barrier(2)
    results = []

    def worker():
        planner = _planner(Database(path))
        barrier.wait(timeout=5)
        results.append(planner.ensure_day(NOW))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert [[row["id"] for row in result] for result in results][0] == [
        row["id"] for row in results[1]
    ]
    assert len(results[0]) == 2
    assert all(row["status"] == "open" for row in results[0])
    assert _planner(Database(path)).ensure_day(NOW) == results[0]


def test_ensure_day_reuses_persisted_installation_id_without_provider(tmp_path):
    db = Database(str(tmp_path / "installation-restart.db"))
    first = _planner(db).ensure_day(NOW)

    def unavailable_provider():
        raise RuntimeError("provider raw failure")

    from modules.publication_queue import PublicationPlanner

    restarted = PublicationPlanner(
        db=Database(str(tmp_path / "installation-restart.db")),
        timing_policy=_policy(),
        timing_sample_provider=lambda _current: [],
        now_fn=lambda: NOW,
        audience_timezone="America/New_York",
        installation_id_provider=unavailable_provider,
        source_expiry_safety_margin=timedelta(hours=2),
        max_links_per_week=1,
        dry_run=True,
    )

    assert restarted.ensure_day(NOW) == first


def test_reconcile_prefers_valid_expiring_source_then_category_diversity(tmp_path):
    db = Database(str(tmp_path / "selection.db"))
    urgent_id = _approved_queue_draft(
        db,
        text="Urgent grounded operator insight",
        category="gym_strategy",
        score=80,
        expiry=NOW + timedelta(days=2),
    )
    proof_id = _approved_queue_draft(
        db,
        text="High quality proof without expiring source",
        category="proof",
        score=90,
    )
    duplicate_category_id = _approved_queue_draft(
        db,
        text="Same category alternative",
        category="gym_strategy",
        score=90,
    )
    planner = _planner(db)

    plans = planner.reconcile(NOW)

    assert [plan["draft_id"] for plan in plans] == [urgent_id, proof_id]
    assert duplicate_category_id not in {plan["draft_id"] for plan in plans}
    for plan in plans:
        assert set(plan["selection_reason"]).issubset({
            "source_urgency",
            "score",
            "category_diversity",
            "format_diversity",
            "approval_age",
            "timing_reason",
            "timing_bucket",
        })
        encoded = json.dumps(plan["selection_reason"])
        assert "grounded" not in encoded.lower()
        assert "traduzione" not in encoded.lower()


def test_reconcile_blocks_source_expiring_before_safety_margin(tmp_path):
    db = Database(str(tmp_path / "expiry-block.db"))
    blocked_id = _approved_queue_draft(
        db,
        text="Soon expired source",
        category="gym_strategy",
        score=99,
        expiry=NOW + timedelta(hours=3),
    )
    valid_id = _approved_queue_draft(
        db,
        text="Stable source",
        category="proof",
        score=80,
    )

    plans = _planner(db).reconcile(NOW)

    assert blocked_id not in {plan.get("draft_id") for plan in plans}
    assert valid_id in {plan.get("draft_id") for plan in plans}


def test_reconcile_skips_invalid_media_winner_and_uses_safe_candidate(tmp_path):
    db = Database(str(tmp_path / "invalid-media-selection.db"))
    invalid_id = _approved_queue_draft(
        db,
        text="Invalid media high score",
        category="gym_strategy",
        score=99,
    )
    assert db.transition_post_draft(
        invalid_id,
        ["approved"],
        "approved",
        media_id=999_999,
    )
    valid_id = _approved_queue_draft(
        db,
        text="Valid text fallback",
        category="proof",
        score=80,
    )

    plans = _planner(db).reconcile(NOW)

    assigned = {plan.get("draft_id") for plan in plans}
    assert invalid_id not in assigned
    assert valid_id in assigned


def test_reconcile_filters_revoked_malformed_stale_not_before_and_link_quota(
    tmp_path,
):
    db = Database(str(tmp_path / "selection-gates.db"))
    revoked_id = _approved_queue_draft(
        db, text="Revoked source", category="gym_strategy", score=99,
    )
    revoked = db.get_queue_draft(revoked_id)
    with db._conn() as conn:
        conn.execute(
            "UPDATE content_sources SET trust_state = 'revoked' WHERE id = ?",
            (revoked["source_ids"][0],),
        )
    malformed_id = _approved_queue_draft(
        db, text="Malformed score", category="proof", score=98,
    )
    stale_id = _approved_queue_draft(
        db, text="Stale translation", category="shareable_insight", score=97,
    )
    future_id = _approved_queue_draft(
        db, text="Future not before", category="product_education", score=96,
    )
    link_id = _approved_queue_draft(
        db,
        text="Read https://flexdropin.com/blog/operator-guide",
        category="proof",
        score=95,
    )
    valid_id = _approved_queue_draft(
        db, text="Safe eligible fallback", category="founder_story", score=80,
    )
    with db._conn() as conn:
        conn.execute(
            "UPDATE post_drafts SET score_json = ? WHERE id = ?",
            ('{"total":"98"}', malformed_id),
        )
        conn.execute(
            "UPDATE editorial_queue SET translation_status = 'invalidated' "
            "WHERE draft_id = ?",
            (stale_id,),
        )
        conn.execute(
            "UPDATE editorial_queue SET not_before = ? WHERE draft_id = ?",
            ((NOW + timedelta(days=1)).isoformat(), future_id),
        )
        conn.execute(
            "INSERT INTO posted_tweets "
            "(tweet_id,text,category,has_link,created_at) VALUES (?,?,?,?,?)",
            (
                "4000",
                "Recent linked post",
                "proof",
                1,
                (NOW - timedelta(days=1)).isoformat(),
            ),
        )

    plans = _planner(db).reconcile(NOW)

    assigned = {plan.get("draft_id") for plan in plans}
    assert valid_id in assigned
    assert not assigned.intersection({
        revoked_id, malformed_id, stale_id, future_id, link_id,
    })


def test_concurrent_reconcile_assigns_two_distinct_stable_drafts(tmp_path):
    path = str(tmp_path / "reconcile-race.db")
    seed = Database(path)
    for index, category in enumerate((
        "gym_strategy", "proof", "shareable_insight", "founder_story",
    )):
        _approved_queue_draft(
            seed,
            text=f"Concurrent candidate {index}",
            category=category,
            score=90 - index,
        )
    _planner(seed).ensure_day(NOW)
    barrier = threading.Barrier(2)
    errors = []
    results = []

    def worker():
        try:
            planner = _planner(Database(path))
            barrier.wait(timeout=5)
            results.append(planner.reconcile(NOW))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert all(
        len(result) == 2 and all(plan["status"] == "planned" for plan in result)
        for result in results
    )
    final = Database(path).list_publication_positions(
        NOW.astimezone(_planner(seed).audience_zone).date()
    )
    assert len(final) == 2
    assert all(plan["status"] == "planned" for plan in final)
    assert len({plan["draft_id"] for plan in final}) == 2


def test_concurrent_reconcile_cannot_exceed_one_weekly_link_plan(tmp_path):
    class BarrierAssignDatabase(Database):
        barrier = None

        def __init__(self, path):
            super().__init__(path)
            self.waited = False

        def assign_publication_plan_atomic(self, *args, **kwargs):
            if not self.waited:
                self.waited = True
                self.barrier.wait(timeout=5)
            return super().assign_publication_plan_atomic(*args, **kwargs)

    path = str(tmp_path / "link-plan-race.db")
    seed = Database(path)
    for index in range(2):
        _approved_queue_draft(
            seed,
            text=f"Link candidate {index} https://flexdropin.com/blog/{index}",
            category="proof",
            score=90 - index,
        )
    _planner(seed).ensure_day(NOW)
    BarrierAssignDatabase.barrier = threading.Barrier(2)
    errors = []

    def worker():
        try:
            _planner(BarrierAssignDatabase(path)).reconcile(NOW)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    final = Database(path).list_publication_positions(
        NOW.astimezone(_planner(seed).audience_zone).date()
    )
    assert sum(plan["status"] == "planned" for plan in final) == 1
    assert sum(plan["status"] == "open" for plan in final) == 1


def test_dry_run_simulation_keeps_approved_drafts_and_media_reservations(tmp_path):
    db = Database(str(tmp_path / "simulation.db"))
    media_root = tmp_path / "simulation-media"
    media_root.mkdir(mode=0o700)
    staged = media_root / "staged.jpg"
    content = b"\xff\xd8\xff\xe0adaptive-simulation"
    staged.write_bytes(content)
    media = MediaProcessor(db).process_new_file(
        str(staged),
        "simulation.jpg",
        "image/jpeg",
        len(content),
        "Simulation preview",
    )
    first_id = _approved_queue_draft(
        db,
        text="First simulated post",
        category="gym_strategy",
        score=90,
        media_id=media["id"],
    )
    second_id = _approved_queue_draft(
        db,
        text="Second simulated post",
        category="proof",
        score=89,
    )
    planner = _planner(db)
    plans = planner.reconcile(NOW)
    due = max(datetime.fromisoformat(plan["scheduled_for"]) for plan in plans)

    simulated = planner.simulate_due(due + timedelta(minutes=1))

    assert len(simulated) == 2
    assert {db.get_post_draft(first_id)["status"], db.get_post_draft(second_id)["status"]} == {
        "approved"
    }
    assert all(plan["status"] == "simulated" for plan in simulated)
    preserved_media = db.get_media_by_id(media["id"])
    assert preserved_media["lifecycle_state"] == "reserved"
    assert preserved_media["reserved_by_draft_id"] == first_id
    assert db.get_queue_counts(
        due.date(), "America/New_York"
    )["approved_or_planned"] == 2


def test_second_dry_run_day_prefers_unsimulated_drafts_without_consuming_queue(
    tmp_path,
):
    db = Database(str(tmp_path / "two-simulation-days.db"))
    draft_ids = [
        _approved_queue_draft(
            db,
            text=f"Simulation reserve {index}",
            category=category,
            score=90 - index,
        )
        for index, category in enumerate((
            "gym_strategy", "proof", "shareable_insight", "founder_story",
        ))
    ]
    first_planner = _planner(db, now=NOW)
    first_plans = first_planner.reconcile(NOW)
    first_ids = {plan["draft_id"] for plan in first_plans}
    first_due = max(datetime.fromisoformat(plan["scheduled_for"]) for plan in first_plans)
    assert len(first_planner.simulate_due(first_due + timedelta(minutes=1))) == 2

    next_day = NOW + timedelta(days=1)
    second_planner = _planner(db, now=next_day)
    second_plans = second_planner.reconcile(next_day)
    second_ids = {plan["draft_id"] for plan in second_plans}

    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == set(draft_ids)
    assert all(db.get_post_draft(draft_id)["status"] == "approved" for draft_id in draft_ids)


def test_plan_reason_decoder_rejects_sensitive_or_malformed_values(tmp_path):
    db = Database(str(tmp_path / "plan-reason-safety.db"))
    plans = _planner(db).ensure_day(NOW)
    with db._conn() as conn:
        conn.execute(
            "UPDATE publication_plans SET selection_reason_json = ? WHERE id = ?",
            (
                json.dumps({
                    "timing_reason": "cold_start",
                    "timing_bucket": "morning:0",
                    "source_urgency": "RAW SOURCE SECRET",
                }),
                plans[0]["id"],
            ),
        )

    assert db.list_publication_positions(
        NOW.astimezone(_planner(db).audience_zone).date()
    ) == [plans[1]]
