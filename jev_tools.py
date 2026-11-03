"""Small, fast Jev judgments (TypeSafe System One, about half a second each) for places where code used to ask Claude
or guess with text rules. Every function returns None when Jev is unavailable or unsure, so callers keep their
existing (slower or stricter) path and a failure here never accepts a product or fails a job."""
import logging

import agent
import metering

log = logging.getLogger("jev_tools")

SURE_YES = 0.8  # a probability at or above this counts as yes ...
SURE_NO = 0.2   # ... at or below this as no; in between is "unsure"
THIN_PAGE_CHARS = 400  # a page with less visible text than this may be an interstitial, not real content
TIMEOUT_S = 6


def _verdict(p) -> bool | None:
    if p is None:
        return None
    return True if p >= SURE_YES else False if p <= SURE_NO else None


def _ask(state: dict, questions: dict) -> dict | None:
    """One Jev call -> {question name: probability}, or None when Jev is off or the call fails."""
    client = agent.jev()
    if client is None:
        return None
    metering.count_jev()  # counted when made: a failed call may still be billed
    try:
        answers = client.system_one(state=state, questions=questions, timeout=TIMEOUT_S).answers
        return {name: answers[name].noul for name in questions}
    except Exception as e:  # never log the state: it holds product names or page text
        log.warning("Jev judgment failed (%s)", type(e).__name__)
        return None


def judge_match(text: str, names: list[str]) -> list[bool | None] | None:
    """For each product name: is it the same item the shopping-list line asks for (same kind, the brand if one is
    named, and the size)? One call for all of them. None when Jev is unavailable; an entry is None when unsure."""
    if not names:
        return []
    from typesafe_sdk import Noul
    questions = {f"p{i}": Noul(instructions=(
        f'The shopping-list line is the text in "shopping_list_line". Is the product "{n}" a good match for what the '
        "line asks for: the same kind of item, the same brand if the line names one, and the same size or pack count "
        "if the line names one? Spelling, spacing, abbreviations and word order do not matter (500g = 500 g). A "
        "different flavour, variant, brand or size than the one named is NOT a match.")) for i, n in enumerate(names)}
    probs = _ask({"shopping_list_line": text}, questions)
    if probs is None:
        return None
    return [_verdict(probs[f"p{i}"]) for i in range(len(names))]


def page_is_challenge(title: str, text: str) -> bool | None:
    """Is this page a bot check, captcha, 'access denied' or 'verify you are human' screen rather than the site's real
    content? Only worth asking for a page the text rules passed but that has almost no content."""
    from typesafe_sdk import Noul
    probs = _ask({"page_title": (title or "")[:200], "visible_text": (text or "")[:THIN_PAGE_CHARS]},
                 {"challenge": Noul(instructions=(
                     "Is this page a bot check, captcha, 'access denied', rate-limit or 'verify you are human' screen "
                     "instead of the site's real content?"))})
    return None if probs is None else _verdict(probs["challenge"])
