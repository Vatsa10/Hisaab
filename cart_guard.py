"""Cart-mode request guard (Plan 6 R1): the only write path to a delivery app, and it is add-only.

Rule order (decide_cart):
  1. a method outside GET/HEAD/OPTIONS/POST is blocked (the effective method: override headers and `_method`
     params count, and can never turn a POST into a safe method);
  2. a forbidden category (URL path+query decoded up to 3 times, the host on navigations and writes, GraphQL
     operation names, or a removal-shaped body) is blocked; bodies are scanned raw and decoded (JSON escapes,
     JSON inside JSON strings, form-urlencoded);
  3. a body that can't be decoded on a non-GET is blocked, unless it goes to an exact add entry declared binary;
  4. a bot-challenge POST is allowed (AWS WAF host, or a Cloudflare challenge *path*);
  5. a safe method is allowed;
  6. a POST is allowed only through the cart, location or read allowlist; each regex is matched as ^(?:regex)$.
Forbidden beats every allowlist, so even a broad approved regex can't reach checkout, payment or removal.
The read-only guard.decide / guard.Guard are separate and unchanged; connect, sync and search never build this.

Residual risk, accepted: route.continue_() follows a redirect without routing the next hop. CartGuard records
every 30x answer to a non-GET request in `redirected_writes`; the fill engine aborts when it is non-empty.
"""
import functools
import json
import logging
import random
import re
import string
from dataclasses import dataclass, replace
from re import _constants as sre_c
from re import _parser as sre_parse
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from guard import MUTATION, SAFE_METHODS, redact_url

log = logging.getLogger("cart_guard")

# Searched (re.I) in the URL path+query and in the body's GraphQL operation name(s). Patterns verbatim from the plan.
FORBIDDEN: dict[str, re.Pattern] = {
    "checkout": re.compile(r"check-?out", re.I),
    "payment": re.compile(r"payment|(?<![a-z])pay(?![a-z])|pay-?now|razorpay|juspay|cashfree|(?<![a-z])upi(?![a-z])",
                          re.I),
    "order_placement": re.compile(
        r"place-?order|placeorder|order/(create|confirm|place|submit)|(create|confirm|submit)[-_]?order", re.I),
    "address_write": re.compile(
        r"(add|create|new|save|update|edit|delete|remove)[-_]?(user[-_]?)?address"
        r"|address(es)?/(add|create|new|save|update|edit|delete|remove)", re.I),
    "coupon": re.compile(r"coupon|promo|voucher|apply-?offer|offers?/apply", re.I),
    "wallet": re.compile(r"wallet|gift-?card|(?<![a-z])money(?![a-z])|cashback|super-?cash", re.I),
    "cart_removal": re.compile(
        r"(remove|delete|clear|empty|reset|decrement)[-_/]?(from[-_])?(cart|items?|products?)"
        r"|cart/(remove|delete|clear|empty|reset|decrement)", re.I),
}
def _path_word(word: str) -> re.Pattern:
    """A word in a path, in any case, as a whole word or a camelCase segment: it starts after a non-letter, after
    a lowercase letter with a capital (newOrder, newORDER), or after capitals as Capital+lowercase (NEWOrder).
    The singular must end at a non-lowercase character ("orderly" doesn't count); the plural may run on
    ("ordersv2"). "reorder", "border" and "recorder" don't count."""
    first, rest = word[0], word[1:]
    return re.compile(
        rf"(?:(?<![A-Za-z])[{first}{first.upper()}]|(?<=[a-z]){first.upper()}|(?<=[A-Z]){first.upper()}(?={rest[0]}))"
        rf"(?i:{rest})(?:[sS]|(?![a-z]))")


# Writes only, both: Hisaab never writes to orders or purchases. GETs stay safe, so order-history reads still work.
# /orders, /orders/new, /v2/order/initiate, newOrder, NEWOrder, orderId, ordersv2; /purchase, newPurchase.
ORDER_PATH = _path_word("order")
PURCHASE_PATH = _path_word("purchase")
# Encoded '/', '\' or '.' in a path (also double-encoded), and other odd encodings (%u...., overlong UTF-8 %c0 %c1
# %e0%80, a bare %%): refused on every non-GET, and never a challenge path.
_ENCODED_SEP = re.compile(r"%(25)*(2f|5c|2e)|%u|%c0|%c1|%e0%80|%%", re.I)
# Navigations only, as address_write: address pages are where addresses get added and edited.
ADDRESS_PAGE = re.compile(r"address", re.I)
# Searched in POST bodies, as cart_removal: a zero/negative quantity, or a remove-like action field.
# The plan's pattern, widened to 0.0 / 0e0 / -1e0 / " 0" / null / false, the newQuantity / itemQuantity spellings
# and negative deltas. Any quantity-like key whose value is not a plain integer from 1 to 99 (bare or quoted) is
# removal too: "+0", "0x0", 0.4, 2.0, "02", [[0]], {"value": 1}, true. A second layer: cart entries with a qty_path
# also require that value to be a plain integer from 1 to 99.
_QTY_KEYS = (r'"(quantity|qty|count|itemCount|item_count|newQuantity|new_quantity|newQty|new_qty|itemQuantity'
             r'|item_quantity)"')
