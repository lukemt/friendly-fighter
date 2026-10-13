# friendly-fighter: project notes

Last updated 2026-10-13 (evening, US Pacific). These are the working notes for
this agent: what we're trying to do, what's built, what we learned, and what's
next. Secrets (keys and tokens) are never written here.

## Goal

Enter the **UCLA AltruAgent tournament** (<https://platform.altruagent-game.com/tournament>)
with a **self-hosted Red Alert agent**. Red Alert is a live 1v1 real-time game
on the OpenRA engine, run at 25 ticks/s. The agent:

- runs on our machine, using the Official Agent Key in `.env` (gitignored);
- is published as this public repo, `lukemt/friendly-fighter`, which
  registration requires.

Choices so far: self-hosted (not platform-hosted Markdown); OpenAI for any LLM
use; Red Alert as the only game we optimise for.

### Deadlines and caveats

- **Status at 2026-10-13 evening:** the Tournaments page says "No tournaments
  right now". The organizers haven't opened the Red Alert tournament yet.
  "Register my agent" appears once they do; you can register (and withdraw)
  until it starts. Keep checking
  https://platform.altruagent-game.com/tournament/dashboard/tournaments.
- **Tournament scoring:**
  - Win +1, loss 0, **draw 0**, bye +1. A draw is worth nothing, so play to
    win outright or win the time-limit tiebreak (kills − losses, then assets)
    clearly.
  - Format: Swiss rounds, then a single-elimination bracket of best-of-3
    matches (the final may differ).
  - A draw or no-result game is replayed. After 3 games in a row that don't
    count, an admin decides.
  - Server-side failures are replayed and never scored.

- **Submissions close 2026-10-13, 11:59 PM PDT.** The **"Register my agent"**
  click on the Tournaments page must happen before then. Self-hosted vs.
  platform-hosted can't be switched after the deadline.
- The docs suggest self-hosted code can keep changing after the deadline, since
  the platform only plays whatever runtime we run. **This is unconfirmed**:
  check the Official Rules before relying on it.
- The tournament start time is unknown; check the dashboard. Keep
  `python -m agent --tournament --match` running from before the start until
  the tournament ends. Only one runtime may run per key.
- The user wants about **2 more days of practice**, hence the local simulator
  work below.

## Architecture (what's built)

| Piece | File | What it does |
|---|---|---|
| Starter runtime | `altruagent/`, `agent/__main__.py` | The official starter (commit `e01e662`, imported unchanged). It polls assignments, takes seat grants, runs one process per match, and keeps leases alive |
| Entry point | `agent/agent.py` | `create_agent()` returns a `FriendlyFighter`. Red Alert goes to the scripted bot; other games go to the starter's LLM example if `OPENAI_API_KEY` is set, otherwise the first legal move |
| Red Alert bot | `agent/redalert_bot.py` | Scripted real-time controller, one batch per view (details below) |
| OpenAI commander | `agent/commander.py` | Optional; on only if `OPENAI_API_KEY` is set (currently **not set**). A background thread asks the model every ~15 s and adjusts the bot's settings (posture, attack threshold, unit focus, target). Play never waits for it |
| Recorder | `agent/recorder.py`, `scripts/ff_recordings.py` | Saves the config, states, moves and answers of every match to `recordings/<session>/seat<N>.jsonl.gz` (gitignored). `FF_RECORD=0` turns it off |
| Remote control | `agent/remote.py`, `scripts/ff_play.py` | Lets a human or Claude steer one seat via files in `remote/` (gitignored), with the bot as autopilot |
| Fast transport | `altruagent/mcp_transport.py` | Our change to the starter: one JSON-RPC POST per tool call over a kept-open `httpx` connection. Falls back to the SDK path (`FF_MCP_TRANSPORT=sdk` forces it) |
| Simulator setup | `sim/openra/` | Builds and runs OpenRA locally for AI-vs-AI matches (Phase 1 below) |

### Red Alert bot logic (`agent/redalert_bot.py`)

1. **Deploy** the MCV, then **place** every finished building right away.
2. **Power** comes first whenever spare power would drop below 20.
3. **Build order:** power → refinery → barracks → war factory → 2nd refinery
   → service depot → radar. More factories and refineries follow when cash
   allows. Defenses (at most 2) come only after the war factory and 2
   refineries exist.
4. **Spending:** units are bought only from *free* money, meaning cash minus
   what's still owed to queued items. At most 5 infantry before the war
   factory. Harvesters (2 per refinery, 6 max) come before tanks.
5. **Units:** the best vehicle available (heavy tanks first), with artillery
   or V2 rockets every 4th vehicle. Infantry alternate rifles and rockets.
6. **Defense:** enemies within 16 cells of home trigger an attack-move by
   everything nearby.
7. **Attack waves:**
   - Launch at the `attack_threshold` (default 8 units), growing by +2 per
     wave up to +6.
   - The first wave needs 3 vehicles, or 6 minutes of game time.
   - Targets in order: remembered enemy buildings → enemy spawn → a 4×4 map
     sweep (victory requires every enemy building dead).
   - In the last 60 s, no new waves if we're ahead on kills.
8. **Repeats and refusals:** build, train and place orders count as pending
   for 3 s, so they aren't sent twice. Rejected orders back off from 4 s to
   30 s. A building that can't be placed is cancelled after 6 tries.
9. **Logs:** a status line every 30 s (`[ff] status ...`).

## Findings from live test matches

- **Test 1 (self-play, old economy):** both sides hit $0 by 2:00. Infantry,
  an early tower and the war factory all competed for one refinery. An
  infantry wave into a flame tower decided the game at about 3:50. This led
  to the free-money spending rule (commit `2726b67`).
- **Test 2 (vs. "Quicksilver"):** cancelled before it started; the opponent
  never connected.
- **Test 3 (self-play, new economy, 10-minute limit):** the economy stayed
  healthy (12 buildings, 3 waves). It ended on time; Russia won the tiebreak
  17,400 vs. 17,200 in kills/losses. The recording is in
  `recordings/1deb69ec-…`; its last ~8 s are missing because the recorder
  wasn't closed at exit (fixed since).
- **Latency (big):** with the old transport the bot acted only every ~2.25 s.
  Each MCP call took ~1.9 s, because the SDK opened a new TCP+TLS connection
  and MCP session per call. With a kept-open connection a call takes
  ~0.2 s, so **about 0.4–0.5 s per decision is expected. Not yet confirmed in
  a live match.**
- **Data volume:**
  - A state is 3–10 KB of JSON (median ~7 KB, ~2,150 tokens at ~3 chars per
    token).
  - The game config is 63 KB (~17k tokens).
  - A 20-minute game at the new rate is ~2,700 states, ~20 MB, ~6 M tokens
    per seat.
  - So never send raw states to an LLM per decision; use summaries.
- **Map:** the live test map was the snow map `sidestep`. The bot only uses
  the spawns and bounds from `get_game_config`, **not the terrain layout**.
  Using the layout (ore fields, chokepoints, staging spots) is a
  to-do.

## Local simulator plan

**Goal:** practise and A/B-test without the platform, by rebuilding its
private game server (`Agent_ACP`, not public) as closely as we can.

**Evidence the platform is plain OpenRA:**

- 25 ticks/s is OpenRA's "Normal" speed.
- The seat names `Multi0`/`Multi1` are OpenRA's.
- The match rules (`starting_cash`, `starting_units`, `explored_map`,
  `kill_bounties`, `crates`) are OpenRA lobby options.
- The rule fields (`cost`, `build_time_s`, `power`, `prerequisites`, ...)
  look like OpenRA's YAML.
- The tournament maps `sidestep`, `trapped` and `shadowfiend-II` **ship with
  OpenRA**.

**Planned architecture:**

```text
our bot (unchanged runtime) --MCP--> local "gameapi" (Python, built from
  skill/redalert.md + real recordings) --socket--> OpenRA + an ExternalBot
  C# component per seat (observation out every 5 ticks, orders in)
orchestrator: parallel matches, random factions and maps, opponents =
  our variants / OpenRA AIs (rush, normal, turtle, naval) / starter LLM bot
```

**Phases:**

- **Phase 0 (done):** recorder, so real server data is available as ground
  truth.
- **Phase 1 (mostly done):**
  - OpenRA `release-20250330` built from source in `~/openra-sim`, with .NET
    6 installed in `~/openra-sim/dotnet`.
  - Freeware RA content installed through OpenRA's quick-install package,
    SHA1 checked.
  - Patch `sim/openra/aimatch.patch`: `OPENRA_AI_MATCH=normal,rush` with
    `Launch.Map=<map>` seats two bots, makes the host a spectator, starts
    the game, and prints `AIMATCH start` / `AIMATCH end ... result=...`.
  - `sim/openra/setup.sh` reproduces all of this on Debian/Ubuntu.
  - Runs under Xvfb with software GL.
  - Measured: **~110% CPU and ~600 MB RAM per match** with
    `LP_NUM_THREADS=1` (~280% without it).
  - **Still open:**
    - The first AI match had no time limit and ran 15+ minutes;
      `AIMATCH end` hasn't been seen yet.
    - Set a time limit via `OPENRA_AI_OPTIONS`.
    - Measure 2 matches at once.
    - Optionally build a null renderer to save the ~1 core spent on
      rendering.
- **Phase 2:** ExternalBot C# component (observation JSON and order
  execution, modelled on OpenRA's `ModularBot`).
- **Phase 3:** local game server matching `skill/redalert.md`, checked
  against the recordings.
- **Phase 4:**
  - Orchestrator and scoreboard.
  - A Docker image that runs on all machines (this container: 6 cores and
    ~3 GB free; the user's Intel Mac host and M2 MacBook Air, 16 GB each).
  - Keep CPU below about 70%, so the bots' reaction times stay realistic.

**Cheaper alternative (not yet built):** A/B tests on the real platform. In
self-play, each seat runs a different strategy variant (picked by
`seat_position`); several test matches run in parallel.

## How to

```bash
python -m agent --check-tournament            # key and registration check
python -m agent --tournament --match          # play everything (one copy per key!)
python -m pytest -q                           # tests (tests/conftest.py disables recording)
python scripts/ff_recordings.py [--extract-config cfg.json]
python scripts/ff_play.py on --seat 1         # take a seat (bot stays on as autopilot; --manual for none)
python scripts/ff_play.py status | attack X Y [all|ids] | build powr | train 3tnk 2 | knobs posture=defend | log | off
~/openra-sim/aimatch.sh sidestep.oramap normal rush 99   # local AI-vs-AI match on Xvfb display :99
```

- New match processes import the code fresh, so code changes apply to the
  **next** match without restarting the runtime.
- To end a live self-play match: stop the runtime, wait ~35 s for the seat
  lease to expire, then run `python scripts/ff_resign.py`.

## Gotchas learned

- `git` here needs `-c safe.directory=/workspace`; the commit identity is
  passed per command.
- This machine has no GitHub credentials. Pushes have used a user-provided
  token, which **should be revoked** after use. The Official Agent Key was
  also pasted into chat; consider **rotating** it (Agent Configuration →
  Rotate key) and updating `.env`.
- Never `rm` `recordings/` while a match runs. The workspace is virtiofs, so
  deleted-but-open files can't be recovered. A live recording was lost this
  way once.
- `pkill -f <pattern>` can kill the calling shell if the pattern is in its
  own command line. Kill by PID instead.
- OpenRA needs real ICU, not invariant globalization: install `libicu76`.
  Bot lobby commands must come before `state Ready`, which must come before
  `startgame`.
- The starter's per-call MCP session was the latency bottleneck (fixed).

## Next steps (in priority order)

1. **User:** click "Register my agent" before 11:59 PM PDT. Revoke the
   GitHub token. Consider rotating the agent key.
2. Run a self-play test match to **confirm the faster transport live** and
   check decisions per second in the recording.
3. Optionally play a seat with `ff_play.py`, Claude against the bot.
4. Use the map layout from the recorded config: ore fields for harvesters,
   defense placement, staging points, the order to sweep for remaining
   buildings.
5. Finish Phase 1 (time limit, parallel test), then Phases 2–4, or start
   with the cheaper platform A/B testing.
6. (Done: `scripts/ff_resign.py`.)
