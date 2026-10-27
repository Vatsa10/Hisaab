"""Fee rules (Plan 5 R3): validation and fee computation for grocery and food carts.

Rules are admin-edited data in `fee_rules.rules`. Each fee component is resolved, in order, from:
  1. the value the app's own response returned (`observed`),
  2. 0 when the response says the user is a member and the membership waives that component,
  3. the rule.
"Estimated" is set when any component came from an unverified rule, when surge is possible and wasn't observed,
when the membership is defined but the response didn't say whether the user is a member, and (food) when the
distance is unknown or packaging came from the per-item rule.
Pure: no database, no I/O.
"""
from __future__ import annotations

import math

MAX_FEE = 500
MAX_BELOW = 2000
MAX_GST_PCT = 28
MAX_NAME = 40
MAX_TEXT = 500

GROCERY_COMPONENTS = ("delivery", "handling", "small_cart", "surge")
FOOD_COMPONENTS = ("delivery", "packaging", "platform", "small_order", "surge")

_GROCERY_KEYS = {"kind", "delivery", "handling", "small_cart", "surge", "membership", "source_url", "note"}
_GROCERY_REQUIRED = ("delivery", "handling")
_FOOD_KEYS = {"kind", "delivery_by_km", "packaging_per_item", "platform", "small_order", "surge", "gst", "membership",
              "source_url", "note"}
_FOOD_REQUIRED = ("kind", "delivery_by_km", "packaging_per_item", "platform", "gst")


# --- validation --------------------------------------------------------------------------------------------------

def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _num(errors: list[str], where: str, v, lo: float, hi: float) -> None:
    if not _is_num(v):
        errors.append(f"{where} must be a number")
    elif not lo <= v <= hi:
        errors.append(f"{where} must be from {lo:g} to {hi:g}")


def _keys(errors: list[str], where: str, d: dict, required: set[str], optional: set[str] = frozenset()) -> bool:
    if not isinstance(d, dict):
        errors.append(f"{where} must be an object")
        return False
    for k in sorted(set(d) - required - optional):
        errors.append(f"{where}: unknown key {k!r}")
    for k in sorted(required - set(d)):
        errors.append(f"{where}: missing {k!r}")
    return True


def _threshold(errors: list[str], where: str, v) -> None:
    """`small_cart` / `small_order`: null or {below 0..2000, fee}."""
    if v is None:
        return
    if _keys(errors, where, v, {"below", "fee"}):
        if "below" in v:
            _num(errors, f"{where}.below", v["below"], 0, MAX_BELOW)
        if "fee" in v:
            _num(errors, f"{where}.fee", v["fee"], 0, MAX_FEE)


def _surge(errors: list[str], v) -> None:
    if _keys(errors, "surge", v, {"possible"}, {"fee"}):
        if "possible" in v and not isinstance(v["possible"], bool):
            errors.append("surge.possible must be true or false")
        if "fee" in v:
            _num(errors, "surge.fee", v["fee"], 0, MAX_FEE)


def _membership(errors: list[str], v, components: tuple[str, ...]) -> None:
    if v is None:
        return
    if not _keys(errors, "membership", v, {"name", "waives"}):
        return
    name = v.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME:
        errors.append(f"membership.name must be 1 to {MAX_NAME} characters")
    waives = v.get("waives")
    if not isinstance(waives, list) or not all(isinstance(w, str) for w in waives):
        errors.append("membership.waives must be a list of fee names")
    else:
        for w in waives:
            if w not in components:
                errors.append(f"membership.waives: {w!r} is not one of {', '.join(components)}")
        if len(set(waives)) != len(waives):
            errors.append("membership.waives has duplicates")


def _text(errors: list[str], where: str, v) -> None:
    if v is not None and (not isinstance(v, str) or len(v) > MAX_TEXT):
        errors.append(f"{where} must be null or text up to {MAX_TEXT} characters")


def _common(errors: list[str], rules: dict, components: tuple[str, ...]) -> None:
    if "surge" in rules:
        _surge(errors, rules["surge"])
    if "membership" in rules:
        _membership(errors, rules["membership"], components)
    _text(errors, "source_url", rules.get("source_url"))
    if isinstance(rules.get("source_url"), str) and not rules["source_url"].startswith(("https://", "http://")):
        errors.append("source_url must start with http:// or https://")
    _text(errors, "note", rules.get("note"))