REMOVE_BODY = re.compile(
    _QTY_KEYS + r'\s*:\s*"?\s*(-?0+(\.0+)?(e[-+]?\d+)?|-[\d.]+(e[-+]?\d+)?|null|false)\s*"?\s*[,}\]]'
    r'|' + _QTY_KEYS + r'\s*:(?!\s*(?:[1-9][0-9]?|"[1-9][0-9]?")\s*[,}\]])'
    r'|"(delta|change|step|diff|\w*quantity_?(delta|change)|\w*qty_?(delta|change))"\s*:\s*"?\s*-'
    r'|"(action|op|operation|type|event|operationName)"\s*:\s*'
    r'"[^"]*(remove|delete|clear|empty|decrement|reset|decrease|subtract|minus)[^"]*"',
    re.I)
# A quantity wrapped in a list ("qty": [0]) is a removal too.
_REMOVE_LIST_QTY = re.compile(
    _QTY_KEYS + r'\s*:\s*\[\s*"?\s*(-?0+(\.0+)?|-[\d.]+|null|false)\s*"?\s*[,\]]', re.I)
_QTY_NUMBER = re.compile(r"[1-9][0-9]?")
MAX_QTY = 99
HEAVY_RESOURCE_TYPES = frozenset({"image", "media", "font"})  # as Plan 4's guard.HEAVY_RESOURCE_TYPES
POST_METHODS = SAFE_METHODS | {"POST"}
OVERRIDE_HEADERS = frozenset({"x-http-method-override", "x-method-override", "x-http-method"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Field values that name what a request does; scanned like GraphQL operation names ("op": "applyCoupon").
_OP_NAME_FIELD = re.compile(r'"(?:operationName|operation|action|op)"\s*:\s*"([^"]*)"', re.I)
_REMOVE_WORD = re.compile(r"remove|delete|clear|empty|decrement|reset|decrease|subtract|minus", re.I)
_GQL_OP = re.compile(r"\b(?:query|mutation|subscription)\s+([_A-Za-z]\w*)")
_AWSWAF_HOST = re.compile(r"(?:^|\.)awswaf\.com$", re.I)
_CF_CHALLENGE_PATH = "/cdn-cgi/challenge-platform/"
_MAX_DECODES = 3
_MAX_WILDCARD = 40  # a repeat of a class that can match "/" or "?" may span at most this many characters


@dataclass(frozen=True)
class CartPolicy:
    """Approved POST entries {url_regex, why, op, operation?, binary?} for adding, selecting a location, reading."""
    cart: tuple[dict, ...] = ()
    location: tuple = ()
    read: tuple = ()


# --- body decoding -------------------------------------------------------------------------------

def _json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _form(text: str) -> list[tuple[str, str]]:
    if "=" not in text or text.lstrip()[:1] in ("{", "["):
        return []
    try:
        return parse_qsl(text, keep_blank_values=True)
    except ValueError:
        return []


# One multipart part, found by its Content-Disposition header rather than by a boundary, so a preamble (or a fake
# boundary line in it) can't hide parts. name*= (RFC 2231, e.g. name*=utf-8''quantity) counts as name=.
_MULTIPART_PART = re.compile(
    r'content-disposition:[^\r\n]*?(?<![\w-])name(\*?)\s*=\s*(?:"([^"\r\n]*)"|([^;\s]*))[^\r\n]*'
    r'(?:\r?\n[^\r\n]+)*\r?\n\r?\n(.*?)(?=\r?\n--|\Z)', re.I | re.S)


def _multipart(text: str) -> list[tuple[str, str]]:
    """(name, value) for each part of a multipart/form-data body."""
    if "content-disposition" not in text.lower():
        return []
    pairs = []
    for star, quoted, bare, value in _MULTIPART_PART.findall(text):
        name = quoted if quoted or not bare else bare
        if star:
            name = unquote(name.split("'", 2)[-1])
        pairs.append((name, value))
    return pairs


_CD_HEADER = re.compile(r"content-disposition\s*:", re.I)
_NAME_PARAM = re.compile(r"(?<![\w-])name\*?\s*=", re.I)


def _ambiguous_multipart(text: str) -> bool:
    """A multipart body (not JSON) that servers could read differently from us: a Content-Disposition our parser
    didn't turn into a part (e.g. "Content-Disposition :"), a header with two name parameters, a folded header
    line, or a field name repeated with different values. Such a body is refused, not guessed at."""
    if _json(text) is not None or "content-disposition" not in text.lower():
        return False
    pairs = _multipart(text)
    if len(_CD_HEADER.findall(text)) != len(pairs):
        return True
    for m in _CD_HEADER.finditer(text):
        start = text.rfind("\n", 0, m.start()) + 1
        ends = [i for i in (text.find("\r\n\r\n", start), text.find("\n\n", start)) if i != -1]
        block = text[start:min(ends) if ends else len(text)]
        lines = block.replace("\r\n", "\n").split("\n")
        if len(_NAME_PARAM.findall(lines[0])) != 1 or any(ln[:1] in (" ", "\t") for ln in lines[1:]):
            return True
    seen: dict[str, str] = {}
    for name, value in pairs:
        if seen.setdefault(name, value) != value:
            return True
    return False


def _fields(text: str) -> list[tuple[str, str]]:
    """Form fields: multipart parts when the text carries Content-Disposition headers, else urlencoded pairs."""
    return _multipart(text) or _form(text)


def _structured(body: str | None):
    """The body as JSON, else as a dict of form or multipart fields (last value wins), else None."""
    if not body:
        return None
    data = _json(body)
    if data is not None:
        return data
    pairs = _fields(body)
    return dict(pairs) if pairs else None


def _nested_json(data, depth: int = 0) -> list:
    """JSON documents carried inside string values (e.g. GraphQL "variables" sent as a string)."""
    if depth >= 3:
        return []
    found = []
    values = data.values() if isinstance(data, dict) else data if isinstance(data, list) else ()
    for v in values:
        if isinstance(v, str) and v.lstrip()[:1] in ("{", "["):
            inner = _json(v)
            if inner is not None:
                found += [inner, *_nested_json(inner, depth + 1)]
        elif isinstance(v, (dict, list)):
            found += _nested_json(v, depth + 1)
    return found


@functools.lru_cache(maxsize=512)
def _texts(body: str | None) -> tuple[str, ...]:
    """The body raw plus every decoded view of it, for the forbidden and mutation scans."""
    if not body:
        return ()
    texts = [body]
    data = _json(body)
    if data is None:
        pairs = _fields(body)
        if pairs:
            data = [{k: v} for k, v in pairs]
            texts += [v for _, v in pairs]
    if data is not None:
        for doc in (data, *_nested_json(data)):
            texts.append(json.dumps(doc, ensure_ascii=False))
    return tuple(texts)


def operation_name(body: str | None) -> str | None:
    """The one GraphQL operation a body names (operationName, else the name in its query text), or None.
    A batch of operations, or an anonymous one, has no single name. JSON and form bodies are understood."""
    data = _structured(body)
    if not isinstance(data, dict):
        return None
    name = data.get("operationName")
    if isinstance(name, str) and name:
        return name
    query = data.get("query")
    if isinstance(query, str):
        names = _GQL_OP.findall(query)
        if len(names) == 1:
            return names[0]
    return None


def _operation_names(body: str | None) -> list[str]:
    """Every operation name anywhere in a body, batched or not, for the forbidden scan (not for matching)."""
    names = []
    for t in _texts(body):
        names += _OP_NAME_FIELD.findall(t) + _GQL_OP.findall(t)
    return names


def _decodings(text: str) -> set[str]:
    """text and its percent-decodings, repeated until stable (at most 3 times), against double encoding."""
    out = {text}
    for _ in range(_MAX_DECODES):
        decoded = unquote(text)
        if decoded == text:
            break
        out.add(decoded)
        text = decoded
    return out


def forbidden_category(url: str, body: str | None, is_nav: bool, *, write: bool = False) -> str | None:
    """The forbidden category a request falls in, or None. The host is searched on navigations and writes."""
    p = urlsplit(url)
    targets = _decodings(p.path + ("?" + p.query if p.query else ""))
    if is_nav or write:
        targets.add(p.hostname or "")
    for cat, pat in FORBIDDEN.items():
        if any(pat.search(t) for t in targets):
            return cat
    if write:
        paths = _decodings(p.path)
        if any(ORDER_PATH.search(t) for t in paths):
            return "order"
        if any(PURCHASE_PATH.search(t) for t in paths):
            return "purchase"
    for name in _operation_names(body):
        for cat, pat in FORBIDDEN.items():
            if pat.search(name):
                return cat
        # REMOVE_BODY's operationName rule, applied to names read from the query text too ("RemoveFromCart")
        if _REMOVE_WORD.search(name):
            return "cart_removal"
    if is_nav and any(ADDRESS_PAGE.search(t) for t in targets):
        return "address_write"
    # A write's query string gets the same removal and quantity rules as its body; a GET's doesn't (it is safe).
    sources = [body, p.query] if write and p.query else [body]
    for src in sources:
        if src is not body:
            for name in _operation_names(src):
                if _REMOVE_WORD.search(name):
                    return "cart_removal"
        if any(REMOVE_BODY.search(t) or _REMOVE_LIST_QTY.search(t) for t in _texts(src)) or _bad_quantity(src):
            return "cart_removal"
    return None


# Quantity-like keys, matched by substring after normalising: "cartQuantity", "units", "quantity ", "quantity[]",
# "items[0][quantity]". "country" and "account" contain "count" but are not quantities.
_QTY_KEY = re.compile(r"qty|quantity|count|units")
_NOT_QTY_WORDS = re.compile(r"country|account")


def _qty_key(key) -> bool:
    k = re.sub(r"\[\d*\]", "", str(key)).strip().lower()
    k = re.sub(r"[^a-z]", "", k)
    return bool(_QTY_KEY.search(_NOT_QTY_WORDS.sub("", k)))


class _Pairs(list):
    """A JSON object kept as its (key, value) pairs, so a duplicated key can't hide a value."""


def _pairs_json(text: str):
    try:
        return json.loads(text, object_pairs_hook=_Pairs)
    except (TypeError, ValueError, RecursionError):
        return None


def _bad_in(data, depth: int = 0) -> bool:
    if depth > 32:
        return True
    if isinstance(data, _Pairs):
        items = list(data)
    elif isinstance(data, list):
        return any(_bad_in(v, depth + 1) for v in data)
    elif isinstance(data, str) and data.lstrip()[:1] in ("{", "["):
        inner = _pairs_json(data)
        return inner is not None and _bad_in(inner, depth + 1)
    else:
        return False
    for k, v in items:
        if _qty_key(k) and not _adds_at_least_one(v):
            return True
        if _bad_in(v, depth + 1):
            return True
    return False


@functools.lru_cache(maxsize=512)
def _bad_quantity(text: str | None) -> bool:
    """Any quantity-like key, anywhere in a JSON, form or multipart body (or a query string), whose value isn't a
    plain integer from 1 to 99."""
    if not text:
        return False
    data = _pairs_json(text)
    if data is None:
        pairs = _fields(text)
        data = _Pairs(pairs) if pairs else None
    return data is not None and _bad_in(data)


def effective_method(method: str, url: str, body: str | None, headers: dict | None = None) -> str:
    """The method the server will act on: an override header or `_method` param that names a method outside
    GET/HEAD/OPTIONS/POST wins. An override never downgrades a POST to a safe method."""
    method = method.upper()
    overrides = [v for k, v in (headers or {}).items() if k.lower() in OVERRIDE_HEADERS]
    overrides += [v for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True) if k.lower() == "_method"]
    data = _structured(body)
    if isinstance(data, dict):
        overrides += [v for k, v in data.items() if str(k).lower() == "_method"]
    for o in overrides:
        o = str(o or "").strip().upper()
        if o and o not in POST_METHODS:
            return o
    return method


def _fully_decoded(text: str) -> str:
    for _ in range(_MAX_DECODES):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def _encoded_separator(url: str) -> bool:
    """An encoded '/', '\\' or '.' (%2f, %5c, %2e, double-encoded too) or a raw backslash in the URL's path."""
    path = urlsplit(url).path
    return bool(_ENCODED_SEP.search(path)) or "\\" in path


def _dot_dot_segment(url: str) -> bool:
    """A path segment that is `..` once a `;` and everything after it is stripped (`..`, `..;`, `..;x`)."""
    return any(seg.split(";")[0] == ".." for seg in urlsplit(url).path.split("/"))


def _challenge(url: str) -> bool:
    """An AWS WAF host, or a Cloudflare challenge path checked decoded: no dot segments, encoded slashes or
    backslashes, so `/cdn-cgi/challenge-platform/../../api/cart` can't pose as a challenge."""
    p = urlsplit(url)
    if p.scheme != "https" or p.username or p.password:
        return False
    if _AWSWAF_HOST.search(p.hostname or ""):
        return True
    if _encoded_separator(url):
        return False
    path = _fully_decoded(p.path)
    if "\\" in path or any(seg.split(";")[0] in (".", "..") for seg in path.split("/")):
        return False
    return path.startswith(_CF_CHALLENGE_PATH)


def _value_at(url: str, body: str | None, path: str):
    """The value at a recipe `*_path` ("query:<name>" or a dotted body path with list indices), or None."""
    if path.startswith("query:"):
        name = path.removeprefix("query:")
        values = [v for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True) if k == name]
        return values[-1] if values else None
    obj = _structured(body)
    for part in re.sub(r"\[(-?\d+)\]", r".\1", path).split("."):
        if isinstance(obj, list) and part.lstrip("-").isdigit() and -len(obj) <= int(part) < len(obj):
            obj = obj[int(part)]
        elif isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return None
    return obj


