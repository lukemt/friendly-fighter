"""The bot registry: every bot the lab can seat, by name (``lab/bots.json``).

A bot is a ``kind`` and its settings:

- ``ff``: our agent (``agent.agent.create_agent``) with ``params.knobs`` (posture, attack_threshold,
  unit_focus, target);
- ``module``: any agent factory, ``params.factory`` = ``"package.module:function"`` (plus ``kwargs``);
- ``ai``: one of OpenRA's own AIs, ``params.type`` = normal / rush / turtle / naval / beginner / easy / medium;
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
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from . import FAMILY, ROOT

REGISTRY = ROOT / "lab" / "bots.json"
# Each family session registers its bots in a file of its own, named <family>.json (AGENTS.md): no
# two sessions write the same file. Every bot in it is named "<family>-...".
FAMILY_DIR_NAME = "bots.d"
FAMILY_NAME = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
KINDS = ("ff", "module", "ai", "human")
AI_TYPES = ("normal", "rush", "turtle", "naval", "beginner", "easy", "medium")
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
    source: str = field(default="", compare=False)  # the family file it is registered in ("": lab/bots.json)

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
            if module == "bots.external":  # (a contestant's bot: its commit is in the settings)
                advisor = str((self.params.get("kwargs") or {}).get("advisor") or "")
                scripted = (advisor.partition(":")[0].replace(".", "/") + ".py",) if ":" in advisor else ()
                return _files_hash(("bots/external.py", "lab/advise.py", "altruagent/runner.py") + scripted)
            if module.startswith("bots.frozen."):  # (a frozen copy carries all of its code)
                package = ROOT / Path(*module.split(".")[:3])
                files += sorted(str(f.relative_to(ROOT)) for f in package.iterdir() if f.suffix in (".py", ".json"))
            else:
                files += module_files(module)  # (the module and every repository module it imports)
                files += [f for f in CODE["ff"] if f not in files]  # (module bots usually build on our agent's code)
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
    family = owner(new_name)  # (before anything is written: a family session freezes only into its family)
    if str(bot.params.get("factory", "")).startswith("bots.external:"):
        raise ValueError(f"{name} is a contestant's bot, already pinned to a commit: copy its entry instead")
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
    def localize(text: str, here: Path) -> str:
        """Imports of the scripted bot and of other bots/ modules (``bots.x``, ``bots.family.x``, or
        relative ones in the bot's own folder) point into the package, and are copied too."""
        text = text.replace("from agent.redalert_bot import", "from .redalert_bot import")
        for dep in sorted({d for d in re.findall(r"^from bots\.([\w.]+) import", text, re.M) if not d.startswith("frozen")}):
            local = dep.replace(".", "__")
            if not (package / f"{local}.py").exists():
                file = ROOT / "bots" / (dep.replace(".", "/") + ".py")
                (package / f"{local}.py").write_text(localize(file.read_text(), file.parent))
            text = re.sub(rf"^from bots\.{re.escape(dep)} import", f"from .{local} import", text, flags=re.M)
        if here != ROOT / "bots":
            for dep in sorted(set(re.findall(r"^from \.(\w+) import", text, re.M)) - {"redalert_bot"}):
                if not (package / f"{dep}.py").exists() and (here / f"{dep}.py").is_file():
                    (package / f"{dep}.py").write_text(localize((here / f"{dep}.py").read_text(), here))
        return text

    module_file = ROOT / (module.replace(".", "/") + ".py")
    source = localize(module_file.read_text(), module_file.parent)
    (package / "bot.py").write_text(f"# Frozen from {module} ({name} {bot.version()}) at {commit or 'the working tree'}.\n" + source)
    (package / "redalert_bot.py").write_text((ROOT / "agent" / "redalert_bot.py").read_text())
    params = {**bot.params, "factory": f"bots.frozen.{safe}.bot:{func or 'create_agent'}"}
    kwargs = dict(params.get("kwargs") or {})
    for key, value in kwargs.items():  # (files the bot reads, such as a playbook variant, are frozen too)
        if isinstance(value, str) and value.endswith(".json") and (ROOT / value).is_file():
            (package / Path(value).name).write_text((ROOT / value).read_text())
            kwargs[key] = str((package / Path(value).name).relative_to(ROOT))
    if kwargs:
        params["kwargs"] = kwargs
    frozen = Bot(name=new_name, kind="module", params=params,
                 think=bot.think, description=f"frozen {name} {bot.version()}" + (f" at {commit}" if commit else "")
                 + f": {bot.description}", tags=sorted((set(bot.tags) - {"board"}) | {"frozen"}), source=family)
    table[new_name] = frozen
    return frozen


_IMPORT = re.compile(r"^\s*(?:from\s+(\.*[\w.]*)\s+import\s+([\w, ()]+)|import\s+([\w.]+))", re.M)
LOCAL_PACKAGES = ("agent", "bots")  # (the platform client and examples count as CODE["ff"] lists them)


def module_files(module: str) -> list[str]:
    """A module's file and the files of every repository module it imports, directly or not
    (relative imports too): the code a module bot runs, so that its version changes with any of it."""
    def file_of(name: str) -> str | None:
        for rel in (name.replace(".", "/") + ".py", name.replace(".", "/") + "/__init__.py"):
            if (ROOT / rel).is_file():
                return rel
        return None

    out: list[str] = []
    todo = [module]
    while todo:
        name = todo.pop()
        rel = file_of(name)
        if rel is None or rel in out:
            continue
        out.append(rel)
        package = name if rel.endswith("__init__.py") else name.rpartition(".")[0]
        for frm, names, plain in _IMPORT.findall((ROOT / rel).read_text()):
            if plain:
                targets = [plain]
            elif frm.startswith("."):
                base = package
                for _ in range(len(frm) - len(frm.lstrip(".")) - 1):
                    base = base.rpartition(".")[0]
                stem = frm.lstrip(".")
                prefix = f"{base}.{stem}" if stem else base
                targets = [prefix] + [f"{prefix}.{n.strip()}" for n in names.strip("() ").split(",") if n.strip()]
            else:
                targets = [frm] + [f"{frm}.{n.strip()}" for n in names.strip("() ").split(",") if n.strip()]
            todo += [t for t in targets if t.split(".")[0] in LOCAL_PACKAGES]
    return out


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
    """The registry: ``path`` and every family's file in ``bots.d/`` beside it."""
    bots: dict[str, Bot] = {}
    files = [("", path)] + [(f.stem, f) for f in sorted((path.parent / FAMILY_DIR_NAME).glob("*.json"))]
    for family, file in files:
        if family and not FAMILY_NAME.match(family):
            raise ValueError(f"{file}: a family is named in lowercase letters, digits and hyphens")
        for row in json.loads(file.read_text()).get("bots", []):
            bot = Bot(**row, source=family)
            if bot.kind not in KINDS:
                raise ValueError(f"{file}: bot {bot.name}: unknown kind {bot.kind!r}")
            if family and not bot.name.startswith(family + "-"):
                raise ValueError(f"{file}: bot {bot.name}: the {family} family's bots are named {family}-...")
            if bot.name in bots:
                raise ValueError(f"{file}: bot {bot.name} is registered twice (also in "
                                 f"{bots[bot.name].source + '.json' if bots[bot.name].source else path.name})")
            bots[bot.name] = bot
    return bots


def owner(name: str, family: str | None = FAMILY, path: Path = REGISTRY) -> str:
    """The file a new bot of this name goes into: the session's family file (and a family session
    may only add bots named "<family>-..."), or the family whose prefix it has, or lab/bots.json."""
    if family:
        if not name.startswith(family + "-"):
            raise ValueError(f"this is the {family} family's session: its bots are named {family}-... "
                             f"(AGENTS.md); {name!r} isn't")
        return family
    for f in sorted((path.parent / FAMILY_DIR_NAME).glob("*.json"), key=lambda f: -len(f.stem)):
        if name.startswith(f.stem + "-"):
            return f.stem
    return ""


def save(bots: dict[str, Bot], path: Path = REGISTRY) -> None:
    """Each bot back into the file it belongs to (a family's own file, or ``path``)."""
    by_file: dict[str, list[dict]] = {}
    for b in bots.values():
        row = {k: v for k, v in asdict(b).items() if k != "source" and (v not in ({}, [], "") or k in ("name", "kind"))}
        by_file.setdefault(b.source, []).append(row)
    path.write_text(json.dumps({"bots": by_file.pop("", [])}, indent=1) + "\n")
    for family, rows in by_file.items():
        file = path.parent / FAMILY_DIR_NAME / f"{family}.json"
        file.parent.mkdir(exist_ok=True)
        file.write_text(json.dumps({"bots": rows}, indent=1) + "\n")


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