def _validate_grocery(rules: dict) -> list[str]:
    errors: list[str] = []
    _keys(errors, "rules", rules, set(_GROCERY_REQUIRED), _GROCERY_KEYS - set(_GROCERY_REQUIRED))
    tiers = rules.get("delivery")
    if "delivery" in rules:
        if not isinstance(tiers, list) or not tiers:
            errors.append("delivery must be a non-empty list of tiers")
        else:
            prev = None
            for i, t in enumerate(tiers):
                where = f"delivery[{i}]"
                if not _keys(errors, where, t, {"min_subtotal", "fee"}):
                    continue
                _num(errors, f"{where}.fee", t.get("fee"), 0, MAX_FEE)
                m = t.get("min_subtotal")
                if not _is_num(m) or m < 0:
                    errors.append(f"{where}.min_subtotal must be a number of 0 or more")
                    continue
                if i == 0 and m != 0:
                    errors.append("delivery[0].min_subtotal must be 0")
                if prev is not None and m <= prev:
                    errors.append(f"{where}.min_subtotal must be above the previous tier")
                prev = m
    if "handling" in rules:
        _num(errors, "handling", rules["handling"], 0, MAX_FEE)
    if "small_cart" in rules:
        _threshold(errors, "small_cart", rules["small_cart"])
    _common(errors, rules, GROCERY_COMPONENTS)
    return errors


def _validate_food(rules: dict) -> list[str]:
    errors: list[str] = []
    _keys(errors, "rules", rules, set(_FOOD_REQUIRED), _FOOD_KEYS - set(_FOOD_REQUIRED))
    tiers = rules.get("delivery_by_km")
    if "delivery_by_km" in rules:
        if not isinstance(tiers, list) or not tiers:
            errors.append("delivery_by_km must be a non-empty list of tiers")
        else:
            prev = None
            for i, t in enumerate(tiers):
                where = f"delivery_by_km[{i}]"
                if not _keys(errors, where, t, {"max_km", "fee"}):
                    continue
                _num(errors, f"{where}.fee", t.get("fee"), 0, MAX_FEE)
                k = t.get("max_km")
                last = i == len(tiers) - 1
                if k is None:
                    if not last:
                        errors.append(f"{where}.max_km may be null only on the last tier")
                    continue
                if last:
                    errors.append(f"{where}.max_km must be null on the last tier")
                if not _is_num(k) or k <= 0:
                    errors.append(f"{where}.max_km must be a number above 0")
                    continue
                if prev is not None and k <= prev:
                    errors.append(f"{where}.max_km must be above the previous tier")
                prev = k
    for key in ("packaging_per_item", "platform"):
        if key in rules:
            _num(errors, key, rules[key], 0, MAX_FEE)
    if "small_order" in rules:
        _threshold(errors, "small_order", rules["small_order"])
    if "gst" in rules and _keys(errors, "gst", rules["gst"], {"food_pct", "fees_pct"}):
        for key in ("food_pct", "fees_pct"):
            if key in rules["gst"]:
                _num(errors, f"gst.{key}", rules["gst"][key], 0, MAX_GST_PCT)
    _common(errors, rules, FOOD_COMPONENTS)
    return errors


def validate(rules: dict) -> list[str]:
    """Every problem with `rules`, or [] when valid. Dispatches on `rules.get("kind", "grocery")`."""
    if not isinstance(rules, dict):
        return ["rules must be an object"]
    kind = rules.get("kind", "grocery")
    if kind == "grocery":
        return _validate_grocery(rules)
    if kind == "food":
        return _validate_food(rules)
    return [f"kind must be 'grocery' or 'food', not {kind!r}"]


# --- computation -------------------------------------------------------------------------------------------------

def _money(x: float) -> float:
    return round(float(x) + 0.0, 2)


def _observed_num(observed: dict, key: str) -> float | None:
    v = observed.get(key)
    return float(v) if _is_num(v) else None


def _member(rules: dict, observed: dict) -> tuple[str | None, frozenset[str], bool]:
    """(applied membership name, waived components, membership defined but unknown)."""
    plan = rules.get("membership")
    flag = observed.get("member")
    if not plan:
        return None, frozenset(), False
    if flag is None:
        return None, frozenset(), True
    if not flag:
        return None, frozenset(), False
    name = flag.strip()[:MAX_NAME] if isinstance(flag, str) and flag.strip() else plan["name"]
    return name, frozenset(plan.get("waives") or ()), False


