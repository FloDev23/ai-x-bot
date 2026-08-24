from datetime import datetime
from zoneinfo import ZoneInfo

from modules.content_planner import ContentPlanner, choose_portfolio_category


ROME = ZoneInfo("Europe/Rome")


def test_largest_rolling_deficit_wins():
    counts = {
        "gym_strategy": 1,
        "fitness_business_insight": 5,
        "shareable_fitness": 4,
        "product_proof": 2,
        "founder_journey": 2,
    }
    assert choose_portfolio_category(counts) == "gym_strategy"


def test_planner_skips_when_no_category_has_an_eligible_source(fake_db):
    fake_db.content_counts = {}
    fake_db.sources = []
    planner = ContentPlanner(fake_db)
    slot = datetime(2026, 8, 11, 14, 0, tzinfo=ROME)
    assert planner.plan(slot) is None


def test_planner_never_exceeds_two_slots_in_local_day(fake_db):
    fake_db.drafts_today = 2
    fake_db.sources = [{"id": 1, "source_type": "evergreen_idea"}]
    planner = ContentPlanner(fake_db)
    slot = datetime(2026, 8, 11, 20, 0, tzinfo=ROME)
    assert planner.plan(slot) is None


def test_planner_uses_largest_deficit_among_categories_with_sources(fake_db):
    fake_db.content_counts = {
        "gym_strategy": 10,
        "fitness_business_insight": 0,
        "shareable_fitness": 4,
        "product_proof": 0,
        "founder_journey": 0,
    }
    fake_db.sources = [{"id": 7, "source_type": "verified_news"}]
    planner = ContentPlanner(fake_db)

    plan = planner.plan(datetime(2026, 8, 11, 14, 0, tzinfo=ROME))

    assert plan.category == "fitness_business_insight"
    assert plan.source_ids == [7]
    assert plan.include_link is False


def test_gym_strategy_does_not_mix_in_product_proof_sources(fake_db):
    fake_db.content_counts = {}
    fake_db.sources = [
        {"id": 7, "source_type": "evergreen_idea"},
        {"id": 8, "source_type": "product_fact"},
    ]
    planner = ContentPlanner(fake_db)

    plan = planner.plan(datetime(2026, 8, 11, 14, 0, tzinfo=ROME))

    assert plan.category == "gym_strategy"
    assert plan.source_ids == [7]


def test_gym_strategy_prefers_one_recent_verified_news_source(fake_db):
    fake_db.content_counts = {}
    fake_db.sources = [
        {"id": 8, "source_type": "verified_news"},
        {"id": 5, "source_type": "founder_note"},
        {"id": 4, "source_type": "evergreen_idea"},
    ]
    planner = ContentPlanner(fake_db)

    plan = planner.plan(datetime(2026, 8, 11, 14, 0, tzinfo=ROME))

    assert plan.category == "gym_strategy"
    assert plan.source_ids == [8]


def test_product_proof_uses_only_the_most_recent_fact(fake_db):
    fake_db.content_counts = {
        "gym_strategy": 10,
        "fitness_business_insight": 10,
        "shareable_fitness": 10,
        "product_proof": 0,
        "founder_journey": 10,
    }
    fake_db.sources = [
        {"id": 7, "source_type": "product_fact"},
        {"id": 6, "source_type": "product_fact"},
    ]
    planner = ContentPlanner(fake_db)

    plan = planner.plan(datetime(2026, 8, 11, 14, 0, tzinfo=ROME))

    assert plan.category == "product_proof"
    assert plan.source_ids == [7]


def test_product_proof_gets_link_only_below_weekly_cap(fake_db):
    fake_db.sources = [{"id": 8, "source_type": "product_fact"}]
    planner = ContentPlanner(fake_db, max_links_per_week=1)
    slot = datetime(2026, 8, 11, 14, 0, tzinfo=ROME)

    first_plan = planner.plan(slot)
    fake_db.links_last_days = 1
    second_plan = planner.plan(slot)

    assert first_plan.category == "product_proof"
    assert first_plan.include_link is True
    assert second_plan.include_link is False


def _source(source_id, source_type, published_at="2026-08-20"):
    return {
        "id": source_id,
        "source_type": source_type,
        "metadata": {"published_at": published_at},
    }


def _force_gym_strategy(fake_db):
    fake_db.content_counts = {
        "gym_strategy": 0,
        "fitness_business_insight": 20,
        "shareable_fitness": 20,
        "product_proof": 20,
        "founder_journey": 20,
    }


def test_owned_blog_articles_are_eligible_only_for_approved_categories(fake_db):
    blog = _source(30, "owned_blog_article")
    planner = ContentPlanner(fake_db)

    by_category = planner._eligible_sources_by_category([blog])

    assert by_category["gym_strategy"] == [blog]
    assert by_category["fitness_business_insight"] == [blog]
    assert by_category["shareable_fitness"] == [blog]
    assert by_category["product_proof"] == []
    assert by_category["founder_journey"] == []


