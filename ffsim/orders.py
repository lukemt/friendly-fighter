"""The platform runner's order checks and their expansion into engine commands.

Each order of a ``play_action`` batch is checked on its own against the seat's
latest view (``Checked``): a runner rejection (``invalid``, ``not_owned``,
``wreck``, ``is_building``, ``target_not_visible``, ``not_available``,
``not_ready``) or a ``pending`` row plus the engine commands to send. Messages
match what the hosted platform answered in our probe matches (see
docs/BACKEND_ANALYSIS.md sections 6-7); a few never observed are marked
``unverified``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CMDS = (
    "attack", "attack_move", "build", "cancel", "deploy", "enter_transport", "guard", "harvest", "move", "place",
    "power_toggle", "rally", "repair", "sell", "set_primary", "set_stance", "stop", "train", "unload",
)
FIELDS = {
    "move": ("units", "to", "queued"),
    "attack_move": ("units", "to", "queued"),
    "attack": ("units", "target", "to", "queued"),
    "stop": ("units",),
    "deploy": ("units",),
    "unload": ("units",),
    "guard": ("units", "target", "queued"),
    "enter_transport": ("units", "target", "queued"),
    "harvest": ("units", "to", "queued"),
    "set_stance": ("units", "stance"),
    "build": ("item",),
    "train": ("item", "count"),
    "place": ("item", "at"),
    "cancel": ("item",),
    "sell": ("building",),
    "set_primary": ("building",),
    "repair": ("building", "on"),
    "power_toggle": ("building", "on"),
    "rally": ("building", "to"),
}
UNIT_ORDERS = {c for c, f in FIELDS.items() if "units" in f}
TAKES_BUILDINGS = {"attack", "stop", "set_stance", "guard"}
STANCES = ["hold_fire", "return_fire", "defend", "attack_anything"]
ACTION = {
    "move": "MOVE", "attack_move": "ATTACK_MOVE", "attack": "ATTACK", "stop": "STOP", "deploy": "DEPLOY",
    "unload": "UNLOAD", "guard": "GUARD", "enter_transport": "ENTER_TRANSPORT", "harvest": "HARVEST",
    "set_stance": "SET_STANCE", "build": "BUILD", "train": "TRAIN", "place": "PLACE_BUILDING",
    "cancel": "CANCEL_PRODUCTION", "sell": "SELL", "set_primary": "SET_PRIMARY", "repair": "REPAIR",
    "power_toggle": "POWER_DOWN", "rally": "SET_RALLY_POINT",
}
MAX_UNITS = 50
MAX_TRAIN = 10
TOGGLE_MEMORY_SECONDS = 2.0


@dataclass
class Checked:
    """One order after the runner's checks."""

    index: int
    cmd: str | None
    status: str = "pending"  # pending | rejected | ok (a no-op repair / power_toggle)
    reason: str = "pending"
    detail: str | None = None
    failed: list = field(default_factory=list)  # [{unit, reason}] skipped by the runner
    commands: list = field(default_factory=list)  # engine commands (dicts)

    def row(self) -> dict:
        return {"index": self.index, "cmd": self.cmd, "detail": self.detail, "commands": self._count(),
                "status": self.status, "reason": self.reason, "failed": list(self.failed)}

    def _count(self) -> int:
        return sum(max(1, c.get("count", 1)) for c in self.commands)

    @property
    def engine_commands(self) -> int:
        return self._count()


def _reject(c: Checked, reason: str, detail: str | None, failed: list | None = None) -> Checked:
    c.status, c.reason, c.detail, c.commands = "rejected", reason, detail, []
    if failed is not None:
        c.failed = failed
    return c


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _cell(value, name: str, size: tuple[int, int]) -> tuple[list | None, str | None]:
    if not (isinstance(value, (list, tuple)) and len(value) == 2 and all(_is_int(v) for v in value)):
        return None, f"{name} must be a cell [x, y] of two integers"
    w, h = size
    if not (0 <= value[0] < w and 0 <= value[1] < h):
        return None, f"{name} [{value[0]}, {value[1]}] is outside the map ({w}x{h})"
    return [value[0], value[1]], None


