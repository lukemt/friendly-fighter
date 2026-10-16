"""Contestants' bots in the lab (bots/external.py) and the advisor desk (lab/advise.py), on a fake repo."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import types

import pytest

from bots import external
from lab import advise
from lab import bots as registry

SCHEMA = {"type": "object", "properties": {"posture": {"type": "string", "enum": ["calm", "bold"]},
                                           "count": {"type": "integer"}},
          "required": ["posture", "count"], "additionalProperties": False}

FILES = {
    "agent/__init__.py": "",
    "agent/helper.py": "import os\nimport time\n\nNAME = 'theirs'\n\n\ndef now():\n    return time.monotonic()\n\n\n"
                       "def env(key):\n    return os.environ.get(key)\n",
    "agent/advisor.py": (
        "import json\nimport threading\nimport time\n\nSCHEMA = " + repr(SCHEMA) + "\n\n\n"
        "def ask(question):\n    import anthropic\n    out = {}\n\n    def run():\n        try:\n"
        "            client = anthropic.Anthropic(api_key='x').with_options(timeout=5)\n"
        "            r = client.messages.create(model='m', system=[{'type': 'text', 'text': 'be brief'}],\n"
        "                                       messages=[{'role': 'user', 'content': json.dumps(question)}],\n"
        "                                       output_config={'format': {'type': 'json_schema', 'schema': SCHEMA}})\n"
        "            out['answer'] = json.loads(r.content[0].text)\n"
        "        except anthropic.APIError as exc:\n            out['error'] = str(exc)\n"
        "        out['at'] = time.monotonic()\n\n"
        "    threading.Thread(target=run, daemon=True).start()\n    return out\n"),
    "agent/agent.py": (
        "from dotenv import load_dotenv\n\nfrom agent.helper import NAME, now\nfrom agent import advisor\n\n\n"
        "class Agent:\n    def __init__(self):\n        self.seen = []\n\n"
        "    def choose_action(self, state, context):\n        self.seen.append((NAME, now()))\n        print('chose at', now())\n"
        "        return 'WAIT'\n\n\ndef create_agent():\n    load_dotenv()\n    return Agent()\n"),
}


def _git(path, *args):
    return subprocess.run(["git", "-c", "safe.directory=*", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(path),
                           *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    root = tmp_path / "external" / "fake-repo"
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fake")
    monkeypatch.setattr(external, "EXTERNAL_DIR", tmp_path / "external")
    monkeypatch.setattr(external, "LOG_DIR", tmp_path / "logs")
    external.check_checkout.cache_clear()
    yield {"repo": "fake-repo", "commit": _git(root, "rev-parse", "HEAD")[:12], "root": root}
    external.check_checkout.cache_clear()
    advise._DESK = None


def _view(tick):
    return types.SimpleNamespace(raw={"observation": {"tick": tick}})


CONTEXT = types.SimpleNamespace(session_id="s1", seat_position=0)


def test_a_contestant_bot_loads_under_its_own_name(repo):
    a = external.create_agent(repo=repo["repo"], commit=repo["commit"], name="fake")
    b = external.create_agent(repo=repo["repo"], commit=repo["commit"], name="fake")
    assert a.seat.alias != b.seat.alias
    helper_a = sys.modules[f"{a.seat.alias}.agent.helper"]
    assert helper_a.NAME == "theirs" and helper_a is not sys.modules[f"{b.seat.alias}.agent.helper"]
    assert "agent.helper" not in sys.modules  # (their 'from agent.helper' never touched our package)
    assert not list(repo["root"].rglob("*.pyc"))  # (compiled in memory: their tree stays clean)


def test_its_clock_is_the_game_for_every_thread(repo):
    a = external.create_agent(repo=repo["repo"], commit=repo["commit"], name="fake")
    a.choose_action(_view(250), CONTEXT)
    assert a.inner.seen[-1] == ("theirs", 10.0)
    helper = sys.modules[f"{a.seat.alias}.agent.helper"]
    seen = []
    t = threading.Thread(target=lambda: seen.append(helper.now()))
    t.start()
    t.join()
    assert seen == [10.0]
    a.choose_action(_view(100), CONTEXT)  # (never backwards)
    assert helper.now() == 10.0
    assert "chose at 10.0" in a.seat.lines[-1]


def test_the_environment_is_the_seats_own(repo):
    a = external.create_agent(repo=repo["repo"], commit=repo["commit"], name="fake", env={"LAB_TEST_KNOB": "1"})
    b = external.create_agent(repo=repo["repo"], commit=repo["commit"], name="fake")
    env = lambda agent, key: sys.modules[f"{agent.seat.alias}.agent.helper"].env(key)  # noqa: E731
    assert env(a, "LAB_TEST_KNOB") == "1" and env(b, "LAB_TEST_KNOB") is None
    assert env(a, "ANTHROPIC_API_KEY") == "" and "LAB_TEST_KNOB" not in os.environ


def test_no_api_without_a_desk(repo):
    a = external.create_agent(repo=repo["repo"], commit=repo["commit"], name="fake")
    out = sys.modules[f"{a.seat.alias}.agent.advisor"].ask({"q": 1})
    time.sleep(0.2)
    assert "no API" in out["error"]
    with pytest.raises(external.ExternalBotError):  # (a desk bot outside 'lab advise' fails before the game)
        external.create_agent(repo=repo["repo"], commit=repo["commit"], name="fake", advisor="desk")


def test_the_desk_answers_inline_and_checks_the_schema(repo, tmp_path):
    desk = advise.Desk(folder=tmp_path / "advise")
    advise._DESK = desk
    a = external.create_agent(repo=repo["repo"], commit=repo["commit"], name="fake", advisor="desk")
    a.choose_action(_view(500), CONTEXT)
    replies = []

    def operator():
        while desk.pending() is None:
            time.sleep(0.01)
        replies.append(desk.answer('{"posture": "wild", "count": 1}'))
        replies.append(desk.answer('{"posture": "bold", "count": 3}'))
        while desk.pending() is None:
            time.sleep(0.01)
        replies.append(desk.skip())

    t = threading.Thread(target=operator)
    t.start()
    ask = sys.modules[f"{a.seat.alias}.agent.advisor"].ask
    first = ask({"q": 1})  # (inline: the answer is there when ask returns, at the same game time)
    assert first == {"answer": {"posture": "bold", "count": 3}, "at": 20.0}
    assert "not one of" in replies[0] and replies[1].startswith("answered")
    second = ask({"q": 2})
    t.join()
    assert second["error"] == "skipped"
    rows = [json.loads(line) for line in desk.transcript().read_text().splitlines()]
    assert [r["answer"] for r in rows] == [{"posture": "bold", "count": 3}, None]
    assert rows[0]["payload"] == {"q": 1} and rows[0]["game_seconds"] == 20.0
    assert desk.brief().startswith("strategy:") and desk.brief("strategy") == "be brief"  # (posture: a strategy layer)
    desk.detach()
    assert ask({"q": 3})["error"] == "detached"


def test_the_checkout_is_pinned(repo):
    with pytest.raises(external.ExternalBotError, match="pinned"):
        external.create_agent(repo=repo["repo"], commit="0000000", name="fake")
    (repo["root"] / "agent" / "helper.py").write_text("NAME = 'changed'\n")
    external.check_checkout.cache_clear()
    with pytest.raises(external.ExternalBotError, match="local changes"):
        external.create_agent(repo=repo["repo"], commit=repo["commit"], name="fake")


def test_versions_follow_the_commit_not_our_bot():
    def bot(commit):
        return registry.Bot(name="ext", kind="module", params={"factory": "bots.external:create_agent",
                                                              "kwargs": {"repo": "r", "commit": commit}})
    assert bot("aaa").version() != bot("bbb").version()
    assert bot("aaa").code_hash() == registry._files_hash(("bots/external.py", "lab/advise.py", "altruagent/runner.py"))
    with pytest.raises(ValueError):
        registry.freeze({"ext": bot("aaa")}, "ext", "ext-v1")


def test_answers_are_checked_against_the_schema():
    assert advise.validate({"posture": "calm", "count": 2}, SCHEMA) == []
    errors = advise.validate({"posture": "calm", "count": "2", "extra": 1}, SCHEMA)
    assert any("integer" in e for e in errors) and any("unknown field" in e for e in errors)
    assert advise.skeleton(SCHEMA) == {"posture": "calm", "count": 0}
    assert advise.layer_of({"properties": {"main_action": {}}}) == "tactical"


def test_the_scripted_plan_answers_both_layers_and_aims_clear_of_defenses():
    from bots.ryan_plan import PlanDesk

    desk = PlanDesk()
    strategy = desk.ask(system="", payload={"match": {"time": {"game_seconds": 30}}},
                        schema={"properties": {"posture": {}}})
    assert strategy["posture"] == "economy" and strategy["build_targets"][0] == {"item": "proc", "count": 2}
    p = {"known_enemy_buildings": [{"type": "tsla", "x": 60, "y": 60}],
         "visible_enemies": [{"type": "e1", "x": 58, "y": 59}, {"type": "harv", "x": 40, "y": 45}]}
    assert desk._target(p, {}, (20, 20), (66, 66)) == (40, 45)  # (the rifleman stands under the tesla coil)
    p["visible_enemies"] = []
    spot = desk._target(p, {}, (20, 20), (66, 66))
    assert ((spot[0] - 60) ** 2 + (spot[1] - 60) ** 2) ** 0.5 > 9
