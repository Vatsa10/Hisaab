"""Match shopping-list items to products and dishes (Plan 5 R4, R14). Pure: no database, no network.

Nothing is ever guessed. An item without a confident match becomes "pick" (the user chooses). An optional judge
may only confirm NEAR candidates; it is injected by the caller (wired to `llm.py` elsewhere) and never needed here.
The best-scoring candidate wins; price only breaks ties between equal scores.
"""
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Literal

SURE = 0.8
NEAR = 0.5
SHOW = 0.3
MAX_CANDIDATES = 5
JUDGE_TOP = 3
_EPS = 1e-9  # scores are sums of floats: 0.4 + 0.15 + 0.25 is 0.7999999999999999


def _is_sure(s: float) -> bool:
    return s >= SURE - _EPS


def _is_near(s: float) -> bool:
    return s >= NEAR - _EPS


def _is_shown(s: float) -> bool:
    return s >= SHOW - _EPS


STOPWORDS = frozenset({"pack", "of", "the", "and", "fresh", "x", "pcs", "pc", "each"})

# unit spelling -> (dimension, multiplier). Longest spellings first so the regex prefers them.
_UNITS = {
    "kg": ("g", 1000), "kgs": ("g", 1000), "g": ("g", 1), "gm": ("g", 1), "gms": ("g", 1), "gram": ("g", 1),
    "grams": ("g", 1), "gr": ("g", 1), "grm": ("g", 1),
    "l": ("ml", 1000), "ltr": ("ml", 1000), "ltrs": ("ml", 1000), "litre": ("ml", 1000), "litres": ("ml", 1000),
    "liter": ("ml", 1000), "liters": ("ml", 1000), "ml": ("ml", 1),
    "pc": ("pc", 1), "pcs": ("pc", 1), "piece": ("pc", 1), "pieces": ("pc", 1), "unit": ("pc", 1),
    "units": ("pc", 1), "nos": ("pc", 1), "dozen": ("pc", 12), "dozens": ("pc", 12),
}
_UNIT_RE = "|".join(sorted(map(re.escape, _UNITS), key=len, reverse=True))
_NUM = r"(\d+(?:\.\d+)?)"
_END = r"(?![a-z])"            # a unit must not run into a longer word ("1 l" yes, "1 large" no)
_START = r"(?<![\w.])"         # a number must not be the tail of another token
_MULTI_COUNT_FIRST = re.compile(rf"{_START}(\d+)\s*[x×*]\s*{_NUM}\s*({_UNIT_RE}){_END}")
_MULTI_SIZE_FIRST = re.compile(rf"{_START}{_NUM}\s*({_UNIT_RE}){_END}\s*[x×*]\s*(\d+)(?!\d)")
_SINGLE = re.compile(rf"{_START}{_NUM}\s*({_UNIT_RE}){_END}")
_PACK_OF = re.compile(r"\bpack\s+of\s+(\d+)\b")
_N_PACK = re.compile(rf"{_START}(\d+)\s*-?\s*packs?\b")
_QTY_SUFFIX = re.compile(r"\s*(?:[×*]|\bx)\s*(\d+)\s*$", re.I)

_SIZE_TOKEN = re.compile(rf"^\d+(?:x\d+)?(?:{_UNIT_RE})$")
_UNIT_WORDS = frozenset(_UNITS) | {"pack", "packs"}


@dataclass(frozen=True)
class Size:
    amount: float
    unit: Literal["g", "ml", "pc"]

    def label(self) -> str:
        amount, unit = self.amount, self.unit
        if unit == "g" and amount >= 1000:
            amount, unit = amount / 1000, "kg"
        elif unit == "ml" and amount >= 1000:
            amount, unit = amount / 1000, "L"
        return f"{amount:g} {unit}"


def _size(amount: float, unit: str, count: float = 1) -> Size:
    dim, mult = _UNITS[unit]
    return Size(float(amount) * mult * count, dim)


