#!/usr/bin/env python3
"""Sessions that build bots of their own family, side by side (AGENTS.md).

    python scripts/session.py new NAME [--base main]   # a worktree .worktrees/NAME on branch family/NAME
    python scripts/session.py list                     # the family sessions, their branches and bots
    python scripts/session.py remove NAME [--force]    # the worktree (the branch stays)
    python scripts/session.py hooks                    # (re)install the repository's git hooks

Run it from the main checkout (/workspace). A family session works in its worktree only: its bots
live in ``bots/<package>/`` (the name with underscores: ``rush-wave`` -> ``bots/rush_wave/``), are
registered in ``lab/bots.d/NAME.json`` and named ``NAME-...``; the lab
(database, traces, moment sets, notes, game slots) is the main checkout's ``sim/lab``, shared.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lab import FAMILY, MAIN_ROOT  # noqa: E402
from lab import bots as registry  # noqa: E402

WORKTREES = MAIN_ROOT / ".worktrees"
HOOKS = ROOT / "scripts" / "git-hooks"
# Names taken by bots that exist already (their prefixes), or that would confuse.
RESERVED = {"main", "ff", "ai", "ext", "exploit", "knobs", "human", "frozen", "lab", "agent", "bots", "test"}
IDENTITY = ["-c", "user.name=Lukas Petry", "-c", "user.email=lukas.petry@pn-consulting.de"]


def git(*args: str, cwd: Path = MAIN_ROOT, check: bool = True) -> str:
    out = subprocess.run(["git", "-c", f"safe.directory={cwd}", "-c", f"safe.directory={MAIN_ROOT}", *args],
                         cwd=cwd, capture_output=True, text=True)
    if check and out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {out.stderr.strip() or out.stdout.strip()}")
    return out.stdout.strip()


def install_hooks() -> list[str]:
    """Copy scripts/git-hooks/* into the repository's hooks folder (shared by every worktree)."""
    common = Path(git("rev-parse", "--git-common-dir"))
    hooks = (common if common.is_absolute() else MAIN_ROOT / common) / "hooks"
    hooks.mkdir(exist_ok=True)
    done = []
    for src in sorted(HOOKS.iterdir()):
        dst = hooks / src.name
        shutil.copyfile(src, dst)
        dst.chmod(0o755)
        done.append(src.name)
    return done


README = """# The {name} family

> A family of bots built in its own session (AGENTS.md). Keep this file current: it is how the
> coordinator and the other sessions learn what this family is and what it has shown.

## Brief

(Written by the coordinator: the idea, the weakness it targets with its evidence, the moment sets
and baselines to start from, and what counts as done.)

## The idea

(What these bots do differently, and why it could beat the champion or a part of the field.)

## Bots

| Bot | What it is | Status |
|---|---|---|

## Results

(Each claim with its evidence: run ids, `lab probe` / `lab compare` output, games to look at.)

## Core changes

(Every change to shared code, rule 8: the file, what it adds, that it is off by default, its test.)

## Open questions and next steps
"""

INIT = '''"""The {name} family's bots (bots/{package}/README.md, AGENTS.md)."""
'''


def package(name: str) -> str:
    """A family's folder under bots/: its name with underscores (a Python package can't have hyphens)."""
    return name.replace("-", "_")


def cmd_new(a) -> int:
    name = a.name
    if FAMILY or ROOT != MAIN_ROOT:
        raise SystemExit("run this from the main checkout (/workspace), not from a family's worktree")
    if not registry.FAMILY_NAME.match(name) or len(name) > 24:
        raise SystemExit("a family's name: lowercase letters, digits and hyphens, up to 24 characters (e.g. rush-hunter)")
    taken = sorted(b for b in registry.load() if b.startswith(name + "-"))
    if name in RESERVED or taken:
        raise SystemExit(f"{name!r} is taken" + (f" (bots named {', '.join(taken[:3])}...)" if taken else ""))
    path = WORKTREES / name
    if path.exists() or git("branch", "--list", f"family/{name}"):
        raise SystemExit(f"{path} or the branch family/{name} exists already (python scripts/session.py list)")
    install_hooks()
    WORKTREES.mkdir(exist_ok=True)
    git("worktree", "add", str(path), "-b", f"family/{name}", a.base)
    pkg = path / "bots" / package(name)
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(INIT.format(name=name, package=package(name)))
    (pkg / "README.md").write_text(README.format(name=name))
    (path / "lab" / "bots.d").mkdir(exist_ok=True)
    (path / "lab" / "bots.d" / f"{name}.json").write_text(json.dumps({"bots": []}, indent=1) + "\n")
    git("add", f"bots/{package(name)}", f"lab/bots.d/{name}.json", cwd=path)
    git(*IDENTITY, "commit", "-q", "-m", f"The {name} family: its folder and registry file (scripts/session.py new)",
        cwd=path)
    print(f"""family session {name!r} ready:
  worktree  {path}  (branch family/{name}, from {a.base})
  bots      bots/{package(name)}/  (README.md: keep it current; modules bots.{package(name)}.<module>)
  registry  lab/bots.d/{name}.json  (bots named {name}-...)
  lab       {MAIN_ROOT / 'sim' / 'lab'}  (shared: found by itself, nothing to set)

Start the agent in the worktree, e.g.:
  cd {path} && claude        (or: codex)
and give it its brief; AGENTS.md there has the rules.""")
    return 0


def worktrees() -> list[dict]:
    out, cur = [], {}
    for line in git("worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if cur:
                out.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value or True
    return out


def cmd_list(a) -> int:
    from lab.db import LabDB

    db = LabDB()
    rows = [w for w in worktrees() if Path(w["worktree"]).parent == WORKTREES]
    if not rows:
        print("no family sessions (python scripts/session.py new NAME)")
        return 0
    for w in rows:
        path = Path(w["worktree"])
        name = path.name
        branch = str(w.get("branch", "")).removeprefix("refs/heads/")
        ahead = git("rev-list", "--count", f"main..{branch}", check=False) or "?"
        behind = git("rev-list", "--count", f"{branch}..main", check=False) or "?"
        last = git("log", "-1", "--format=%cr: %s", branch, check=False)
        dirty = git("status", "--porcelain", cwd=path, check=False)
        try:
            bots = [b["name"] for b in json.loads((path / "lab" / "bots.d" / f"{name}.json").read_text())["bots"]]
        except (OSError, ValueError, KeyError):
            bots = []
        games = db.one("SELECT COUNT(DISTINCT game_id) AS n FROM seats WHERE bot LIKE ?", name + "-%")["n"]
        print(f"{name:20} {branch:28} {ahead} ahead, {behind} behind main{', uncommitted changes' if dirty else ''}")
        print(f"{'':20} last commit {last[:90]}")
        print(f"{'':20} {len(bots)} bot(s){': ' + ', '.join(bots[:6]) if bots else ''}; {games} game(s) in the lab")
    return 0


def cmd_remove(a) -> int:
    path = WORKTREES / a.name
    if not path.exists():
        raise SystemExit(f"no worktree {path}")
    if git("status", "--porcelain", cwd=path, check=False) and not a.force:
        raise SystemExit(f"{path} has uncommitted changes (commit them on family/{a.name}, or --force)")
    git("worktree", "remove", *(["--force"] if a.force else []), str(path))
    print(f"removed {path}; the branch family/{a.name} stays (git branch -d family/{a.name} once merged)")
    return 0


def cmd_hooks(a) -> int:
    print("installed: " + ", ".join(install_hooks()))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("new", help="a family session: worktree, branch, folder, registry file")
    s.add_argument("name")
    s.add_argument("--base", default="main", help="the commit to start from (default main)")
    sub.add_parser("list", help="the family sessions")
    s = sub.add_parser("remove", help="remove a family's worktree (the branch stays)")
    s.add_argument("name")
    s.add_argument("--force", action="store_true")
    sub.add_parser("hooks", help="install the git hooks (the push guard) for every worktree")
    a = p.parse_args()
    return {"new": cmd_new, "list": cmd_list, "remove": cmd_remove, "hooks": cmd_hooks}[a.command](a)


if __name__ == "__main__":
    raise SystemExit(main())
