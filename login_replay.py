"""Learned login scripts: the steps a successful login took up to "send OTP", replayed later with no model.

A script is generic: it names elements by role + accessible name and never holds a typed value (phone, OTP,
location) or a URL. The OTP step stays with LoginAgent's OTP detector; what comes after it stays with the agent.
"""
import re

MAX_STEPS = 8
NAME_MAX = 80
ACTIONS = {"click", "type"}
ROLES = {"button", "link", "textbox", "tab", "menuitem", "option", "generic"}
TAGS = {"button", "a", "input", "textarea", "div", "span", "li", "label", "p", "img", "svg"}
INPUT_TYPES = {None, "tel", "text", "number", "button", "submit"}
STEP_KEYS = {"action", "field", "role", "name", "tag", "input_type"}
_URLISH = re.compile(r"https?:|://|www\.|\.(com|in|net|org)\b|/", re.I)
_DIGITS = re.compile(r"\d(?:[ -]?\d){3,}")  # 4+ digits: a phone, an OTP, an id


def normalise(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip().casefold()


def role_of(el: dict) -> str:
    if el.get("role") in ROLES:
        return el["role"]
    tag = el.get("tag")
    if tag in ("input", "textarea"):
        return "button" if el.get("type") in ("button", "submit") else "textbox"
    return {"button": "button", "a": "link"}.get(tag, "generic")


def name_of(el: dict) -> str:
    """The accessible name the way a user sees it: visible text, else aria-label/title, else the placeholder."""
    return normalise(el.get("text") or el.get("label") or el.get("placeholder") or "")


def step_of(action: str, el: dict, field: str | None = None) -> dict:
    tag = el.get("tag")
    return {"action": action, "field": field, "role": role_of(el), "name": name_of(el)[:NAME_MAX],
            "tag": tag, "input_type": el.get("type") if tag in ("input", "textarea") else None}


def matches(step: dict, el: dict) -> bool:
    return (role_of(el) == step["role"] and name_of(el)[:NAME_MAX] == step["name"] and el.get("tag") == step["tag"]
            and (el.get("type") if el.get("tag") in ("input", "textarea") else None) == step["input_type"])


def derive(steps: list[dict]) -> list[dict] | None:
    """The script from the steps a login executed before the OTP prompt, ending at the click that sent it.
    None when it cannot be generic (no phone typed, a location typed, nothing clicked after the phone)."""
    if any(s["action"] == "type" and s["field"] != "PHONE" for s in steps):
        return None
    phone_at = next((i for i, s in enumerate(steps) if s["action"] == "type"), None)
    if phone_at is None:
        return None
    send_at = next((i for i in range(phone_at + 1, len(steps)) if steps[i]["action"] == "click"), None)
    if send_at is None:
        return None
    script = [dict(s) for s in steps[:send_at + 1]]
    return script if validate(script) is None else None


def validate(script) -> str | None:
    """Why a login script must not be stored or replayed, or None when it is safe."""
    from agent import LOGIN_CLICK_BLOCK  # lazy: agent imports this module
    if not isinstance(script, list) or not 2 <= len(script) <= MAX_STEPS:
        return f"needs 2 to {MAX_STEPS} steps"
    phones = 0
    for i, s in enumerate(script, 1):
        if not isinstance(s, dict) or set(s) != STEP_KEYS:
            return f"step {i}: unexpected shape"
        if s["action"] not in ACTIONS:
            return f"step {i}: action {s['action']!r} is not allowed"
        if s["action"] == "type":
            phones += 1
            if s["field"] != "PHONE":
                return f"step {i}: only the phone number may be typed"
        elif s["field"] is not None:
            return f"step {i}: a click carries no value"
        name = s["name"]
        if not isinstance(name, str) or not name or len(name) > NAME_MAX or name != normalise(name):
            return f"step {i}: name must be 1-{NAME_MAX} normalised characters"
        if _URLISH.search(name) or _DIGITS.search(name):
            return f"step {i}: name looks like a URL or a number"
        if s["role"] not in ROLES or s["tag"] not in TAGS or s["input_type"] not in INPUT_TYPES:
            return f"step {i}: unexpected element kind"
        if s["action"] == "click" and LOGIN_CLICK_BLOCK.search(name):
            return f"step {i}: the click policy refuses {name!r}"
    if phones != 1:
        return "exactly one phone step is needed"
    if script[-1]["action"] != "click":
        return "the last step must send the OTP"
    return None


def plain_words(step: dict) -> str:
    what = {"textbox": "box", "generic": "element"}.get(step["role"], step["role"])
    if step["action"] == "type":
        return f"Type the user's phone number into the {what} '{step['name']}'"
    return f"Click the {what} '{step['name']}'"


def make_recipe(script: list[dict]) -> dict:
    """The shared "login" recipe: the steps plus their plain-words summary for the approval card."""
    return {"steps": script, "summary": [plain_words(s) for s in script]}
