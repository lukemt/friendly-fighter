"""The bot registry: every bot the lab can seat, by name (``lab/bots.json``).

A bot is a ``kind`` and its settings:

- ``ff``: our agent (``agent.agent.create_agent``) with ``params.knobs`` (posture, attack_threshold,
  unit_focus, target);
- ``module``: any agent factory, ``params.factory`` = ``"package.module:function"`` (plus ``kwargs``);
- ``ai``: one of OpenRA's own AIs, ``params.type`` = normal / rush / turtle / naval;
- ``human``: a person or Claude at the controls (``lab human``), the game paused between moves.

``think`` is how much game time each decision costs the bot (virtual time): seconds, ``"measured"``
(the wall time it really takes) or ``"paused"``. Our agent decides in milliseconds; an LLM agent
takes one to a few seconds, which is how ``ff-slow`` plays.

A bot's **version** (``name@hash``) changes with its settings and with the code it runs (the
files in ``CODE``), so every game in the database says exactly what played, even between commits.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from . import ROOT

REGISTRY = ROOT / "lab" / "bots.json"
KINDS = ("ff", "module", "ai", "human")
AI_TYPES = ("normal", "rush", "turtle", "naval")
DEFAULT_THINK = 0.005  # seconds of game time per decision: what our agent spends (it decides in ~2-5 ms)
# The code each kind of bot runs (relative to the repository): its version changes with it.
CODE = {
    "ff": ["agent/agent.py", "agent/redalert_bot.py", "agent/commander.py", "examples/llm/redalert.py",
           "altruagent/runner.py"],
    "human": ["lab/human.py", "agent/redalert_bot.py"],
}


@dataclass
class Bot:
    name: str
    kind: str
    params: dict = field(default_factory=dict)
    think: float | str = DEFAULT_THINK
    description: str = ""
    tags: list[str] = field(default_factory=list)

    def spec(self) -> dict:
        return {"kind": self.kind, "params": self.params, "think": self.think}

    @property
    def is_agent(self) -> bool:
        return self.kind != "ai"

    def seat(self) -> dict:
        """The seat for ``Platform.create_match``."""
        if self.kind == "ai":
            return {"kind": f"ai:{self.params.get('type', 'normal')}", "label": self.name}
        return {"kind": "agent", "label": self.name, "key": "eak_live_" + hashlib.sha256(self.name.encode()).hexdigest(),
                "think": "paused" if self.kind == "human" else self.think}

    def code_hash(self) -> str:
        files = list(CODE.get(self.kind, []))
        if self.kind == "module":
            module = str(self.params.get("factory", "")).split(":", 1)[0]
            if module.startswith("bots.frozen."):  # (a frozen copy carries all of its code)
                package = ROOT / Path(*module.split(".")[:3])
                files += sorted(str(f.relative_to(ROOT)) for f in package.glob("*.py"))
            else:
                files.append(module.replace(".", "/") + ".py")
                files += CODE["ff"]  # (module bots usually build on our agent's code)
        if self.kind == "ai":
            return _engine_hash()
        return _files_hash(tuple(files))

    def version(self) -> str:
        h = hashlib.sha1((json.dumps(self.spec(), sort_keys=True) + self.code_hash()).encode()).hexdigest()[:8]
        return f"{self.name}@{h}"

    def make_agent(self, *, record: bool = False):
        """A fresh agent object for one game (what ``create_agent()`` returns to the runtime)."""
        if self.kind == "ff":
            from agent.agent import create_agent

            return create_agent(knobs=self.params.get("knobs") or {}, record=record, controls=False)
        if self.kind == "module":
            module, _, func = str(self.params["factory"]).partition(":")
            factory = getattr(importlib.import_module(module), func or "create_agent")
            return factory(**(self.params.get("kwargs") or {}))
        raise ValueError(f"bot {self.name} ({self.kind}) is not played by an agent object")


def freeze(table: dict, name: str, new_name: str, *, commit: str = "") -> "Bot":
    """A copy of a module bot that never changes: its module and the scripted bot it builds on
    (agent/redalert_bot.py) copied to ``bots/frozen/<new_name>/``, registered as ``new_name``."""
    bot = table[name]
    if bot.kind != "module":
        raise ValueError(f"{name} is a {bot.kind} bot: only module bots can be frozen (ff bots are knobs: copy the entry)")
    module, _, func = str(bot.params["factory"]).partition(":")
    safe = "".join(c if c.isalnum() else "_" for c in new_name)
    package = ROOT / "bots" / "frozen" / safe
    if package.exists():
        raise ValueError(f"{package} exists")
    package.mkdir(parents=True)
    for init in (ROOT / "bots" / "frozen" / "__init__.py", package / "__init__.py"):
        if not init.exists():
            init.write_text('"""Frozen bots: copies that never change (lab bots freeze)."""\n' if init.parent.name == "frozen" else "")
    source = (ROOT / (module.replace(".", "/") + ".py")).read_text()
    source = source.replace("from agent.redalert_bot import", "from .redalert_bot import")
    (package / "bot.py").write_text(f"# Frozen from {module} ({name} {bot.version()}) at {commit or 'the working tree'}.\n" + source)
    (package / "redalert_bot.py").write_text((ROOT / "agent" / "redalert_bot.py").read_text())
    frozen = Bot(name=new_name, kind="module", params={**bot.params, "factory": f"bots.frozen.{safe}.bot:{func or 'create_agent'}"},
                 think=bot.think, description=f"frozen {name} {bot.version()}" + (f" at {commit}" if commit else "")
                 + f": {bot.description}", tags=sorted(set(bot.tags) | {"frozen"}))
    table[new_name] = frozen
    return frozen


@lru_cache(maxsize=None)
def _files_hash(files: tuple[str, ...]) -> str:
    h = hashlib.sha1()
    for rel in files:
        path = ROOT / rel
        h.update(rel.encode())
        h.update(path.read_bytes() if path.exists() else b"(missing)")
    return h.hexdigest()


@lru_cache(maxsize=None)
def _engine_hash() -> str:
    try:
        from ffsim.engine_openra import ENGINE_DIR, engine_build_id

        return engine_build_id(ENGINE_DIR)
    except Exception:  # noqa: BLE001
        return "engine"


def load(path: Path = REGISTRY) -> dict[str, Bot]:
    data = json.loads(path.read_text())
    bots = {}
    for row in data["bots"]:
        bot = Bot(**row)
        if bot.kind not in KINDS:
            raise ValueError(f"{path}: bot {bot.name}: unknown kind {bot.kind!r}")
        bots[bot.name] = bot
    return bots


def save(bots: dict[str, Bot], path: Path = REGISTRY) -> None:
    rows = [{k: v for k, v in asdict(b).items() if v not in ({}, [], "") or k in ("name", "kind")}
            for b in bots.values()]
    path.write_text(json.dumps({"bots": rows}, indent=1) + "\n")


def parse_knobs(text: str) -> dict:
    from agent.redalert_bot import Knobs

    fields = Knobs.__dataclass_fields__
    knobs = {}
    for part in filter(None, text.split(",")):
        key, _, value = part.partition("=")
        if key not in fields or key in ("note", "aim"):
            raise KeyError(f"unknown knob {key!r} in {text!r}")
        knobs[key] = int(value) if value.lstrip("-").isdigit() else value
    return knobs


def knobs_name(knobs: dict) -> str:
    return "knobs:" + ",".join(f"{k}={v}" for k, v in sorted(knobs.items()))


def get(name: str, bots: dict[str, Bot] | None = None) -> Bot:
    """A bot by name; ``ai:<type>`` and ``human[:who]`` work without an entry."""
    bots = bots if bots is not None else load()
    if name in bots:
        return bots[name]
    if name.startswith("ai:") and name[3:] in AI_TYPES:
        return Bot(name=f"ai-{name[3:]}", kind="ai", params={"type": name[3:]})
    if name == "human" or name.startswith("human:"):
        return Bot(name=name.replace(":", "-"), kind="human", think="paused")
    if name.startswith("knobs:"):  # (our scripted bot with these knobs: knobs:posture=attack,attack_threshold=12)
        return Bot(name=name, kind="ff", params={"knobs": parse_knobs(name[6:])}, tags=["generated"])
    raise KeyError(f"no bot {name!r}: see 'lab bots' (or add one with 'lab bots add')")