def test_rotation_excludes_live_and_orders_never_old_then_least_recent(fake_db):
    _force_gym_strategy(fake_db)
    fake_db.sources = [
        _source(1, "verified_news", "2026-08-22"),
        _source(2, "evergreen_idea", "2026-08-21"),
        _source(3, "owned_blog_article", "2026-08-20"),
        _source(4, "verified_news", "2026-08-23"),
    ]
    fake_db.source_usage = {
        1: {"bound_to_live_draft": True},
        2: {"last_published_at": "2026-07-01T10:00:00+00:00"},
        3: {"last_published_at": "2026-08-20T10:00:00+00:00"},
    }
    planner = ContentPlanner(fake_db)
    slot = datetime(2026, 8, 24, 14, 0, tzinfo=ROME)

    assert planner.plan(slot).source_ids == [4]

    fake_db.source_usage[4] = {
        "last_published_at": "2026-07-15T10:00:00+00:00",
    }
    assert planner.plan(slot).source_ids == [2]

    fake_db.source_usage[2] = {
        "last_published_at": "2026-08-23T10:00:00+00:00",
    }
    assert planner.plan(slot).source_ids == [4]


def test_rotation_ties_use_newest_source_date_then_descending_id(fake_db):
    _force_gym_strategy(fake_db)
    fake_db.sources = [
        _source(7, "evergreen_idea", "2026-08-20"),
        _source(8, "verified_news", "2026-08-21"),
        _source(9, "owned_blog_article", "2026-08-21"),
    ]
    planner = ContentPlanner(fake_db)

    assert planner.plan(
        datetime(2026, 8, 24, 14, 0, tzinfo=ROME),
    ).source_ids == [9]


def test_rotation_ranks_across_types_without_fixed_type_priority(fake_db):
    _force_gym_strategy(fake_db)
    fake_db.sources = [
        _source(20, "verified_news", "2026-08-24"),
        _source(10, "evergreen_idea", "2026-08-01"),
    ]
    fake_db.source_usage = {
        20: {"last_published_at": "2026-08-23T10:00:00+00:00"},
    }

    plan = ContentPlanner(fake_db).plan(
        datetime(2026, 8, 24, 14, 0, tzinfo=ROME),
    )

    assert plan.source_ids == [10]


def test_malformed_usage_fails_closed_without_a_plan(fake_db):
    fake_db.sources = [_source(1, "evergreen_idea")]
    fake_db.source_usage = None

    assert ContentPlanner(fake_db).plan(
        datetime(2026, 8, 24, 14, 0, tzinfo=ROME),
    ) is None


def test_malformed_usage_boolean_and_source_metadata_fail_closed(fake_db):
    fake_db.sources = [_source(1, "evergreen_idea")]
    fake_db.source_usage = {1: {"bound_to_live_draft": 1}}
    slot = datetime(2026, 8, 24, 14, 0, tzinfo=ROME)

    assert ContentPlanner(fake_db).plan(slot) is None

    fake_db.source_usage = {}
    fake_db.sources[0]["metadata"] = None
    assert ContentPlanner(fake_db).plan(slot) is None


def test_rotation_parses_news_datetime_when_breaking_publication_ties(fake_db):
    _force_gym_strategy(fake_db)
    fake_db.sources = [
        _source(20, "verified_news", "2026-08-23T12:00:00Z"),
        _source(21, "owned_blog_article", "2026-08-22"),
    ]

    plan = ContentPlanner(fake_db).plan(
        datetime(2026, 8, 24, 14, 0, tzinfo=ROME),
    )

    assert plan.source_ids == [20]


def test_owned_blog_link_requires_global_budget_and_thirty_day_source_gap(fake_db):
    _force_gym_strategy(fake_db)
    fake_db.sources = [_source(30, "owned_blog_article")]
    slot = datetime(2026, 8, 24, 14, 0, tzinfo=ROME)
    planner = ContentPlanner(fake_db, max_links_per_week=1)

    assert planner.plan(slot).include_link is True

    fake_db.links_last_days = 1
    assert planner.plan(slot).include_link is False

    fake_db.links_last_days = 0
    fake_db.source_usage[30] = {
        "last_linked_at": "2026-08-01T10:00:00+00:00",
    }
    link_free = planner.plan(slot)
    assert link_free.source_ids == [30]
    assert link_free.include_link is False

    fake_db.source_usage[30] = {
        "last_linked_at": "2026-07-01T10:00:00+00:00",
    }
    assert planner.plan(slot).include_link is True


def test_product_proof_preserves_global_link_cap_and_returns_one_source(fake_db):
    fake_db.content_counts = {
        "gym_strategy": 20,
        "fitness_business_insight": 20,
        "shareable_fitness": 20,
        "product_proof": 0,
        "founder_journey": 20,
    }
    fake_db.sources = [
        _source(8, "product_fact"),
        _source(7, "product_fact"),
    ]
    planner = ContentPlanner(fake_db, max_links_per_week=2)

    plan = planner.plan(datetime(2026, 8, 24, 14, 0, tzinfo=ROME))
    assert plan.source_ids == [8]
    assert plan.include_link is True

    fake_db.links_last_days = 2
    assert planner.plan(
        datetime(2026, 8, 24, 14, 0, tzinfo=ROME),
    ).include_link is False
