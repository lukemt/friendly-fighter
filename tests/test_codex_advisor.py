"""Codex as a bot's advisor (bots/codex_advisor.py), against a fake ``codex app-server``: no network."""

from __future__ import annotations

import json
import sys
import textwrap
import types

import pytest

from bots import codex_advisor as ca
from lab.advise import Skipped

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["posture", "confidence"],
          "properties": {"posture": {"type": "string", "enum": ["attack", "hold"]}, "confidence": {"type": "number"}}}

# A fake app-server: the JSON-RPC subset the desk speaks. Its mode (argv[1]) decides the answer.
FAKE = textwrap.dedent('''
    import json, os, sys
    mode = sys.argv[1]
    def send(m):
        sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()
    seen = {}
    for line in sys.stdin:
        m = json.loads(line)
        if "id" in m and "method" not in m:   # (our reply to its request)
            seen["reply"] = m.get("result"); continue
        method, rid = m.get("method"), m.get("id")
        if method == "initialize":
            send({"id": rid, "result": {"userAgent": "fake"}})
        elif method == "thread/start":
            seen["thread"] = m["params"]
            send({"id": rid, "result": {"thread": {"id": "t1"}, "model": m["params"]["model"]}})
        elif method == "account/rateLimits/read":
            send({"id": rid, "result": {"rateLimits": {"primary": {"usedPercent": 3}}}})
        elif method == "turn/start":
            send({"id": rid, "result": {"turn": {"id": "u1", "status": "inProgress"}}})
            if mode == "slow":
                continue
            if mode == "approval":
                send({"id": 99, "method": "item/commandExecution/requestApproval", "params": {"threadId": "t1"}})
                while "reply" not in seen:
                    reply = json.loads(sys.stdin.readline())
                    if reply.get("id") == 99:
                        seen["reply"] = reply.get("result")
            p = m["params"]
            if mode == "bad":
                text = json.dumps({"posture": "dance", "confidence": 1})
            elif mode == "fenced":
                text = "```json\\n" + json.dumps({"posture": "attack", "confidence": 0.5}) + "\\n```"
            else:
                text = json.dumps({"posture": "hold", "confidence": 0.9})
            echo = {"model": p.get("model"), "effort": p.get("effort"), "schema": p.get("outputSchema"),
                    "text": p["input"][0]["text"], "base": seen["thread"].get("baseInstructions"),
                    "dev": seen["thread"].get("developerInstructions"), "sandbox": seen["thread"].get("sandbox"),
                    "approval": seen["thread"].get("approvalPolicy"), "ephemeral": seen["thread"].get("ephemeral"),
                    "secret_seen": "FAKE_SECRET_KEY" in os.environ, "home_seen": "HOME" in os.environ,
                    "reply": seen.get("reply")}
            with open(os.environ.get("FAKE_ECHO", "/dev/null"), "w") as f:
                json.dump(echo, f)
            send({"method": "item/completed", "params": {"threadId": "t1", "item": {"type": "agentMessage", "text": text}}})
            send({"method": "thread/tokenUsage/updated", "params": {"threadId": "t1", "turnId": "u1",
                  "tokenUsage": {"total": {"totalTokens": 1234}, "last": {"totalTokens": 1234}}}})
            send({"method": "turn/completed", "params": {"threadId": "t1", "turn": {"id": "u1", "status": "completed", "durationMs": 7}}})
''')


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_app_server.py"
    script.write_text(FAKE)
    echo = tmp_path / "echo.json"
    monkeypatch.setenv("FAKE_ECHO", str(echo))
    monkeypatch.setenv(ca.OPT_IN, "1")
    monkeypatch.setattr(ca, "CODEX_DIR", tmp_path / "codex")
    servers = []

    def server(mode="ok"):
        s = ca.CodexServer(command=[sys.executable, str(script), mode], cwd=tmp_path / "cwd").start()
        servers.append(s)
        return s

    server.echo = lambda: json.loads(echo.read_text())
    yield server
    for s in servers:
        s.close()


def _desk(fake, mode="ok", cls=ca.CodexDesk):
    return cls(server_factory=lambda: fake(mode))


def test_a_desk_needs_the_opt_in(monkeypatch):
    monkeypatch.delenv(ca.OPT_IN, raising=False)
    with pytest.raises(RuntimeError, match="FF_LAB_CODEX=1"):
        ca.CodexDesk()