class View:
    """What the runner checks orders against: the seat's latest observation."""

    def __init__(self, obs: dict, map_size: tuple[int, int], *, now: float = 0.0,
                 repair_memory: dict | None = None, power_memory: dict | None = None) -> None:
        self.obs = obs or {}
        self.map_size = map_size
        self.now = now
        self.repair_memory = repair_memory if repair_memory is not None else {}
        self.power_memory = power_memory if power_memory is not None else {}
        units = self.obs.get("units") or []
        self.units = {u["id"]: u for u in units}
        self.buildings = {b["id"]: b for b in self.obs.get("buildings") or []}
        self.visible = {e["id"] for e in (self.obs.get("visible_enemies") or []) + (self.obs.get("visible_enemy_buildings") or [])}
        self.available = {str(i).lower() for i in self.obs.get("available_production") or []}
        self.production = self.obs.get("production") or []


def check_order(index: int, order, view: View) -> Checked:
    """The runner's verdict on one order, and the engine commands for it."""
    if not isinstance(order, dict):
        return _reject(Checked(index, None), "invalid", "an order must be an object with a cmd")  # unverified wording
    cmd = order.get("cmd")
    if cmd not in FIELDS:
        return _reject(Checked(index, None), "invalid", f"cmd must be one of {list(CMDS)}")
    c = Checked(index, cmd)
    unknown = sorted(k for k in order if k != "cmd" and k not in FIELDS[cmd])
    if unknown:
        return _reject(c, "invalid", f"unknown field(s) for {cmd}: {unknown}")
    queued = order.get("queued", False)
    if "queued" in order and not isinstance(queued, bool):
        return _reject(c, "invalid", "queued must be true or false")  # unverified wording
    if cmd in UNIT_ORDERS:
        return _unit_order(c, order, view, bool(queued))
    if cmd in ("build", "train", "place", "cancel"):
        return _item_order(c, order, view)
    return _building_order(c, order, view)


def _unit_order(c: Checked, order: dict, view: View, queued: bool) -> Checked:
    cmd = c.cmd
    units = order.get("units")
    if not isinstance(units, list) or not units:
        return _reject(c, "invalid", "units must be a non-empty list of unit ids")
    if not all(_is_int(u) for u in units):
        return _reject(c, "invalid", "units must be integer actor ids")
    if len(set(units)) != len(units):
        return _reject(c, "invalid", "units must be distinct")
    if len(units) > MAX_UNITS:
        return _reject(c, "invalid", f"at most {MAX_UNITS} units per order")

    base: dict = {"action": ACTION[cmd], "index": c.index}
    if cmd in ("move", "attack_move"):
        if "to" not in order:
            return _reject(c, "invalid", "to must be a cell [x, y] of two integers")
        cell, err = _cell(order["to"], "to", view.map_size)
        if err:
            return _reject(c, "invalid", err)
        base.update(target_x=cell[0], target_y=cell[1], queued=queued)
    elif cmd == "attack":
        has_target, has_to = order.get("target") is not None, order.get("to") is not None
        if has_target == has_to:
            return _reject(c, "invalid", "attack needs exactly one of target (an actor id) or to (a cell: attack ground)")
        if has_target:
            target = order["target"]
            if not _is_int(target):
                return _reject(c, "invalid", "target must be an integer actor id")  # unverified wording
            if target not in view.visible and target not in view.units and target not in view.buildings:
                return _reject(c, "target_not_visible", f"actor {target} is not visible to you")
            base.update(target_actor_id=target, queued=queued)
        else:
            cell, err = _cell(order["to"], "to", view.map_size)
            if err:
                return _reject(c, "invalid", err)
            base.update(target_x=cell[0], target_y=cell[1], queued=queued)
    elif cmd in ("guard", "enter_transport"):
        target = order.get("target")
        if not _is_int(target):
            return _reject(c, "invalid", "target must be an integer actor id")  # unverified wording
        if target not in view.units and target not in view.buildings:
            return _reject(c, "not_owned", f"target {target} is not one of your actors")  # unverified wording
        base.update(target_actor_id=target, queued=queued)
    elif cmd == "harvest":
        if order.get("to") is not None:
            cell, err = _cell(order["to"], "to", view.map_size)
            if err:
                return _reject(c, "invalid", err)
            base.update(target_x=cell[0], target_y=cell[1])
        base.update(queued=queued)
    elif cmd == "set_stance":
        stance = order.get("stance")
        if stance not in STANCES:
            return _reject(c, "invalid", f"stance must be one of {STANCES}")
        base.update(target_x=STANCES.index(stance))

    failed, details, live = [], [], []
    for uid in units:
        unit = view.units.get(uid)
        if unit is not None and unit.get("wreck"):
            failed.append({"unit": uid, "reason": "wreck"})
            details.append(f"unit {uid} is a wreck and takes no orders")  # unverified wording
        elif unit is not None:
            live.append(uid)
        elif uid in view.buildings:
            if cmd in TAKES_BUILDINGS:
                live.append(uid)
            else:
                failed.append({"unit": uid, "reason": "is_building"})
                details.append(f"unit {uid} is a building and cannot take this order")
        else:
            failed.append({"unit": uid, "reason": "not_owned"})
            details.append(f"unit {uid} no longer exists or is not yours")
    if not live:
        return _reject(c, failed[0]["reason"], "; ".join(details), failed)
    c.failed = failed
    c.commands = [dict(base, actor_id=uid) for uid in live]
    return c


