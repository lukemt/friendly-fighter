"""Reading other players' chat: seer claims, seer results, accusations, wolf-pair plans.

Plain regular expressions over the lowercased text. The other seats are probably LLM agents, so the
phrasing varies; everything here is best effort and conservative: a message that doesn't match
yields no events, never a wrong strong one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

N_SEATS = 7
ABSTAIN = 7
MAX_WORDS = 50
SAFE_WORDS = 44  # a margin under the platform's 50-word limit (tokenisers differ)
MAX_CHARS = 400

SEAT = r"\b(?:player|seat|p)\s*#?\s*([0-6])\b"
_SEAT_RE = re.compile(SEAT)
_T = r"(?:(?:player|seat|p)\s*#?\s*|#\s*)([0-6])\b"  # a seat mention (with a prefix) with its number captured
_S = r"(?:player|seat|p)\s*#?\s*([0-6])\b"  # a seat mention with its number captured

# "<seat> is a wolf", "<seat>: wolf", "<seat> -> villager", "<seat> (werewolf)", "<seat> is not a wolf":
# the verdict must follow the seat directly (copula, a few filler words), so that "Player0, who checked
# them as a wolf" does not make Player0 the wolf.
_FILLER = r"(?:(?:probably|likely|definitely|clearly|surely|really|also|indeed|actually|just|now|certainly|a|an|the|our|one)\s+)*"
_VERDICT_AFTER = re.compile(
    r"\s*(?:(?:,\s*)?(?:and\s+|but\s+)?(?:they|he|she|it)\s+)?(?:[:=]|->|\u2192|-|\(|is|was|are|'s|seems|looks|must be|has to be|turned out|came back|returned)?\s*"
    r"(?:(?:as|to be)\s+)?" + _FILLER + r"(?P<neg>(?:not|n't|no)\s+)?" + _FILLER +
    r"(?P<v>wolf|werewolf|villager|innocent|clean|town|human|safe|good)\b"
)

_SEER_CLAIM = re.compile(
    r"\bi(?:'m|m| am| was)\s+(?!not\b|no\b)(?:(?:actually|really|the|a|your|truly|also)\s+)*(?:real\s+|true\s+)?seer\b"
    r"|\bas (?:the |a |your )?(?:real )?seer\b"
    r"|\bi (?:claim|am claiming|will claim|hereby claim)(?: to be| as| the)?(?: the)? seer\b"
    r"|\bmy (?:investigation|investigations|check|checks|scan|scans|result|results)\b"
    r"|\bi (?:investigated|checked|scanned|inspected)\b"
    r"|\bseer here\b|\bi'?m seer\b"
)
_RESULT_WORDS = re.compile(
    r"\binvestigat|\bchecked\b|\bcheck\b|\bresult|\bscan|\binspect|\bfound\b|\bsaw\b|\blearned\b|\bnight\s*\d|\bn\d\b"
)
_NEG_VOTE = r"(?:don't|dont|do not|not|won't|never|shouldn't|no)\s+(?:\w+\s+)?"
_VOTE_FOR = re.compile(
    r"(?P<neg>" + _NEG_VOTE + r")?\b(?:vote|voting|lynch|lynching|eliminate|eliminating|hang|execute|kill)\b"
    r"(?:\s+(?:for|on|out|to|against|off|the))*\s*[:,]?\s*" + _T
)
_SUSPECT_VERB = re.compile(
    r"(?P<neg>" + _NEG_VOTE + r")?\b(?:suspect|suspicious of|accuse|accusing|distrust|doubt|don't trust|dont trust|targeting)\s+" + _S
)
_SUSPECT_ADJ = re.compile(
    _S + r"\s*(?:is|looks|seems|acts|was|has been|'s)\s+(?:(?:acting|being|looking|seeming)\s+)?(?:very |really |quite |kinda |a bit |pretty |so |too )*"
    r"(?:suspicious|sus|fishy|shady|lying|guilty|a liar|evil|the wolf|a wolf|wolf)\b"
)
_DEFEND_VERB = re.compile(r"\b(?:trust|believe|vouch for|defend|clear|cleared|back|backing|support)\s+" + _S)
_CLAIMED_BY = re.compile(_S + r"\s*(?:is|was|=|:|claimed|claims)?\s*(?:the |a )?(?:real )?seer\b")


@dataclass(frozen=True)
class ChatEvent:
    kind: str  # claim | result | accuse | defend | plan_kill | plan_vote | plan_counterclaim
    sender: int
    target: int | None = None
    verdict: str | None = None  # result: "wolf" | "villager"
    strength: float = 1.0


def verdict_after(text: str, start: int) -> str | None:
    """The role word that directly follows the seat mention ending at ``start``, as "wolf" or "villager"."""
    m = _VERDICT_AFTER.match(text, start)
    if m is None:
        return None
    wolf = m.group("v") in ("wolf", "werewolf")
    if m.group("neg"):
        wolf = not wolf
    return "wolf" if wolf else "villager"


def parse_message(text: str, sender: int, *, already_claimed: bool = False, from_partner: bool = False,
                  private: bool = False) -> list[ChatEvent]:
    """Events a chat message holds. ``already_claimed``: the sender claimed seer in an earlier message
    (so "Player3 is a wolf" in a later message of theirs can be a result)."""
    low = text.lower()
    events: list[ChatEvent] = []
    if private:
        for m in re.finditer(r"\bkill\b\D{0,14}?([0-6])\b", low):
            events.append(ChatEvent("plan_kill", sender, int(m.group(1))))
        for m in re.finditer(r"\bvote\b\D{0,14}?([0-6])\b", low):
            events.append(ChatEvent("plan_vote", sender, int(m.group(1))))
        if re.search(r"counter-?\s?claim|claim (?:as )?seer|i'?ll claim|i will claim", low):
            events.append(ChatEvent("plan_counterclaim", sender))
        return events
    claims = bool(_SEER_CLAIM.search(low))
    if claims:
        events.append(ChatEvent("claim", sender))
    is_seer = claims or already_claimed
    seen: set[tuple[str, int]] = set()
    at: dict[tuple[str, int], int] = {}

    def add(kind: str, seat: int, strength: float = 1.0, verdict: str | None = None) -> None:
        if seat == sender:
            return
        if (kind, seat) in at:  # said twice: keep the stronger
            i = at[(kind, seat)]
            if strength > events[i].strength:
                events[i] = ChatEvent(kind, sender, seat, verdict, strength)
            return
        at[(kind, seat)] = len(events)
        events.append(ChatEvent(kind, sender, seat, verdict, strength))

    for sent in sentences(text):
        result_sentence = is_seer and (bool(_RESULT_WORDS.search(sent)) or bool(_SEER_CLAIM.search(sent)) or claims)
        claimed_about = {int(m.group(1)) for m in _CLAIMED_BY.finditer(sent)}
        for m in _SEAT_RE.finditer(sent):
            seat = int(m.group(1))
            if seat in claimed_about:
                continue  # "Player3 is the seer": a claim about someone else, not a result
            verdict = verdict_after(sent, m.end())
            if verdict is None:
                continue
            if result_sentence:
                if seat != sender and ("result", seat) not in seen:
                    seen.add(("result", seat))
                    events.append(ChatEvent("result", sender, seat, verdict))
            elif verdict == "wolf":
                add("accuse", seat, 1.0)
            else:
                add("defend", seat, 0.8)
        for m in _VOTE_FOR.finditer(sent):
            if not m.group("neg"):
                add("accuse", int(m.group(m.lastindex)), 1.0)
        for m in _SUSPECT_VERB.finditer(sent):
            if not m.group("neg"):
                add("accuse", int(m.group(m.lastindex)), 0.6)
        for m in _SUSPECT_ADJ.finditer(sent):
            add("accuse", int(m.group(1)), 0.6)
        for m in _DEFEND_VERB.finditer(sent):
            seat = int(m.group(1))
            if seat not in claimed_about or True:
                add("defend", seat, 0.8)
    return events


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[.;!?\n]+", text.lower()) if s.strip()]


def trim(text: str, words: int = SAFE_WORDS, chars: int = MAX_CHARS) -> str:
    """Cut a message to the platform's limits (50 words, 1024 characters), on a word boundary."""
    parts = text.split()
    if len(parts) > words:
        parts = parts[:words]
    out = " ".join(parts)
    if len(out) > chars:
        out = out[:chars].rsplit(" ", 1)[0]
    return out
