"""Resign one of our seats in every active Red Alert Testing game (ends a self-play match).

Stop the agent runtime first and wait ~35 s, or the seat's lease is still
held (seat_busy). Usage: python scripts/ff_resign.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from altruagent.client import AltruAgentClient  # noqa: E402
from altruagent.mcp_game import MCPGameSession  # noqa: E402
from altruagent.official import OfficialAgentClient, OfficialSeatAuth, new_execution_id  # noqa: E402


def main() -> int:
    official = OfficialAgentClient()
    official.authenticate()
    seats = [a for a in official.assignments()
             if getattr(a, "context", "tournament") == "testing" and a.game_type == "red_alert"]
    print("testing Red Alert seats:", [a.seat_id[:8] for a in seats])
    execution_id = new_execution_id()
    done: set[str] = set()
    for seat in seats:
        for _ in range(8):
            try:
                auth = OfficialSeatAuth(official, seat.seat_id, execution_id)
                client = AltruAgentClient(control_url=official.control_url, auth=auth, load_env_file=False)
                client.login()
                grant = auth.grant
                break
            except Exception as exc:  # seat_busy while the old lease runs out
                print("grant failed, retrying:", type(exc).__name__, str(exc)[:120])
                time.sleep(10)
        else:
            continue
        if grant.game_session_id in done:
            continue
        game = MCPGameSession(client, session_id=grant.game_session_id, game_server_url=grant.gameapi_server_url)
        print("resigned:", str(game.resign())[:200])
        done.add(grant.game_session_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
