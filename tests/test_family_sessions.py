"""Several sessions side by side (AGENTS.md): worktrees find the shared lab, families register their
own bots, game slots are shared, module bots are versioned by everything they import."""

from __future__ import annotations

import json
import subprocess
import threading
import time

import pytest

from lab import _main_checkout, batch
from lab import bots as registry


def _git(*args, cwd):
    subprocess.run(["git", "-c", "safe.directory=*", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   check=True, capture_output=True)


def test_a_worktree_finds_the_main_checkout(tmp_path):
    main = tmp_path / "repo"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    (main / "a.txt").write_text("a")
    _git("add", "a.txt", cwd=main)
    _git("commit", "-q", "-m", "a", cwd=main)
    _git("worktree", "add", "-q", str(main / ".worktrees" / "fam"), "-b", "family/fam", cwd=main)
    assert _main_checkout(main / ".worktrees" / "fam").resolve() == main.resolve()
    assert _main_checkout(main) == main


def _registry(tmp_path, core, families):
    path = tmp_path / "bots.json"
    path.write_text(json.dumps({"bots": core}))
    (tmp_path / "bots.d").mkdir()
    for name, rows in families.items():
        (tmp_path / "bots.d" / f"{name}.json").write_text(json.dumps({"bots": rows}))
    return path


def test_families_register_in_their_own_files_and_save_back_there(tmp_path):
    path = _registry(tmp_path, [{"name": "ff-x", "kind": "ff"}],
                     {"rush-hunter": [{"name": "rush-hunter-a", "kind": "ff", "params": {"knobs": {"posture": "defend"}}}]})
    table = registry.load(path)
    assert table["ff-x"].source == "" and table["rush-hunter-a"].source == "rush-hunter"
    table["rush-hunter-b"] = registry.Bot(name="rush-hunter-b", kind="ff", source="rush-hunter")
    registry.save(table, path)
    assert [b["name"] for b in json.loads(path.read_text())["bots"]] == ["ff-x"]
    family = json.loads((tmp_path / "bots.d" / "rush-hunter.json").read_text())["bots"]
    assert [b["name"] for b in family] == ["rush-hunter-a", "rush-hunter-b"] and "source" not in family[0]


def test_a_family_file_holds_only_its_own_names(tmp_path):
    path = _registry(tmp_path, [], {"rush": [{"name": "eco-a", "kind": "ff"}]})
    with pytest.raises(ValueError, match="named rush-"):
        registry.load(path)


def test_no_bot_is_registered_twice(tmp_path):
    path = _registry(tmp_path, [{"name": "rush-a", "kind": "ff"}], {"rush": [{"name": "rush-a", "kind": "ff"}]})
    with pytest.raises(ValueError, match="twice"):
        registry.load(path)


def test_a_new_bot_goes_to_its_family(tmp_path):
    path = _registry(tmp_path, [], {"rush": []})
    assert registry.owner("rush-a", family=None, path=path) == "rush"
    assert registry.owner("ff-new", family=None, path=path) == ""
    assert registry.owner("eco-a", family="eco", path=path) == "eco"
    with pytest.raises(ValueError, match="eco family"):
        registry.owner("ff-new", family="eco", path=path)


def test_a_module_bot_is_versioned_by_what_it_imports():
    files = registry.module_files("bots.jev_council")
    assert files[0] == "bots/jev_council.py" and "bots/jev_bot.py" in files and "agent/redalert_bot.py" in files
    assert not any(f.startswith("altruagent/") for f in files)
    assert registry.get("ff-council-rules").code_hash() != registry._files_hash(("bots/jev_council.py",))


@pytest.fixture()
def slots(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "SLOTS_DIR", tmp_path / "slots")
    return tmp_path / "slots"


def test_game_slots_are_shared_and_freed(slots, monkeypatch):
    monkeypatch.setenv("FF_LAB_SLOTS", "2")
    with batch.GameSlot(run_id=7, label="a", log=lambda *a, **k: None):
        with batch.GameSlot(run_id=8, label="b", log=lambda *a, **k: None):
            held = batch.slots_in_use()
            assert sorted(h["run"] for h in held) == [7, 8] and {h["slot"] for h in held} == {0, 1}
        assert [h["run"] for h in batch.slots_in_use()] == [7]
    assert batch.slots_in_use() == []


def test_a_game_waits_for_a_free_slot(slots, monkeypatch):
    monkeypatch.setenv("FF_LAB_SLOTS", "1")
    got = []
    first = batch.GameSlot(run_id=1, log=lambda *a, **k: None).__enter__()

    def second():
        with batch.GameSlot(run_id=2, poll=0.05, log=lambda *a, **k: None):
            got.append(time.monotonic())

    t = threading.Thread(target=second)
    released = time.monotonic() + 0.3
    t.start()
    time.sleep(0.3)
    assert not got
    first.__exit__(None, None, None)
    t.join(5)
    assert got and got[0] >= released - 0.05


def _quiet(**k):
    return batch.GameSlot(log=lambda *a, **kw: None, poll=0.02, **k)


def test_slots_go_first_come_first_served(slots, monkeypatch):
    """A worker that has just finished a game queues behind those already waiting (2026-10-16: runs
    holding slots took them back within milliseconds, and others waited for an hour)."""
    monkeypatch.setenv("FF_LAB_SLOTS", "1")
    order = []
    held = _quiet(run_id=1).__enter__()

    def wait(run):
        with _quiet(run_id=run):
            order.append(run)
            time.sleep(0.05)

    first = threading.Thread(target=wait, args=(2,))
    first.start()
    deadline = time.monotonic() + 5
    while not batch.waiting() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [w["run"] for w in batch.waiting()] == [2]
    held.__exit__(None, None, None)
    again = threading.Thread(target=wait, args=(1,))  # (run 1's worker, back for its next game at once)
    again.start()
    first.join(5)
    again.join(5)
    assert order == [2, 1] and batch.waiting() == []


def test_a_dead_waiters_ticket_is_dropped(slots, monkeypatch):
    monkeypatch.setenv("FF_LAB_SLOTS", "1")
    q = slots / batch.QUEUE_DIR_NAME
    q.mkdir(parents=True)
    (q / "00000000000000000001-999999-1").write_text('{"run": 5}')  # (nobody holds its lock)
    assert batch.waiting() == [] and not any(q.iterdir())
    with _quiet(run_id=6):
        assert [h["run"] for h in batch.slots_in_use()] == [6]
