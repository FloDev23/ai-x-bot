import pytest

from tests.fakes import FakeDatabase, FakeNewsFetcher


@pytest.fixture
def fake_db():
    return FakeDatabase()


@pytest.fixture
def fake_news():
    return FakeNewsFetcher()
