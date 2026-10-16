"""Paid Jev (TypeSafe's credits) only for lab runs that ask for it: FF_LAB_PAID_JEV=N games."""

from __future__ import annotations

from lab import batch


def test_lab_runs_switch_paid_jev_off_unless_asked(monkeypatch):
    from bots import jev_bot

    monkeypatch.setenv("TYPESAFE_API_KEY", "a-real-key")
    monkeypatch.delenv(batch.PAID_JEV, raising=False)
    batch.lab_env()
    assert jev_bot._key() is None
    monkeypatch.setenv("TYPESAFE_API_KEY", "a-real-key")
    monkeypatch.setenv(batch.PAID_JEV, "4")
    batch.lab_env()
    assert jev_bot._key() == "a-real-key" and batch.paid_jev_allowed() == 4


def test_a_local_server_needs_no_key(monkeypatch):
    from bots import jev_bot

    monkeypatch.setenv("TYPESAFE_API_KEY", "off")
    monkeypatch.setenv("JEV_URL", "http://localhost:1/v1/systemone")
    assert jev_bot.Jev().key == "local"


def test_bad_values_allow_nothing(monkeypatch):
    for value in ("", "yes", "-3"):
        monkeypatch.setenv(batch.PAID_JEV, value)
        assert batch.paid_jev_allowed() == 0
