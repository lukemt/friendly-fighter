import pytest


@pytest.fixture(autouse=True)
def _no_match_recordings(monkeypatch):
    """friendly-fighter's agent records matches by default; never from tests."""
    monkeypatch.setenv("FF_RECORD", "0")