def _adds_at_least_one(value) -> bool:
    """A quantity that adds: an int, or a plain ASCII digit string without a leading zero, from 1 to MAX_QTY.
    Floats (even 2.0), null, booleans, exponents, padding, non-ASCII digits and anything else fail closed."""
    if isinstance(value, bool):
        return False
    if isinstance(value, str) and _QTY_NUMBER.fullmatch(value):
        value = int(value)
    if isinstance(value, int):
        return 1 <= value <= MAX_QTY
    return False


def _unreadable(method: str, body: str | None, headers: dict | None) -> bool:
    """post_data None is not proof of an empty body: Playwright drops Blob/File parts and bodies Chrome omits.
    On a non-GET it counts as unreadable when a Content-Type or a non-zero Content-Length is present, and on
    POST/PUT/PATCH unless Content-Length says 0."""
    if body is not None or method == "GET":
        return False
    h = {str(k).lower(): str(v).strip() for k, v in (headers or {}).items()}
    length = h.get("content-length")
    if "content-type" in h or (length is not None and length != "0"):
        return True
    return method in ("POST", "PUT", "PATCH") and length != "0"


# --- allowlist matching and validation -----------------------------------------------------------

@functools.lru_cache(maxsize=1024)
def _full(url_regex: str) -> re.Pattern | None:
    """The entry regex wrapped as ^(?:regex)$, so a top-level `|` can't escape the anchors. None if invalid."""
    try:
        return re.compile(f"^(?:{url_regex})$")
    except re.error:
        return None