def _resolve(observed: dict, waived: frozenset[str], component: str, rule_value: float) -> tuple[float, str]:
    seen = _observed_num(observed, component)
    if seen is not None:
        return seen, "observed"
    if component in waived:
        return 0.0, "waived"
    return float(rule_value), "rule"


def _surge_possible(rules: dict) -> bool:
    return bool((rules.get("surge") or {}).get("possible"))


def compute(rules: dict, subtotal: float, verified: bool, observed: dict | None = None) -> dict:
    """Grocery fees for one app's cart: {delivery, handling, small_cart, surge, total, estimated, member}."""
    if not subtotal or subtotal <= 0:
        return {**{c: 0 for c in GROCERY_COMPONENTS}, "total": 0, "estimated": False, "member": None}
    observed = observed or {}
    name, waived, unknown_member = _member(rules, observed)
    tiers = [t for t in rules["delivery"] if t["min_subtotal"] <= subtotal]
    small = rules.get("small_cart")
    rule_values = {
        "delivery": max(tiers, key=lambda t: t["min_subtotal"])["fee"] if tiers else rules["delivery"][0]["fee"],
        "handling": rules.get("handling") or 0,
        "small_cart": small["fee"] if small and subtotal < small["below"] else 0,
        # A surge is never assumed; a possible surge only marks the total as estimated.
        "surge": 0,
    }
    out: dict = {}
    estimated = unknown_member
    for c in GROCERY_COMPONENTS:
        value, source = _resolve(observed, waived, c, rule_values[c])
        out[c] = _money(value)
        if source == "rule" and not verified:
            estimated = True
    if _surge_possible(rules) and _observed_num(observed, "surge") is None:
        estimated = True
    out["total"] = _money(sum(out[c] for c in GROCERY_COMPONENTS))
    out["estimated"] = estimated
    out["member"] = name
    return out


def compute_food(rules: dict, subtotal: float, units: int, distance_km: float | None, verified: bool,
                 observed: dict | None = None) -> dict:
    """Food fees for one app's order: {delivery, packaging, platform, small_order, surge, gst, total, estimated,
    member}. GST = food_pct % of (items + packaging) + fees_pct % of (delivery + platform + small_order + surge)."""
    if not subtotal or subtotal <= 0:
        return {**{c: 0 for c in FOOD_COMPONENTS}, "gst": 0, "total": 0, "estimated": False, "member": None}
    observed = observed or {}
    name, waived, unknown_member = _member(rules, observed)
    tiers = rules["delivery_by_km"]
    known_distance = _is_num(distance_km)
    if known_distance:
        tier = next((t for t in tiers if t["max_km"] is None or t["max_km"] >= distance_km), tiers[-1])
    else:
        tier = tiers[-1]
    small = rules.get("small_order")
    rule_values = {
        "delivery": tier["fee"],
        "packaging": (rules.get("packaging_per_item") or 0) * max(int(units or 0), 0),
        "platform": rules.get("platform") or 0,
        "small_order": small["fee"] if small and subtotal < small["below"] else 0,
        "surge": 0,
    }
    out: dict = {}
    estimated = unknown_member
    for c in FOOD_COMPONENTS:
        value, source = _resolve(observed, waived, c, rule_values[c])
        out[c] = _money(value)
        if source != "rule":
            continue
        if not verified:
            estimated = True
        if c == "delivery" and not known_distance:
            estimated = True
        if c == "packaging":                       # the per-item rule is only an approximation of the menu's
            estimated = True
    if _surge_possible(rules) and _observed_num(observed, "surge") is None:
        estimated = True
    seen_gst = _observed_num(observed, "gst")
    if seen_gst is not None:
        out["gst"] = _money(seen_gst)
    else:
        gst = rules["gst"]
        food_base = subtotal + out["packaging"]
        fee_base = out["delivery"] + out["platform"] + out["small_order"] + out["surge"]
        out["gst"] = _money(gst["food_pct"] / 100 * food_base + gst["fees_pct"] / 100 * fee_base)
        if not verified:
            estimated = True
    out["total"] = _money(sum(out[c] for c in FOOD_COMPONENTS) + out["gst"])
    out["estimated"] = estimated
    out["member"] = name
    return out