def _item_order(c: Checked, order: dict, view: View) -> Checked:
    cmd = c.cmd
    item = order.get("item")
    if not isinstance(item, str) or not item:
        return _reject(c, "invalid", "item must be a name from available_production")  # unverified wording
    item = item.lower()
    if cmd in ("build", "train"):
        count = 1
        if cmd == "train":
            count = order.get("count", 1)
            if not _is_int(count) or not 1 <= count <= MAX_TRAIN:
                return _reject(c, "invalid", f"count must be an integer from 1 to {MAX_TRAIN}")
        if item not in view.available:
            return _reject(c, "not_available", f"{item} is not in available_production")
        command = {"action": ACTION[cmd], "index": c.index, "item_type": item}
        if cmd == "train":
            command["count"] = count
        c.commands = [command]
        return c
    queued = [p for p in view.production if str(p.get("item", "")).lower() == item]
    if cmd == "cancel":
        if not queued:
            return _reject(c, "not_available", f"{item} is not in any production queue")
        c.commands = [{"action": ACTION[cmd], "index": c.index, "item_type": item}]
        return c
    # place
    if not queued:
        return _reject(c, "not_ready", f"{item} is not in any production queue")
    if not any(p.get("ready") for p in queued):
        return _reject(c, "not_ready", f"{item} is not finished yet")  # unverified wording
    command = {"action": ACTION[cmd], "index": c.index, "item_type": item}
    if order.get("at") is not None:
        cell, err = _cell(order["at"], "at", view.map_size)
        if err:
            return _reject(c, "invalid", err)
        command.update(target_x=cell[0], target_y=cell[1])
    c.commands = [command]
    return c


def _building_order(c: Checked, order: dict, view: View) -> Checked:
    cmd = c.cmd
    bid = order.get("building")
    if not _is_int(bid):
        return _reject(c, "invalid", "building must be an integer actor id")  # unverified wording
    building = view.buildings.get(bid)
    if building is None:
        return _reject(c, "not_owned", f"{bid} is not one of your buildings")
    command = {"action": ACTION[cmd], "index": c.index, "actor_id": bid}
    if cmd == "rally":
        if "to" not in order:
            return _reject(c, "invalid", "to must be a cell [x, y] of two integers")
        cell, err = _cell(order["to"], "to", view.map_size)
        if err:
            return _reject(c, "invalid", err)
        command.update(target_x=cell[0], target_y=cell[1])
    elif cmd in ("repair", "power_toggle"):
        on = order.get("on")
        if on is not None and not isinstance(on, bool):
            return _reject(c, "invalid", "on must be true or false")  # unverified wording
        if cmd == "repair":
            on = True if on is None else on
            remembered = view.repair_memory.get(bid)
            current = remembered[0] if remembered and view.now - remembered[1] < TOGGLE_MEMORY_SECONDS else building.get("repairing")
            if bool(current) == on:
                c.status, c.reason, c.detail = "ok", "ok", "already_repairing" if on else "not_repairing"
                return c
        else:
            remembered = view.power_memory.get(bid)
            down = remembered[0] if remembered and view.now - remembered[1] < TOGGLE_MEMORY_SECONDS else building.get("powered_down")
            if on is not None and (not down) == on:
                c.status, c.reason, c.detail = "ok", "ok", "already_on" if on else "already_off"
                return c
    c.commands = [command]
    return c
