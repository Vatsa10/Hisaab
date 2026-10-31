"""Is this the same place on two apps? A scored matcher (classic string and geo algorithms, no LLM). Display only:
pricing always uses each app's own address_id. Mirrored rule for rule in web/lib/addresses.ts; both run the
vectors in tests/fixtures/address_pairs.json. Nothing here logs an address or a coordinate."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from addresses import distance_m

THRESHOLD = 0.75
NEAR_M, FAR_M = 30.0, 250.0  # geo score: 1 up to NEAR_M, linear down to 0 at FAR_M
PIN_VETO_FREE_M = 50.0
W_GEO, W_TEXT, PIN_BONUS, UNIT_BONUS = 0.45, 0.55, 0.15, 0.4

ABBREV = {"soc": "society", "apt": "apartment", "apts": "apartments", "flt": "flat", "nr": "near", "opp": "opposite",
          "rd": "road", "st": "street", "sec": "sector", "ph": "phase", "blk": "block", "bldg": "building",
          "no": "number", "hno": "house number"}
PLACES = frozenset("india karnataka gujarat maharashtra delhi telangana tamil nadu kerala rajasthan haryana "
                   "uttar pradesh punjab bengal bengaluru bangalore vadodara baroda mumbai pune hyderabad chennai "
                   "kolkata ahmedabad surat gurgaon gurugram noida kochi jaipur".split())
FILLER = frozenset("flat house number tower block wing near opposite the".split())
GROUP_WORDS = frozenset("tower block wing".split())
AREA_WORDS = frozenset("road street main cross lane sector phase layout nagar area".split())
_PIN = re.compile(r"^[1-9]\d{5}$")
_NUM = re.compile(r"^\d{1,5}[a-z]?$")


def _tokens(text: object) -> list[str]:
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    s = re.sub("[‘’'`´]", "", s)  # apostrophes join, not split
    out: list[str] = []
    for t in re.sub(r"[^0-9a-z]+", " ", s).split():
        out.extend(ABBREV.get(t, t).split())
    return out


def parse(*parts: object) -> dict[str, Any]:
    """{pin, unit, tokens, name} for the address text parts (line, area, city)."""
    toks = _tokens(" ".join(str(p) for p in parts if p))
    pin = next((t for t in toks if _PIN.match(t)), None)
    toks = [t for t in toks if t != pin]
    number = None
    for i, t in enumerate(toks[:-1]):
        if t in ("flat", "number") and _NUM.match(toks[i + 1]):
            number = i + 1
            break
    if number is None:
        number = next((i for i, t in enumerate(toks) if _NUM.match(t)), None)
    tower, used = None, set()
    for i, t in enumerate(toks):
        if t in GROUP_WORDS:
            for j in (i + 1, i - 1):
                if 0 <= j < len(toks) and len(toks[j]) <= 2 and toks[j] not in FILLER and j != number:
                    tower = toks[j]
                    used.add(j)
                    break
            if tower:
                break
    if tower is None and number is not None and number > 0 and len(toks[number - 1]) == 1 and toks[number - 1].isalpha():
        tower = toks[number - 1]
        used.add(number - 1)
    unit = (tower or "") + toks[number] if number is not None else None
    if number is not None:
        used.add(number)
    rest = [t for i, t in enumerate(toks) if i not in used and t not in PLACES and t not in FILLER]
    name = [t for t in rest if t not in AREA_WORDS]
    return {"pin": pin, "unit": unit, "tokens": rest, "name": " ".join(name)}


def levenshtein_ratio(a: str, b: str) -> float:
    if not a and not b:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return 1 - prev[-1] / max(len(a), len(b))


def _pin_of(r: dict) -> tuple[float, float] | None:
    return (r["lat"], r["lon"]) if r.get("lat") is not None and r.get("lon") is not None else None


def _parsed(r: dict) -> dict[str, Any]:
    return parse(r.get("line"), r.get("area"), r.get("city"))


def score(a: dict, b: dict) -> float:
    """How likely two saved addresses (dicts with line, area, city, lat, lon) are one place; 0 on a veto."""
    pa, pb = _parsed(a), _parsed(b)
    ga, gb = _pin_of(a), _pin_of(b)
    d = distance_m(ga, gb) if ga and gb else None
    if pa["unit"] and pb["unit"] and pa["unit"] != pb["unit"]:
        return 0.0  # different flats are different places, however close
    if pa["pin"] and pb["pin"] and pa["pin"] != pb["pin"] and not (d is not None and d <= PIN_VETO_FREE_M):
        return 0.0
    sa, sb = set(pa["tokens"]), set(pb["tokens"])
    if sa and sb:
        text = 0.5 * (len(sa & sb) / len(sa | sb)) + 0.5 * levenshtein_ratio(pa["name"], pb["name"])
    else:
        text = 0.0
    if d is None:
        total = text
    else:
        geo = 1.0 if d <= NEAR_M else 0.0 if d >= FAR_M else (FAR_M - d) / (FAR_M - NEAR_M)
        total = W_GEO * geo + W_TEXT * text
    if pa["pin"] and pa["pin"] == pb["pin"]:
        total += PIN_BONUS
    if pa["unit"] and pa["unit"] == pb["unit"]:
        total += UNIT_BONUS
    return total


def same_place(a: dict, b: dict) -> bool:
    return score(a, b) >= THRESHOLD


def group(rows: list[dict]) -> list[list[dict]]:
    """Display groups: union-find over pairs scoring >= THRESHOLD, best pair first, never two rows of one app in a
    group. `rows` come in display order (defaults first). Each group is led by its default address, else its
    highest-scoring member; the rest keep their order."""
    n = len(rows)
    parent = list(range(n))
    members: dict[int, list[int]] = {i: [i] for i in range(n)}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    total = [0.0] * n
    scored = [(score(rows[i], rows[j]), i, j) for i in range(n) for j in range(i + 1, n)]
    for s, i, j in sorted((p for p in scored if p[0] >= THRESHOLD), key=lambda p: (-p[0], p[1], p[2])):
        ri, rj = find(i), find(j)
        if ri == rj or {rows[k]["site"] for k in members[ri]} & {rows[k]["site"] for k in members[rj]}:
            continue
        parent[rj] = ri
        members[ri] += members.pop(rj)
        total[i] += s
        total[j] += s
    out = []
    for g in sorted((sorted(m) for m in members.values()), key=lambda m: m[0]):
        lead = next((k for k in g if rows[k].get("is_default")), None)
        if lead is None:
            lead = max(g, key=lambda k: (total[k], -k))
        out.append([rows[lead]] + [rows[k] for k in g if k != lead])
    return out
