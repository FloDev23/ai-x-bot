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


def test_product_proof_gets_link_only_below_weekly_cap(fake_db):
    fake_db.sources = [{"id": 8, "source_type": "product_fact"}]
    planner = ContentPlanner(fake_db)
    slot = datetime(2026, 8, 11, 14, 0, tzinfo=ROME)

    first_plan = planner.plan(slot)
    fake_db.links_last_days = 1
    second_plan = planner.plan(slot)

    assert first_plan.category == "product_proof"
    assert first_plan.include_link is True
    assert second_plan.include_link is False
