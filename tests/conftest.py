import pytest

from modules.ai_generator import AIGenerator
from tests.fakes import FakeDatabase, FakeNewsFetcher


@pytest.fixture
def fake_db():
    return FakeDatabase()


@pytest.fixture
def fake_news():
    return FakeNewsFetcher()


@pytest.fixture
def fake_ai():
    """Real AIGenerator methods with only the external completion boundary faked."""
    generator = AIGenerator.__new__(AIGenerator)
    generator.model = "fake-model"
    generator.responses = []

    def complete(*_args, **_kwargs):
        return generator.responses.pop(0) if generator.responses else None

    generator._complete = complete
    return generator
