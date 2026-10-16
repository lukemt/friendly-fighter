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


def test_estimate_counts_jev_seats_by_minutes():
    jobs = [{"bots": ["j", "ai"], "minutes": 20}, {"bots": ["j", "j"], "minutes": 10}, {"bots": ["ai", "x"]}]
    seats, cost = batch.jev_estimate(jobs, lambda n: n == "j")
    assert seats == 3 and abs(cost - 40 * batch.JEV_USD_PER_SEAT_MINUTE) < 1e-9


def test_budget_defaults_to_a_dollar_an_hour(monkeypatch):
    monkeypatch.delenv(batch.JEV_BUDGET, raising=False)
    assert batch.jev_budget_per_hour() == 1.0
    monkeypatch.setenv(batch.JEV_BUDGET, "0.25")
    assert batch.jev_budget_per_hour() == 0.25
    monkeypatch.setenv(batch.JEV_BUDGET, "lots")
    assert batch.jev_budget_per_hour() == 1.0


def test_spent_counts_only_logs_written_within_the_hour(tmp_path, monkeypatch):
    import json
    import os

    from lab import jevlog

    monkeypatch.setattr(jevlog, "LAB_DIR", tmp_path)
    (tmp_path / "jev").mkdir()
    new, old = tmp_path / "jev" / "lab-a-0.jsonl", tmp_path / "jev" / "lab-b-1.jsonl"
    new.write_text("\n".join(json.dumps({"t": i, "tokens": 500_000}) for i in range(2)) + "\nnot json\n")
    old.write_text(json.dumps({"t": 1, "tokens": 9_000_000}) + "\n")
    os.utime(old, (1_000, 1_000))
    assert abs(jevlog.spent(now=new.stat().st_mtime + 10) - 1.0 * jevlog.PRICE_PER_MTOK) < 1e-9
