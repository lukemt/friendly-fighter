import pytest


@pytest.fixture(autouse=True)
def _no_match_recordings(monkeypatch, tmp_path):
    """friendly-fighter's agent records matches by default, and reads live
    switches in remote/ (remote control, probe, console); never from tests."""
    monkeypatch.setenv("FF_RECORD", "0")
    import agent.console
    import agent.probe
    import agent.remote

    switches = tmp_path / "remote-switches"
    monkeypatch.setattr(agent.remote, "ROOT", switches)
    monkeypatch.setattr(agent.probe, "CONTROL", switches / "probe.json")
    monkeypatch.setattr(agent.console, "ROOT", switches)
