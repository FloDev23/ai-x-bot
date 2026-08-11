import pytest

from modules.ai_generator import AIGenerator
from tests.fakes import FakeDatabase, FakeNewsFetcher


class FakeTelegramApi:
    def __init__(self):
        self.messages = []
        self.answered_callbacks = []
        self.callback_answers = []
        self.callback_error = None

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((str(chat_id), text, kwargs))
        return {"message_id": len(self.messages)}

    def answer_callback(self, callback_id, **kwargs):
        self.answered_callbacks.append(callback_id)
        self.callback_answers.append((callback_id, kwargs))
        if self.callback_error is not None:
            raise self.callback_error
        return True


@pytest.fixture
def fake_db():
    return FakeDatabase()


@pytest.fixture
def fake_telegram():
    return FakeTelegramApi()


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