def test_never_astra():
    for cls in (ca.CodexDesk, ca.HenryCodexDesk, ca.HenryCodexSol, ca.HenryCodexTuned):
        assert "astra" not in cls.MODEL
    assert ca.CodexDesk.MODEL == "gpt-6-luna" and ca.HenryCodexDesk.MODEL == "gpt-6-luna"


def test_codex_answers_a_question_with_the_bots_own_prompt_and_schema(fake, monkeypatch):
    monkeypatch.setenv("FAKE_SECRET_KEY", "must-not-reach-codex")
    desk = _desk(fake)
    answer = desk.ask(system="You advise a bot.", payload={"t": 300, "army": 12}, schema=SCHEMA, model="claude-x")
    assert answer == {"posture": "hold", "confidence": 0.9}
    echo = fake.echo()
    assert echo["model"] == "gpt-6-luna" and echo["effort"] == "low" and echo["schema"] == SCHEMA
    assert json.loads(echo["text"]) == {"t": 300, "army": 12}
    assert echo["dev"] == "You advise a bot." and echo["base"] == ca.INSTRUCTIONS
    assert (echo["sandbox"], echo["approval"], echo["ephemeral"]) == ("read-only", "never", True)
    assert not echo["secret_seen"] and echo["home_seen"]  # (no credentials; its own login is under HOME)
    row = desk.records[-1]
    assert row["tokens"] == {"totalTokens": 1234} and row["answer"] == answer and row["asked_by"] == "claude-x"
    logged = [json.loads(line) for f in ca.CODEX_DIR.glob("*.jsonl") for line in f.read_text().splitlines()]
    assert len(logged) == 1 and "latency" in logged[0]


def test_the_tuned_henry_appends_our_notes_to_its_own_prompt_and_records_the_payload(fake):
    desk = _desk(fake, cls=ca.HenryCodexTuned)
    desk.ask(system="HENRY'S OWN PROMPT", payload={"t": 1000}, schema=SCHEMA)
    echo = fake.echo()
    assert echo["dev"] == "HENRY'S OWN PROMPT\n\n" + ca.HENRY_NOTES and echo["effort"] == "medium"
    assert echo["model"] == "gpt-6-luna" and desk.records[-1]["payload"] == {"t": 1000}
    plain = _desk(fake, cls=ca.HenryCodexDesk)
    plain.ask(system="HENRY'S OWN PROMPT", payload={}, schema=SCHEMA)
    assert fake.echo()["dev"] == "HENRY'S OWN PROMPT"  # (the untuned one sends henry's prompt as it is)


def test_an_answer_that_does_not_fit_the_schema_is_skipped(fake):
    desk = _desk(fake, "bad")
    with pytest.raises(Skipped, match="schema"):
        desk.ask(system="", payload={}, schema=SCHEMA)
    assert "schema" in desk.records[-1]["skipped"]


def test_a_fenced_answer_is_read(fake):
    assert _desk(fake, "fenced").ask(system="", payload={}, schema=SCHEMA)["posture"] == "attack"


def test_no_answer_in_time_is_skipped(fake):
    desk = _desk(fake, "slow")
    desk.TIMEOUT = 1.0
    with pytest.raises(Skipped, match="no answer"):
        desk.ask(system="", payload={}, schema=SCHEMA)


def test_approval_requests_are_declined(fake):
    desk = _desk(fake, "approval")
    desk.ask(system="", payload={}, schema=SCHEMA)
    assert fake.echo()["reply"] == {"decision": "decline"} and desk.server.declined == 1


def test_a_server_that_cannot_start_is_a_skip_not_a_crash(monkeypatch):
    monkeypatch.setenv(ca.OPT_IN, "1")
    desk = ca.CodexDesk(server_factory=lambda: (_ for _ in ()).throw(OSError("no codex")))
    with pytest.raises(Skipped, match="no codex"):
        desk.ask(system="", payload={}, schema=SCHEMA)


def test_rate_limits(fake):
    assert fake().rate_limits()["rateLimits"]["primary"]["usedPercent"] == 3


