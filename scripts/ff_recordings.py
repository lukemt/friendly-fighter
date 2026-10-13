"""Summarize friendly-fighter match recordings (see agent/recorder.py).

    python scripts/ff_recordings.py                 # every recorded match
    python scripts/ff_recordings.py --extract-config game_config.json

For each recording: how many states, moves and answers it holds, the game
time it covers, every observation field seen, the event kinds and the order
verdict reasons. ``--extract-config`` writes the newest recorded
``get_game_config`` (rules, order schema, maps) to a file.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import zlib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "recordings"


def read(path: Path):
    """Records of one file; a worker killed mid-write leaves a truncated tail."""
    try:
        with gzip.open(path, "rt") as handle:
            for line in handle:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    return
    except (EOFError, zlib.error, gzip.BadGzipFile):
        return


def summarize(path: Path) -> dict | None:
    kinds: Counter = Counter()
    obs_keys: set[str] = set()
    events: Counter = Counter()
    verdicts: Counter = Counter()
    seconds: list[float] = []
    config = None
    for record in read(path):
        kinds[record.get("kind")] += 1
        if record.get("kind") == "config":
            config = record.get("config")
        elif record.get("kind") == "state":
            raw = record.get("raw") or {}
            obs = raw.get("observation") or {}
            obs_keys.update(obs)
            for event in obs.get("events") or []:
                events[event.get("kind")] += 1
            game_seconds = (raw.get("time") or {}).get("game_seconds")
            if isinstance(game_seconds, (int, float)):
                seconds.append(game_seconds)
        elif record.get("kind") == "result":
            result = record.get("result") or {}
            if result.get("error"):
                verdicts[f"error:{result['error']}"] += 1
            for row in result.get("results") or []:
                verdicts[f"{row.get('status')}:{row.get('reason')}"] += 1
    if not kinds:
        return None
    return {
        "file": str(path.relative_to(ROOT.parent)),
        "records": dict(kinds),
        "game_seconds": [min(seconds), max(seconds)] if seconds else None,
        "observation_fields": sorted(obs_keys),
        "event_kinds": dict(events.most_common()),
        "verdicts": dict(verdicts.most_common(15)),
        "has_config": config is not None,
        "_config": config,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--extract-config", metavar="PATH", help="write the newest recorded game config here")
    args = parser.parse_args()
    files = sorted(ROOT.glob("*/*.jsonl.gz"), key=lambda p: p.stat().st_mtime)
    if not files:
        print(f"No recordings in {ROOT}.")
        return 1
    newest_config = None
    for path in files:
        summary = summarize(path)
        if summary is None:
            continue
        newest_config = summary.pop("_config") or newest_config
        print(json.dumps(summary, indent=2))
    if args.extract_config:
        if newest_config is None:
            print("No recording holds a game config yet.", file=sys.stderr)
            return 1
        Path(args.extract_config).write_text(json.dumps(newest_config, indent=2))
        print(f"Wrote {args.extract_config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