def _parse_size(text: str | None) -> tuple[Size | None, str | None]:
    """(size, kind): kind is "multi" (the text counts its packs), "single" (one pack) or "count" (only a
    "Pack of N" / "N pack" count, no unit size)."""
    t = (text or "").lower()
    if m := _MULTI_COUNT_FIRST.search(t):
        return _size(float(m.group(2)), m.group(3), int(m.group(1))), "multi"
    if m := _MULTI_SIZE_FIRST.search(t):
        return _size(float(m.group(1)), m.group(2), int(m.group(3))), "multi"
    pack_of = _PACK_OF.search(t)
    if m := _SINGLE.search(t):
        size = _size(float(m.group(1)), m.group(2))
        if pack_of:
            return Size(size.amount * int(pack_of.group(1)), size.unit), "multi"
        return size, "single"
    if m := _N_PACK.search(t):
        return Size(float(m.group(1)), "pc"), "count"
    if pack_of:
        return Size(float(pack_of.group(1)), "pc"), "count"
    return None, None


def parse_size(text: str) -> Size | None:
    """The pack size named in a product name or pack-size label; multipacks multiply."""
    return _parse_size(text)[0]


def _norm(text: str | None) -> str:
    """`db.norm_name`, except combining marks (Unicode Mn/Mc) stay inside words, so Hindi names do not split.
    `db.norm_name` itself is unchanged: the SQL views must keep matching it."""
    out = []
    for c in (text or "").lower().replace("'", "").replace("’", "").replace("ʼ", ""):
        keep = c.isalnum() or c == "_" or c.isspace() or unicodedata.category(c) in ("Mn", "Mc")
        out.append(c if keep else " ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _words(text: str | None) -> list[str]:
    return _norm(text).split()


def tokens(text: str | None) -> frozenset[str]:
    """Name tokens: lower-cased words without sizes, one-letter tokens and stopwords."""
    words = _words(text)
    keep = set()
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if len(w) < 2 or w in STOPWORDS or w in _UNIT_WORDS or _SIZE_TOKEN.match(w):
            continue
        if w.isdigit() and (nxt in _UNIT_WORDS or _SIZE_TOKEN.match(nxt) or nxt == "x"):
            continue  # "52 g", "6 x 300 ml": the number is part of a size
        keep.add(w)
    return frozenset(keep)


def _split_qty(text: str, qty: int) -> tuple[str, int]:
    text = (text or "").strip()
    if m := _QTY_SUFFIX.search(text):
        stripped = text[:m.start()].strip()
        if stripped:
            return stripped, qty * int(m.group(1))
    return text, qty


@dataclass(frozen=True)
class Query:
    idx: int
    text: str
    qty: int
    tokens: frozenset[str]
    size: Size | None


def parse_query(idx: int, text: str, qty: int) -> Query:
    """A list line. A trailing "×N" / "xN" / "x N" / "* N" multiplies qty and leaves the text."""
    stripped, qty = _split_qty(text, max(1, int(qty or 1)))
    return Query(idx, stripped, qty, tokens(stripped), parse_size(stripped))


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    brand: str | None
    size: Size | None
    size_label: str | None
    price: float
    mrp: float | None
    in_stock: bool
    offer: str | None = None


_OFFER = re.compile(r"\+\s*1\s*free|buy\s*1\s*get\s*1(?:\s*free)?|b1g1", re.I)


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _stock(v) -> bool:
    return True if v is None else bool(v)


def from_raw(d: dict) -> Product | None:
    """A product from a `search.extract_products` row; None without an id, a name or a price > 0."""
    name = str(d.get("name") or "").strip()
    pid = d.get("product_id")
    price = _num(d.get("price"))
    if not name or pid is None or price is None or price <= 0:
        return None
    pack = str(d.get("pack_size") or "").strip() or None
    pack_size, pack_kind = _parse_size(pack)
    name_size, name_kind = _parse_size(name)
    if pack_kind == "count" and name_kind == "single" and name_size.unit != "pc":
        size = Size(name_size.amount * pack_size.amount, name_size.unit)   # "Bread 400 g" + "Pack of 2"
    elif pack_kind == "count" and name_kind == "multi":
        size = name_size                                                    # the name already counts its packs
    else:
        size = pack_size or name_size
    mrp = _num(d.get("mrp"))
    brand = str(d.get("brand") or "").strip() or None
    return Product(str(pid), name, brand, size, pack or (size.label() if size else None), price,
                   mrp if mrp and mrp > 0 else None, _stock(d.get("in_stock")),
                   m.group(0) if (m := _OFFER.search(name)) else None)


def _ratio(a: Size, b: Size) -> float:
    return a.amount / b.amount if b.amount else math.inf


def _size_score(want: Size | None, have: Size | None) -> float:
    if want is None:
        return 1.0
    if have is None:
        return 0.2
    if have.unit != want.unit:
        return 0.0
    r = _ratio(have, want)
    if abs(r - 1) < 1e-9:
        return 1.0
    return 0.6 if 0.5 <= r <= 2 else 0.2


def _token_score(qt: frozenset[str], pt: frozenset[str], size_score: float) -> float:
    if not qt or not pt:
        return 0.0
    common = len(qt & pt)
    return 0.6 * common / len(qt) + 0.15 * common / len(pt) + 0.25 * size_score


def score(q: Query, p: Product) -> float:
    """0.6 × query coverage + 0.15 × product precision + 0.25 × size agreement."""
    return _token_score(q.tokens, tokens(p.name) | tokens(p.brand), _size_score(q.size, p.size))


# (item text, up to JUDGE_TOP product names) -> one bool per name. Injected by the caller; never required.
Judge = Callable[[str, list[str]], list[bool]]


def _packs(q: Query, p: Product) -> int:
    """Packs to buy for display: enough to cover the wanted amount when sizes are comparable, else qty."""
    if q.size and p.size and p.size.unit == q.size.unit and p.size.amount > 0:
        return max(1, math.ceil(q.size.amount * q.qty / p.size.amount - 1e-9))
    return q.qty


def _row(p: Product, packs: int | None, s: float) -> dict:
    return {"product_id": p.product_id, "name": p.name, "brand": p.brand, "size_label": p.size_label,
            "price": p.price, "mrp": p.mrp, "in_stock": p.in_stock, "packs": packs,
            "line_total": round(packs * p.price, 2) if packs is not None else None, "score": round(s, 4), **({"offer": p.offer} if p.offer else {})}


def _result(idx, status, confidence=None, product=None, packs=None, swap_from=None, candidates=()) -> dict:
    return {"idx": idx, "status": status, "confidence": confidence, "product": product, "packs": packs,
            "line_total": product["line_total"] if product and packs is not None else None,
            "swap_from": swap_from, "candidates": list(candidates)}


def _brand_key(p: Product) -> str | None:
    """The normalised brand; None without one, and then no swap happens (never guessed from the name)."""
    return _norm(p.brand) or None


def _within(size: Size | None, ref: Size | None) -> bool:
    if size is None or ref is None:
        return size is None and ref is None
    return size.unit == ref.unit and 0.8 <= _ratio(size, ref) <= 1.25


def _priced(q: Query, scored: list[tuple[Product, float]], ref: Size | None) -> list[tuple[Product, float, int]]:
    """Keep the candidates that can fill the item and how many packs each needs (R4 step 3)."""
    out = []
    for p, s in scored:
        if q.size:
            if not p.size or p.size.unit != q.size.unit or p.size.amount <= 0:
                continue
            wanted = q.size.amount * q.qty
            packs = math.ceil(wanted / p.size.amount - 1e-9)
            if packs * p.size.amount <= 1.5 * wanted + 1e-9:
                out.append((p, s, packs))
        elif _within(p.size, ref):
            out.append((p, s, q.qty))
    return out


def _best(options: list[tuple[Product, float, int]]) -> tuple[Product, float, int]:
    """The highest score wins; the cheapest line (then the lower id) only among equal scores."""
    top = max(s for _, s, _ in options)
    tied = [o for o in options if o[1] >= top - _EPS]
    return min(tied, key=lambda o: (round(o[2] * o[0].price, 2), o[0].product_id))


def match_item(q: Query, products: list[Product], judge: Judge | None = None) -> dict:
    scored = sorted(((p, score(q, p)) for p in products), key=lambda t: (-t[1], t[0].price, t[0].product_id))
    shown = [(p, s) for p, s in scored if _is_shown(s)]
    candidates = [_row(p, _packs(q, p), s) for p, s in shown[:MAX_CANDIDATES]]

    # 1. Accepted: SURE and in stock; otherwise the judge may confirm up to JUDGE_TOP NEAR ones. No judge: pick.
    confidence = "sure"
    accepted = [(p, s) for p, s in scored if p.in_stock and _is_sure(s)]
    if not accepted and judge is not None:
        near = [(p, s) for p, s in scored if p.in_stock and _is_near(s) and not _is_sure(s)][:JUDGE_TOP]
        if near:
            answers = judge(q.text, [p.name for p, _ in near])
            accepted = [ps for ps, ok in zip(near, answers) if ok is True]
            confidence = "confirmed"

    if accepted:
        # 2. Accepted products measured in different dimensions (ml vs g) are different things: ask.
        if len({p.size.unit for p, _ in accepted if p.size}) > 1:
            return _result(q.idx, "pick", candidates=candidates)
        # 3-4. Packs, then the best score (price breaks ties).
        options = _priced(q, accepted, None if q.size else accepted[0][0].size)
        if options:
            p, s, packs = _best(options)
            return _result(q.idx, "matched", confidence, _row(p, packs, s), packs, candidates=candidates)

    # 5. The exact item is out of stock. Swap only within the same named brand, dimension and ±25 % size, to a
    #    product that keeps every non-brand word of the query. Otherwise the user picks from what is in stock;
    #    with nothing in stock to show, it is out of stock.
    gone = [(p, s) for p, s in scored if not p.in_stock and _is_sure(s)]
    if gone:
        orig, orig_s = gone[0]
        brand = _brand_key(orig)
        must_keep = q.tokens - tokens(orig.brand)
        subs = [(p, s) for p, s in scored
                if p.in_stock and _is_near(s) and brand is not None and _brand_key(p) == brand
                and must_keep <= (tokens(p.name) | tokens(p.brand))
                and orig.size is not None and _within(p.size, orig.size)]
        options = _priced(q, subs, orig.size)
        orig_row = _row(orig, _packs(q, orig), orig_s)
        if options:
            p, s, packs = _best(options)
            return _result(q.idx, "swapped", None, _row(p, packs, s), packs, swap_from=orig_row,
                           candidates=candidates)
        if any(c["in_stock"] for c in candidates):
            return _result(q.idx, "pick", candidates=candidates)
        return {**_result(q.idx, "out_of_stock", candidates=candidates), "product": {**orig_row, "line_total": None}}

    # 6. Something close enough to show: the user picks. Nothing: not found.
    return _result(q.idx, "pick" if candidates else "not_found", candidates=candidates)


# --- dishes (R14) ------------------------------------------------------------------------------

VARIANTS = ("half", "full", "regular", "small", "medium", "large")
_V = "|".join(VARIANTS)
_VARIANT_PATTERNS = (
    re.compile(rf"\s*[\(\[]\s*({_V})(?:\s+(?:plate|portion|size|qty))?\s*[\)\]]", re.I),   # "(Half)", "[Large]"
    re.compile(rf"\s*[-–—|:/]\s*({_V})(?:\s+(?:plate|portion|size|qty))?\s*$", re.I),      # "- Full"
    re.compile(rf"\s*\b({_V})\s+(?:plate|portion|size|qty)\b", re.I),                      # "Half Plate"
    re.compile(rf"\s+({_V})\s*$", re.I),                                                     # "Biryani Half"
    re.compile(r"^\s*(regular|small|medium|large)\b\s*", re.I),                               # "Large Pizza"
    re.compile(r"^\s*(half|full)\b(?!\s+fry\b)\s*", re.I),                                    # "Half Tandoori"
)
_SERVES = re.compile(r"\s*[\(\[]?\s*\b(?:serves|for)\s+(\d+)(?:\s*-\s*\d+)?(?:\s+(?:people|persons|pax))?\s*[\)\]]?",
                     re.I)
_ADDON = re.compile(r"\s*(?:\bwith\b|\+|\badd\s+on\b)\s*(.+)$", re.I)
_EXTRA = re.compile(r"(?<=\S)\s+\bextra\s+(.+)$", re.I)   # "Burger Extra Cheese", not "Extra Long Hot Dog"
_ADDON_FILLER = frozenset({"extra", "with", "add", "on", "addon", "and"})
# Words that make a different dish: "Paneer Tikka" is not "Paneer Tikka Roll", nor "Chicken Biryani Rice Bowl".
HEAD_NOUNS = frozenset({
    "roll", "rolls", "wrap", "wraps", "bowl", "bowls", "combo", "combos", "thali", "thalis", "meal", "meals",
    "platter", "platters", "box", "bucket", "sandwich", "sandwiches", "burger", "burgers", "pizza", "pizzas",
    "pasta", "salad", "soup", "frankie", "kathi", "sub", "momo", "momos", "noodles", "rice", "pulao", "paratha",
    "parathas", "curry", "masala", "gravy", "kebab", "kebabs", "taco", "tacos", "quesadilla", "biryani",
})


def _variant_of(text: str | None) -> str | None:
    t = (text or "").lower()
    for v in VARIANTS:
        if re.search(rf"\b{v}\b", t):
            return v
    return None


def dish_traits(name: str) -> tuple[str, str | None, int | None, frozenset[str]]:
    """(base_name, variant, serves, addons) from a dish name such as "Chicken Biryani (Half) with Raita"."""
    rest = (name or "").strip()
    serves = None
    if m := _SERVES.search(rest):
        serves = int(m.group(1))
        rest = rest[:m.start()] + " " + rest[m.end():]
    variant = None
    for pat in _VARIANT_PATTERNS:
        if m := pat.search(rest):
            variant = m.group(1).lower()
            rest = rest[:m.start()] + " " + rest[m.end():]
            break
    addons: frozenset[str] = frozenset()
    if m := (_ADDON.search(rest) or _EXTRA.search(rest)):
        addons = frozenset(tokens(m.group(1)) - _ADDON_FILLER)
        rest = rest[:m.start()]
    base = re.sub(r"\s+", " ", rest)
    base = re.sub(r"[\s\-–—|:/,(\[]+$", "", re.sub(r"^[\s\-–—|:/,)\]]+", "", base)).strip()
    base = re.sub(r"\(\s*\)|\[\s*\]", "", base).strip()
    return base or (name or "").strip(), variant, serves, addons


@dataclass(frozen=True)
class Dish:
    dish_id: str
    name: str
    variant: str | None
    serves: int | None
    veg: bool | None
    addons: frozenset[str]
    price: float
    packaging: float | None
    in_stock: bool
    base_name: str = ""


def _veg(v) -> bool | None:
    if v is None or isinstance(v, bool):
        return v
    s = re.sub(r"[^a-z]", "", str(v).lower())
    if s in ("veg", "vegetarian", "true", "1", "yes", "v"):
        return True
    if s in ("nonveg", "nonvegetarian", "egg", "false", "0", "no", "nv"):
        return False
    return None


def dish_from_raw(d: dict) -> Dish | None:
    """A dish from a `food_search.extract_menu` row; None without an id, a name or a price > 0."""
    name = str(d.get("name") or "").strip()
    did = d.get("dish_id")
    price = _num(d.get("price"))
    if not name or did is None or price is None or price <= 0:
        return None
    base, variant, serves, addons = dish_traits(name)
    variant = _variant_of(d.get("variant")) or variant
    packaging = _num(d.get("packaging"))
    return Dish(str(did), name, variant, serves, _veg(d.get("veg")), addons, price,
                packaging if packaging is not None and packaging >= 0 else None, _stock(d.get("in_stock")), base)


@dataclass(frozen=True)
class DishQuery:
    idx: int
    text: str
    qty: int
    tokens: frozenset[str]
    variant: str | None
    serves: int | None
    veg: bool | None
    addons: frozenset[str]


def parse_dish_query(idx: int, text: str, qty: int, variant: str | None = None,
                     veg: bool | None = None) -> DishQuery:
    """A dish line. Explicit variant / veg (known when the dish was picked from a menu) win over the text."""
    stripped, qty = _split_qty(text, max(1, int(qty or 1)))
    base, parsed_variant, serves, addons = dish_traits(stripped)
    return DishQuery(idx, stripped, qty, tokens(base), _variant_of(variant) or parsed_variant, serves,
                     veg, addons)


def _conflicts(a, b) -> bool:
    return a is not None and b is not None and a != b


def _dish_score(q: DishQuery, d: Dish) -> float:
    """`score` on the base names, capped below SURE unless it is plainly the same dish: every query word is in
    it, it adds no head noun ("Roll", "Bowl", ...), its variant is known when the query's is, and the add-ons
    agree."""
    name = d.base_name or d.name
    s = score(Query(q.idx, q.text, q.qty, q.tokens, None),
              Product(d.dish_id, name, None, None, None, d.price, None, d.in_stock))
    dt = tokens(name)
    if (not q.tokens <= dt or (dt - q.tokens) & HEAD_NOUNS
            or (q.variant is not None and d.variant is None) or q.addons != d.addons):
        s = min(s, SURE - 0.01)
    if q.veg is not None and d.veg is None:
        s = min(s, SURE - 0.01)   # veg flag unknown: ask, never sure
    return s


def _dish_row(d: Dish, qty: int, s: float) -> dict:
    return {"product_id": d.dish_id, "name": d.name, "brand": None, "size_label": d.variant, "price": d.price,
            "mrp": None, "in_stock": d.in_stock, "packs": qty, "line_total": round(qty * d.price, 2),
            "score": round(s, 4), "variant": d.variant, "veg": d.veg, "packaging": d.packaging}


def match_dish(q: DishQuery, dishes: list[Dish]) -> dict:
    eligible = [d for d in dishes if not (_conflicts(q.veg, d.veg) or _conflicts(q.variant, d.variant)
                                          or _conflicts(q.serves, d.serves))]
    scored = sorted(((d, _dish_score(q, d)) for d in eligible), key=lambda t: (-t[1], t[0].price, t[0].dish_id))
    candidates = [_dish_row(d, q.qty, s) for d, s in scored if _is_shown(s)][:MAX_CANDIDATES]

    accepted = [(d, s) for d, s in scored if d.in_stock and _is_sure(s)]
    if accepted:
        if q.variant is None and len({d.variant for d, _ in accepted}) > 1:
            return _result(q.idx, "pick", candidates=candidates)
        top = max(s for _, s in accepted)
        d, s = min(((d, s) for d, s in accepted if s >= top - _EPS), key=lambda t: (t[0].price, t[0].dish_id))
        return _result(q.idx, "matched", "sure", _dish_row(d, q.qty, s), q.qty, candidates=candidates)

    gone = [(d, s) for d, s in scored if not d.in_stock and _is_sure(s)]
    if gone:
        d, s = gone[0]
        row = {**_dish_row(d, q.qty, s), "line_total": None}
        return {**_result(q.idx, "out_of_stock", candidates=candidates), "product": row}
    return _result(q.idx, "pick" if candidates else "not_found", candidates=candidates)