def matches(url_regex: str, url: str) -> bool:
    pat = _full(url_regex or "")
    return bool(pat and pat.match(url))


def _top_level_alternation(url_regex: str) -> bool:
    depth, i, in_class = 0, 0, False
    while i < len(url_regex):
        c = url_regex[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            in_class = c != "]" or url_regex[i - 1] == "["
        elif c == "[":
            in_class = True
            if url_regex[i + 1:i + 2] == "^":
                i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            return True
        i += 1
    return False


_SEPARATORS = (ord("/"), ord("?"))
_UNBOUNDED = 1 << 40  # stands in for an unbounded or unmeasurable width; always over the cap
_REPEATS = tuple(op for op in (sre_c.MAX_REPEAT, sre_c.MIN_REPEAT, getattr(sre_c, "POSSESSIVE_REPEAT", None)) if op)


def _class_crosses(items, seps=_SEPARATORS) -> bool:
    """Whether a [...] class can match '/' or '?'. Unknown members count as matching (fail closed)."""
    negate = bool(items) and items[0][0] is sre_c.NEGATE

    def hit(ch: int) -> bool:
        for op, av in items:
            if op is sre_c.NEGATE:
                continue
            if op is sre_c.LITERAL:
                if av == ch:
                    return True
            elif op is sre_c.RANGE:
                if av[0] <= ch <= av[1]:
                    return True
            elif op is sre_c.CATEGORY:
                # '/' and '?' are neither digits, spaces, word characters nor line breaks: only NOT_ classes match
                if "NOT" in str(av):
                    return True
            else:
                raise ValueError(f"unknown class member {op}")
        return False
    try:
        return any(hit(ch) != negate for ch in seps)
    except ValueError:
        return True


def _measure(sub, seps=_SEPARATORS) -> tuple[int, int, bool]:
    """(max width, crossing width, can cross) for a parsed regex sequence, computed structurally:
    - a node that can match '/' or '?' (a class, `.`, `[^x]`) crosses; a literal '/' or '?' crosses only
      when it sits inside a repeat;
    - concatenation adds widths, an alternation takes the max of its branches;
    - a repeat (upper bound > 1) whose body can cross counts its whole span, bound x body width, as crossing.
    Backreferences and unknown nodes measure as unbounded."""
    width = cross = 0
    can = False
    for op, av in sub:
        if op is sre_c.LITERAL:
            w, c, k = 1, 0, av in seps
        elif op is sre_c.ANY:
            w, c, k = 1, 1, True
        elif op is sre_c.NOT_LITERAL:
            k = any(ch != av for ch in seps)
            w, c = 1, int(k)
        elif op is sre_c.IN:
            k = _class_crosses(av, seps)
            w, c = 1, int(k)
        elif op is sre_c.CATEGORY:
            k = "NOT" in str(av)
            w, c = 1, int(k)
        elif op is sre_c.AT:
            w, c, k = 0, 0, False
        elif op is sre_c.SUBPATTERN:
            w, c, k = _measure(av[-1], seps)
        elif op is getattr(sre_c, "ATOMIC_GROUP", None):
            w, c, k = _measure(av, seps)
        elif op is sre_c.BRANCH:
            parts = [_measure(b, seps) for b in av[1]]
            w, c, k = max(p[0] for p in parts), max(p[1] for p in parts), any(p[2] for p in parts)
        elif op in _REPEATS:
            _lo, hi, item = av
            bw, bc, k = _measure(item, seps)
            hi = _UNBOUNDED if hi == sre_c.MAXREPEAT else hi
            w = hi * bw
            c = hi * bw if (k and hi > 1) else hi * bc
        elif op in (sre_c.ASSERT, sre_c.ASSERT_NOT):
            _w, c, _k = _measure(av[1], seps)  # zero width, but its crossing parts still count (fail closed)
            w, k = 0, False
        elif op is sre_c.GROUPREF_EXISTS:
            parts = [_measure(b, seps) for b in av[1:] if b is not None] or [(0, 0, False)]
            w, c, k = max(p[0] for p in parts), max(p[1] for p in parts), any(p[2] for p in parts)
        else:  # GROUPREF and anything unknown
            w, c, k = _UNBOUNDED, _UNBOUNDED, True
        width, cross, can = width + w, cross + c, can or k
    return width, cross, can


def _wildcard_width(sub) -> int:
    """Total characters the segment-crossing parts of a regex can span: (?:ab[^?]){0,40} spans 120,
    (?:[^?]{0,40}){0,40} spans 1600, [^?]{0,30}x[^?]{0,30} spans 60, (?:abc|[^?]{0,40}) spans 40."""
    return _measure(sub)[1]


def _broad_repeat(sub) -> bool:
    return _wildcard_width(sub) > _MAX_WILDCARD


def _is_literal(node) -> bool:
    op, av = node
    if op in (sre_c.LITERAL, sre_c.AT):
        return True
    if op is sre_c.SUBPATTERN:
        return all(_is_literal(n) for n in av[-1])
    if op is getattr(sre_c, "ATOMIC_GROUP", None):
        return all(_is_literal(n) for n in av)
    return False


def _flatten(sub) -> list:
    """Top-level nodes, with literal-only groups such as (?:add) inlined."""
    out = []
    for op, av in sub:
        if op is sre_c.SUBPATTERN and _is_literal((op, av)):
            out += _flatten(av[-1])
        elif op is getattr(sre_c, "ATOMIC_GROUP", None) and _is_literal((op, av)):
            out += _flatten(av)
        else:
            out.append((op, av))
    return out


def _segments(sub) -> list[list]:
    """The top-level nodes split at each literal '/'. A query (after a literal '?') stays with the last segment."""
    segs: list[list] = [[]]
    for node in _flatten(sub):
        if node == (sre_c.LITERAL, ord("/")):
            segs.append([])
        else:
            segs[-1].append(node)
    return segs


def _segment_error(sub) -> bool:
    """True unless at most one path segment is non-literal (holds a class, wildcard, repeat or alternation), that
    segment's non-literal parts span at most 40 characters, and no non-literal part can match '/'."""
    # The first three parts are "https:", "" and the host, which regex_host/site_error hold to a literal.
    loose = [seg for seg in _segments(sub)[3:] if not all(_is_literal(n) for n in seg)]
    if len(loose) > 1:
        return True
    for seg in loose:
        parts = [n for n in seg if not _is_literal(n)]
        width = 0
        for n in parts:
            w, _c, _k = _measure([n])
            if _measure([n], (ord("/"),))[2]:
                return True
            width += w
        if width > _MAX_WILDCARD:
            return True
    return False


def _literal_prefix(sub) -> str:
    """The literal text a regex starts with (after ^), e.g. "https://www.zepto.com/api/v1/cart/" for .../[\\w-]+$."""
    chars = []
    for op, av in _flatten(sub):
        if op is sre_c.AT:
            continue
        if op is not sre_c.LITERAL:
            break
        chars.append(chr(av))
    return "".join(chars)


def _random_paths(url_regex: str) -> list[str]:
    """Random paths (deterministic per regex) that no specific endpoint regex should match: three from the root,
    the regex's literal path prefix plus two and plus three random segments, and one random segment beside the
    prefix's last literal segment."""
    rnd = random.Random(url_regex)
    letters, alnum = string.ascii_lowercase, string.ascii_lowercase + string.digits

    def word(chars=letters, n=9):
        return "".join(rnd.choice(chars) for _ in range(n))
    probes = [f"/{word()}/{word()}", f"/{word(alnum)}",
              f"/{word(alnum)}/{word()}/{word(alnum)}?{word(letters, 4)}=1"]
    try:
        literal = _literal_prefix(sre_parse.parse(url_regex))
    except Exception:
        return probes
    rest = literal.removeprefix("https://")
    if rest == literal or "/" not in rest:
        return probes
    path = rest[rest.index("/"):]
    prefix = path[:path.rindex("/") + 1]                       # "/api/v1/cart/"
    parent = prefix.rstrip("/").rpartition("/")[0] + "/"       # "/api/v1/"
    probes += [f"{prefix}{word()}/{word()}", f"{prefix}{word()}/{word()}/{word()}", f"{parent}{word()}"]
    return probes


def narrow(url_regex: str, host: str) -> bool:
    """An allowlist regex is narrow when, matched as ^(?:regex)$, it is pinned to one endpoint. Heuristic:
    - it starts `^https://`, ends with an unescaped `$`, and has no `.*` / `.+` and no top-level `|`;
    - the parts that can match '/' or '?' ([^?]{1,}, \\S+, (?:ab[^?]){n}, (?:a/){n}) may span at most 40
      characters in total, measured structurally by `_measure` (repeats multiply, alternation takes the max);
    - at most ONE path segment is non-literal (a class, wildcard, repeat or alternation), its non-literal parts
      span at most 40 characters and none of them can match '/'; every other segment is fully literal;
    - it matches neither https://{host}/ nor https://{host}/x, nor any random probe on that host: three from
      the root and three built on the regex's literal path prefix (see _random_paths)."""
    if not (url_regex.startswith("^https://") and url_regex.endswith("$")) or url_regex.endswith("\\$"):
        return False
    if ".*" in url_regex or ".+" in url_regex or _top_level_alternation(url_regex):
        return False
    pat = _full(url_regex)
    if pat is None:
        return False
    try:
        parsed = sre_parse.parse(url_regex)
        if _broad_repeat(parsed) or _segment_error(parsed):
            return False
    except Exception:
        return False
    probes = ["/", "/x", *_random_paths(url_regex)]
    return not any(pat.match(f"https://{host}{path}") for path in probes)


def regex_host(url_regex: str) -> str | None:
    """The literal host an anchored regex is for, or None when the host part isn't a plain literal (a group,
    a class, or an unescaped `.`, which matches any character)."""
    m = re.match(r"\^https://((?:\\.|[^/\\])+)/", url_regex or "")
    # Only \. and \- are literal escapes; \S, \w, \x74, \\ ... are classes or tricks that can match other hosts.
    if not m or re.search(r"\\[^.\-]", m[1]) or re.search(r"(?<!\\)\.", m[1]):
        return None
    host = re.sub(r"\\(.)", r"\1", m[1])
    return host.lower() if re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?", host) else None


def host_ok(site: str, host: str | None) -> bool:
    import os

    import agent  # lazy: agent pulls in the browser and OpenAI client
    if not host:
        return False
    host = host.split(":")[0].lower()
    if os.getenv("WORKER_FAKE") == "1" and host == f"fake.{site}.test":
        return True
    return agent.site_allows_host(site, f"https://{host}/")


def confined(url_regex: str, samples=()) -> bool:
    """Probe host confinement: for each sample URL the regex matches, the same path on a foreign host (or
    behind a userinfo trick) must not match."""
    for s in samples:
        if not matches(url_regex, s):
            continue
        p = urlsplit(s)
        for netloc in ("evil.invalid", f"{p.hostname}.evil.invalid", f"{p.hostname}@evil.invalid"):
            if matches(url_regex, urlunsplit(p._replace(netloc=netloc))):
                return False
    return True


def broad_error(url_regex: str, hosts) -> str | None:
    own = regex_host(url_regex)
    if not all(narrow(url_regex, h) for h in {*hosts, *([own] if own else [])}):
        return f"allowlist regex is too broad: {url_regex}"
    return None


def site_error(url_regex: str, site: str, samples=()) -> str | None:
    own = regex_host(url_regex)
    if not host_ok(site, own) or not confined(url_regex, [*samples, f"https://{own}/"]):
        return "allowlist entry for another site"
    return None


# --- decision ------------------------------------------------------------------------------------

def _passes(entries, url: str, mutation: bool, op: str | None, allow_mutation: bool) -> bool:
    for e in entries:
        if not matches(e.get("url_regex"), url):
            continue
        if not mutation:
            return True
        if allow_mutation and e.get("operation") and e["operation"] == op:
            return True
    return False


def decide_cart(method: str, url: str, is_nav: bool, body: str | None, policy: CartPolicy, *,
                headers: dict | None = None, binary: bool = False) -> tuple[bool, str]:
    """Return (allowed, reason), in the R1 order. binary: the body could not be decoded (so wasn't scanned); a
    None body on a non-GET that may carry one (see _unreadable) is treated the same way."""
    method = effective_method(method, url, body, headers)
    if method not in POST_METHODS:
        return False, f"{method} never allowed"
    if method != "GET" and _encoded_separator(url):
        return False, "encoded path separator"
    if method != "GET" and _dot_dot_segment(url):
        return False, "dot segment in path"
    if method != "GET" and body and _ambiguous_multipart(body):
        return False, "ambiguous multipart body"
    if cat := forbidden_category(url, body, is_nav, write=method not in SAFE_METHODS):
        return False, f"forbidden: {cat}"
    binary = binary or _unreadable(method, body, headers)
    if binary and method != "GET":
        # body here is None or a lossy latin-1 view of the bytes. The quantity is still checked whenever it can
        # be read (a query qty_path, or a view that parses as JSON/form); then only a declared-binary entry passes.
        entries = [e for e in policy.cart if matches(e.get("url_regex"), url)] if method == "POST" else []
        viewable = _structured(body) is not None
        for e in entries:
            qp = e.get("qty_path")
            if qp and (qp.startswith("query:") or viewable) and not _adds_at_least_one(_value_at(url, body, qp)):
                return False, "forbidden: cart_removal"
        if any(e.get("binary") for e in entries):
            return True, "cart add"
        return False, "binary body"
    if method == "POST" and _challenge(url):
        return True, "bot-challenge token"
    if method in SAFE_METHODS:
        return True, "safe method"
    mutation = any(MUTATION.search(t) for t in _texts(body))
    op = operation_name(body) if mutation else None
    cart = [e for e in policy.cart if _passes((e,), url, mutation, op, allow_mutation=True)]
    if cart:
        # The recipe's qty_path, when the entry carries one, must hold a number >= 1 (add-only).
        if any(e.get("qty_path") and not _adds_at_least_one(_value_at(url, body, e["qty_path"])) for e in cart):
            return False, "forbidden: cart_removal"
        return True, "cart add"
    if _passes(policy.location, url, mutation, op, allow_mutation=True):
        return True, "location select"
    if _passes(policy.read, url, mutation, op, allow_mutation=False):
        return True, "read POST"
    return False, "POST not on cart allowlist"


class CartGuard:
    """Playwright route handler for cart mode. Records what it blocked (in memory only, for the learner) and
    every redirected write (`redirected_writes`, which the fill engine must treat as fatal)."""

    OPS = ("cart", "location", "read")

    def __init__(self, policy: CartPolicy, block_media: bool = False, site: str | None = None):
        self.policy = policy
        self.block_media = block_media
        self.site = site
        self.blocked: list[dict] = []
        self.redirected_writes: list[dict] = []
        self.media_blocked = 0

    def install(self, context) -> None:
        context.route("**/*", self._handle)
        context.on("response", self._on_response)

    def allow(self, op: str, entries) -> None:
        """Add approved entries to the cart, location or read allowlist, after the same checks a cart recipe
        gets (narrow, this site's literal host, confined). Raises ValueError and adds nothing on any failure.
        Forbidden categories still win over whatever is allowed."""
        if op not in self.OPS:
            raise ValueError(f"unknown allowlist {op!r}")
        if not self.site:
            raise ValueError("cart guard has no site to confine allowlist entries to")
        entries = [dict(e) for e in entries]
        samples = [b["url"] for b in self.blocked]
        for e in entries:
            qp = e.get("qty_path")
            if op == "cart" and not (isinstance(qp, str) and qp.strip()):
                raise ValueError("cart allowlist entry has no qty_path")
            regex = e.get("url_regex") or ""
            host = regex_host(regex)
            if err := broad_error(regex, [host] if host else []) or site_error(regex, self.site, samples):
                raise ValueError(err)
        self.policy = replace(self.policy, **{op: tuple(getattr(self.policy, op)) + tuple(entries)})

    def _handle(self, route, request) -> None:
        if self.block_media and request.resource_type in HEAVY_RESOURCE_TYPES:
            self.media_blocked += 1
            route.abort("blockedbyclient")
            return
        binary = False
        try:
            body = request.post_data
        except Exception:  # an undecodable (binary) body: scan a lossy latin-1 view of the bytes if there is one
            binary = True
            try:
                buf = request.post_data_buffer
                body = buf.decode("latin-1") if buf else None
            except Exception:
                body = None
        is_nav = request.is_navigation_request()
        ok, reason = decide_cart(request.method, request.url, is_nav, body, self.policy,
                                 headers=getattr(request, "headers", None), binary=binary)
        if ok:
            route.continue_()
            return
        # Unlisted background POSTs (analytics beacons) are blocked by the dozen; forbidden ones are news.
        quiet = reason == "POST not on cart allowlist" and not is_nav
        # Method, redacted URL and category only: never the body, query string or headers.
        log.log(logging.DEBUG if quiet else logging.WARNING, "CART BLOCKED %s %s (%s)",
                request.method, redact_url(request.url), reason)
        self.blocked.append({"method": request.method, "url": request.url,
                             "body": (body or "")[:4000], "reason": reason})
        route.abort("blockedbyclient")

    def _on_response(self, response) -> None:
        """A 30x answer to a non-GET: the browser follows it without routing the next hop through _handle."""
        try:
            status, req = response.status, response.request
            method = req.method.upper()
            if status not in REDIRECT_STATUSES or method == "GET":
                return
            location = (response.headers or {}).get("location")
            self.redirected_writes.append({"method": method, "url": req.url, "status": status, "location": location})
            log.warning("CART REDIRECTED WRITE %s %s (%s)", method, redact_url(req.url), status)
        except Exception:
            return
