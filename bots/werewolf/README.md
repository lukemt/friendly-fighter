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

### Results

All numbers: win rate of our seat's team, 95% interval in brackets, `n` = our seats. Paired = the same game (roles, seats,
opponents) replayed with the reference player in our seats. **Two seed blocks** (1000..3999 and 50000..52999, 3000 games
each) gave the same picture; the tables below are the fresh block (50000..), measured before the last default change
(`preempt_claim` 0 -> 0.5, below).

**1 of our bots among 6 RuleBots** (the stand-in for competent seats):

| Role of our seat | ours (preempt 0) | first-legal in that seat | paired diff | rule bot in that seat | paired diff |
|---|---|---|---|---|---|
| wolf (n=915) | 68.9% | 59.8% | +9.1 [+6.3, +11.9] | 74.2% | -5.4 [-8.1, -2.6] |
| seer (n=419) | 28.4% | 6.0% | +22.4 [+18.1, +26.8] | 28.2% | +0.2 [-5.0, +5.5] |
| villager (n=1666) | 43.8% | 21.6% | +22.1 [+20.0, +24.3] | 28.6% | +15.2 [+12.9, +17.5] |
| all (n=3000) | 49.3% | 31.1% | +18.2 [+16.6, +19.8] | 42.4% | +6.8 [+5.1, +8.6] |

(First block: all +18.1 vs first-legal, +6.9 vs rule bot; wolf -3.4 vs rule bot, seer -1.5.)

**Wolf day-1 fake seer claim (`preempt_claim`), fresh block:** wolf 68.9% (0) -> 72.8% (0.5) -> 75.6% (1.0); all-roles 49.3 -> 50.5 -> 51.3.
First block agreed (68.1 -> 69.8 -> 73.2). The default is **0.5** ("sometimes"): at 1.0 the sim wolf beats the RuleBot wolf, but the
RuleBot seer is a weak contester, so I hedge. With 0.5 the wolf is about on par with a RuleBot wolf (72.8 vs 74.2).

**1 of ours among a mix of first-legal, random and rule bots** (1/3 each; fresh block, paired against first-legal):
all +6.0 [+4.6, +7.4]; wolf 85.8 vs 76.6 (+9.2); seer 32.0 vs 17.2 (+14.8); villager 21.4 vs 19.3 (+2.0 [+0.2, +3.9]).
The village is badly handicapped there (4+ non-talking seats), so our single villager adds little.

**2 of ours, same mix:** all 45.4% vs 38.5% (+6.9 [+5.6, +8.2]); wolf +6.1, seer +17.6, villager +4.6 (seat counts 1754 / 868 / 3378).
**3 of ours, same mix:** all 48.5% vs 38.9% (+9.6 [+8.5, +10.8]); wolf +2.4, seer +19.8, villager +10.7.
**3 of ours among 4 RuleBots:** all 49.0% vs 40.6% with RuleBots in those seats (+8.4); villager-side seats +22.6/+23.4, but when
the 3 seats are wolves they play against our stronger villagers (wolf seats 45.4% vs 72.5%: a table effect, not a weaker wolf).

Night kill resets the stall counter or not (`--night-resets`): identical results (the 3-day rule almost never fires when the
bot votes).

Ablations (first block, 3000 games; wolf / seer / villager win %): default 68.1 / 28.0 / 42.9. **No talk (`talk=false`): 63.0 / 6.7 /
26.6**, so talking is most of the value (in the sim, the RuleBots react to what we say; real seats may not). `max_chats` 1: 64.3 / 29.2 / 40.0;
2: 66.1 / 28.5 / 43.3. Seer `claim_by_day` 1/2/3/never: seer 26.2 / 28.0 / 28.2 / 28.2 (no clear difference). `abstain_below`,
`follow_partner=false`, `no_info_vote=hash`, `counterclaim=false`: within noise.

**What is not shown:** how well this works against real seats (LLM chat is messier than RuleBot's phrasing; the reader is a set
of regexes), and the payload shapes (below). The sim is evidence for the logic, not for the win rate on the platform.

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
for `werewolf` (new `_werewolf()` method). Nothing else changes. A trial three-way merge (`git merge-file`, base `4d11a02`) with the Pokémon branch's `agent/agent.py` gave no conflicts and the merged file parses.

## Testing

```bash
docker exec -u dev -w /workspace friendly-fighter-werewolf-dev-workspace-1 .venv/bin/python -m pytest -q tests/test_werewolf_bot.py
```
