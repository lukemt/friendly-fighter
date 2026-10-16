# bots/werewolf: the `werewolf` bot

## Brief

Before this, the agent played `legal_actions[0]` (voted for and killed the lowest seat) and never talked. This
package plays every role and phase, scripted and deterministic, with no model calls. **Status: built and tested in
the simulator; not yet seen on a real platform payload** (no Werewolf recording exists yet).

## How it plays

`agent/agent.py` routes `werewolf` to `bots.werewolf.create_agent()` (via `_other()`; Red Alert's path is untouched).
One `WerewolfAgent` per match, so memory lasts across days.

| Part | What it does | Code |
|---|---|---|
| Reader | Tolerant reader of `raw["game_state"]` (alive, dead with roles, vote history), the observation (role, wolf partner, seer results) | `view.py` |
| Chat parser | Regexes for seer claims, seer results, accusations, defences, wolf-pair plans. A verdict must follow the seat directly, so "Player0, who checked them as a wolf" never makes Player0 the wolf | `chat.py` |
| Belief | Enumerates every (wolf pair, seer) assignment, drops those contradicting hard facts (revealed roles, our role, our seer checks), and weighs the rest by claims, claimed results, votes and accusations. Gives P(wolf) and P(seer) per seat | `belief.py` |
| Policy | Villager/seer: vote the likeliest wolf, never abstain by default. Seer: check the likeliest unchecked wolf. Wolf: kill the likely seer or the biggest threat, never the partner, blend into the vote | `policy.py` |
| Talk | Scripted templates under 50 words: seer claim with results (at once on a wolf finding, else by day 2), stance and reason, defence, wolf plan whispered to the partner, wolf seer counter-claim (the wolf the real seer did not name; both wolves agree from the public record) | `talk.py` |
| Safety | Every answer is checked against `legal_actions` / message limits / recipients. Any exception falls back to a legal answer (vote: the most suspicious legal seat, never abstain by default; message: `TERMINATE_MESSAGING`) and is logged and kept in `agent.errors` | `agent.py` |

The runtime's recorder also keeps the discussion views (`message_state`) and what we said (`message`), which the
platform's per-move recording would otherwise miss.

## Simulator (`harness/`)

`engine.py` follows GAMES.md (peaceful night 1, tie = no lynch, wolves' tie goes to the lower seat, role reveals,
the 3-day rule, 5 chats of 50 words, private messages to one seat). Open question: does a night kill reset the stall
counter? The default is the strict reading (only a vote-out resets). Players: `FirstLegal` (today's agent), `RandomBot`,
`RuleBot` (talks in its own phrasing, follows the first seer claim, counter-claims as a wolf). RuleBot is my stand-in
for a competent LLM seat; how real seats play is unknown.

```bash
docker exec -u dev -w /workspace friendly-fighter-werewolf-dev-workspace-1 \
  .venv/bin/python -m bots.werewolf.harness.run --ours 1 --mix rule --games 3000 --ref first
```

### Results (1 of our bots, 6 RuleBots; 3000 games, seeds 1000..3999; win rate of our seat's team)

| Role of our seat | ours | first-legal in that seat | paired diff vs first-legal | rule bot in that seat | paired diff vs rule bot |
|---|---|---|---|---|---|
| wolf (n=883) | 68.1% | 59.2% | +8.8 [+5.8, +11.8] | 71.5% | -3.4 [-6.4, -0.4] |
| seer (n=404) | 28.0% | 5.0% | +23.0 [+18.6, +27.4] | 29.5% | -1.5 [-6.9, +4.0] |
| villager (n=1713) | 42.8% | 21.1% | +21.8 [+19.7, +23.9] | 28.7% | +14.1 [+11.8, +16.4] |
| all | 48.3% | 30.1% | +18.1 [+16.5, +19.7] | 41.4% | +6.9 [+5.1, +8.6] |

Ablations on the same games (our role win rates, wolf / seer / villager):
- Default: 68.1 / 28.0 / 42.9.
- No talk (`talk=false`): 63.0 / **6.7** / **26.6**. Talking is most of the value.
- `max_chats=1`: 64.3 / 29.2 / 40.0. `max_chats=2`: 66.1 / 28.5 / 43.3.
- Wolf day-1 fake seer claim (`preempt_claim`): 0.5 gives wolf 69.8, 1.0 gives 73.2. Not yet checked on a second seed.
- Seer claim day (`claim_by_day` 1/2/3/never): seer 26.2 / 28.0 / 28.2 / 28.2. No clear difference.
- `abstain_below` 0.34 / 0.45, `follow_partner=false`, `no_info_vote=hash`, `counterclaim=false`: all within noise.

**Not done yet:** a second seed block for the defaults; runs with 2 or 3 of our bots and with `first,random,rule` mixes
(the harness supports them: `--ours 2 --mix first,random,rule`); the sim wolf is still slightly worse than a RuleBot wolf.
Claims need a second seed (docs/STRATEGY.md).

## Replay

```bash
.venv/bin/python -m bots.werewolf.harness.replay ~/srv/friendly-fighter-werewolf/recordings/<session>/seat<N>.jsonl.gz --audit
```
Reruns a recording through the bot, prints decisions next to the live ones, counts fallbacks, and with `--audit` flags
views whose role, wolf partner, deaths or votes the reader failed to find. **First job once a real recording exists:**
check the guessed shapes (`dead`, `vote_history`, the seer's result text in the observation, `new_messages` on both
phases) and turn the recording into a test fixture. The test fixture today is a simulated recording.

## LLM proposal (not built, nothing is called; needs the user's go)

Seams exist: `Talker.next_message(TalkContext) -> Utterance | None` (the context has `facts()` as JSON), and any exception
or over-limit text is trimmed or falls back to terminate. A model would (a) write the chat from the belief's facts, and
(b) later read others' free text into events (the largest weakness of regexes). Cost guess per game, per LLM seat:
about 3 days x 3 messages + votes ~ 14 calls, ~1,500 input tokens (facts + transcript) and ~80 output tokens each,
so ~21k input and ~1.1k output tokens a game. At small-model prices ($1 / $5 per million tokens) ~ $0.03; at
mid-size prices ($3 / $15) ~ $0.08; prompt caching lowers it. These are assumptions, not quotes.

## Core changes (shared code)

`agent/agent.py`: `choose_action` and `choose_message` remember `context.game_type`; `_other()` returns the Werewolf bot
for `werewolf` (new `_werewolf()` method). Nothing else changes. Merge with the Pokémon branch not yet tried: both
edit `_other()`'s neighbourhood, and I avoided its adjacent lines on purpose.

## Testing

```bash
docker exec -u dev -w /workspace friendly-fighter-werewolf-dev-workspace-1 .venv/bin/python -m pytest -q tests/test_werewolf_bot.py
```