def test_the_child_environment_has_no_credentials(monkeypatch):
    for k in ("TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "GH_TOKEN", "MY_SECRET", "DB_PASSWORD"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("PATH_LIKE_THING", "kept")
    env = ca.child_env()
    assert not {"TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "GH_TOKEN", "MY_SECRET", "DB_PASSWORD"} & set(env)
    assert env["PATH_LIKE_THING"] == "kept"


def test_the_inline_executor_runs_at_once():
    ex = ca.InlineExecutor()
    assert ex.submit(lambda a, b=0: a + b, 2, b=3).result() == 5
    fut = ex.submit(lambda: 1 / 0)
    assert fut.done() and isinstance(fut.exception(), ZeroDivisionError)
    ex.shutdown(wait=False, cancel_futures=True)


def test_henry_adaptation_gives_its_advisor_the_game_clock_and_an_inline_executor(fake, monkeypatch):
    alias = "ext_henry_codex_test_1"

    class APIError(Exception):
        pass

    class Advisor:
        def __init__(self):
            import time
            self._clock, self._pool, self._last_call, self._backoff_until = time.monotonic, object(), 1e9, 5e8

    stub = types.ModuleType(f"{alias}._anthropic")
    stub.APIError = APIError
    mod = types.ModuleType(f"{alias}.agent.redalert.advisor")
    mod.Advisor = Advisor
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    adv = Advisor()
    clock = types.SimpleNamespace(seconds=42.0)
    clock.monotonic = lambda: clock.seconds
    seat = types.SimpleNamespace(alias=alias, clock=clock, session="s1", position=1)

    desk = _desk(fake, cls=ca.HenryCodexDesk)
    desk.ask(system="", payload={}, schema=SCHEMA, seat=seat)
    assert adv._clock() == 42.0 and isinstance(adv._pool, ca.InlineExecutor)
    assert adv._last_call == 42.0 and adv._backoff_until == 0.0  # (the interval now counts in game seconds)
    assert issubclass(stub.BadRequestError, APIError) and stub.BadRequestError is not APIError
    clock.seconds = 99.0
    adv._clock = None
    desk.ask(system="", payload={}, schema=SCHEMA, seat=seat)
    assert adv._clock is None  # (once per seat)


def test_the_registered_codex_henry_is_never_reused_and_leaves_ext_henry_alone():
    from lab import bots, reuse

    table = bots.load()
    b, plain = table["ext-henry-codex"], table["ext-henry"]
    kw = b.params["kwargs"]
    assert kw["advisor"] == "bots.codex_advisor:HenryCodexDesk" and b.think == "paused"
    assert kw["stubs"] == {"agent.vgc.providers:anthropic_credentials": True}
    assert "REDALERT_ADVISOR" not in kw["env"] and kw["env"]["REDALERT_ADVISOR_SECONDS"] == "30"
    assert kw["env"]["REDALERT_PROFILE_DIR"] != plain.params["kwargs"]["env"]["REDALERT_PROFILE_DIR"]
    assert not reuse.replays(b, lambda name: False)
    assert plain.params["kwargs"]["env"]["REDALERT_ADVISOR"] == "off"  # (the plain henry plays as before)
    tuned = table["ext-henry-codex-t"]
    assert tuned.params["kwargs"]["advisor"] == "bots.codex_advisor:HenryCodexTuned" and not reuse.replays(tuned, lambda n: False)
    assert tuned.version() != b.version()


HENRY = ca.LAB_DIR.parent / "external" / "henrysue-ucla-steve-nash-equilibrium"


@pytest.mark.skipif(not (HENRY / "agent" / "redalert" / "advisor.py").exists(), reason="no henry checkout")
def test_the_real_henry_builds_its_advisor_on_the_codex_desk(fake, monkeypatch):
    from bots import external
    from lab import bots

    monkeypatch.setattr(ca, "start_server", lambda: fake("ok"))
    kw = dict(bots.load()["ext-henry-codex"].params["kwargs"])
    kw["env"] = {**kw["env"], "REDALERT_PROFILE_DIR": str(ca.CODEX_DIR / "p"), "REDALERT_LOG_DIR": str(ca.CODEX_DIR / "l")}
    agent = external.create_agent(**kw)
    dispatch = sys.modules[f"{agent.seat.alias}.agent.dispatch"]
    red_alert = agent.inner._resolve(types.SimpleNamespace(game_type=sorted(dispatch._RED_ALERT)[0]))  # (as at its first move)
    advisor = red_alert._advisor
    assert advisor is not None, "henry played rules only: the stub's credentials weren't seen"
    assert type(advisor._provider).__name__ == "AnthropicProvider"
    out = advisor._provider.complete_structured([{"role": "system", "content": "x"}, {"role": "user", "content": "{}"}],
                                                "red_alert_advice", SCHEMA, timeout=5, effort="none")
    assert out == {"posture": "hold", "confidence": 0.9}
    assert isinstance(advisor._pool, ca.InlineExecutor)  # (adapted on its first question)
