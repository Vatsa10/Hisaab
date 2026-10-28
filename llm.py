"""The only place that talks to the LLM: Claude Haiku 5.5 behind a hard cost policy.

Policy (owner's rule): no model at or above $1 per million tokens, input or output, and no prompt past the
100K-token step where Haiku 5.5's price rises to $0.50 / $2.50. The model comes from ANTHROPIC_MODEL (default
claude-haiku-5-5) and must be in MODEL_PRICES under $1; there is no override. Every request's prompt is capped
at INPUT_CAP tokens: an obviously small prompt is bounded by its UTF-8 size, anything larger is counted with
messages.count_tokens first.

Logs carry only the purpose, model, token counts and stop_reason: never the key, prompts or replies.
Metering: on_usage(callback) sees one CallUsage per call.
"""
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass

import anthropic
from dotenv import load_dotenv

log = logging.getLogger("llm")

DEFAULT_MODEL = "claude-haiku-5-5"
# USD per million tokens (input, output) for prompts of 100K tokens or less.
MODEL_PRICES = {"claude-haiku-5-5": (0.10, 0.50)}
MAX_PRICE = 1.00  # per MTok: a model at or above this, for input or output, is refused
INPUT_CAP = 90_000  # tokens per request: stays under the 100K-token price step
# Tokens the API adds around a request (tool-use system prompt, message framing), on top of what we send.
REQUEST_OVERHEAD = 2_000
EFFORTS = ("low", "medium", "high")


class LLMError(RuntimeError):
    """A reply that cannot be used."""


class LLMPolicyError(LLMError):
    """The cost policy refused the model or the prompt."""


class LLMRefusal(LLMError):
    """stop_reason "refusal": Haiku 5.5 has no server-side fallback, so the caller decides what to do."""


class LLMTruncated(LLMError):
    """stop_reason "max_tokens": the reply (or a tool input) was cut off."""


@dataclass(frozen=True)
class CallUsage:
    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None


_usage_hooks: list[Callable[[CallUsage], None]] = []


def on_usage(callback: Callable[[CallUsage], None]) -> Callable[[], None]:
    """Call callback(CallUsage) after every API reply (including refused or truncated ones). Returns a function
    that unregisters it. A failing callback is logged by class and never breaks the call."""
    _usage_hooks.append(callback)
    return lambda: _usage_hooks.remove(callback) if callback in _usage_hooks else None


def check_policy(name: str | None = None) -> str:
    """The model name if the cost policy allows it, else LLMPolicyError. Called at first use and at worker/API
    start-up."""
    name = name if name is not None else _configured()
    prices = MODEL_PRICES.get(name)
    if prices is None:
        raise LLMPolicyError(f"model {name!r} is not allowed by the cost policy (allowed: {', '.join(MODEL_PRICES)})")
    if any(p >= MAX_PRICE for p in prices):
        raise LLMPolicyError(f"model {name!r} costs ${prices[0]:.2f} / ${prices[1]:.2f} per MTok; "
                             f"the cost policy allows only models under ${MAX_PRICE:.2f}")
    return name


def _configured() -> str:
    return (os.getenv("ANTHROPIC_MODEL") or "").strip() or DEFAULT_MODEL


def model() -> str:
    load_dotenv()
    return check_policy(_configured())


def client() -> anthropic.Anthropic:
    """An Anthropic client (credentials from the environment), only once the model passes the policy."""
    model()
    return anthropic.Anthropic()


def _new_client():
    return client()  # looked up at call time, so tests can swap llm.client


def tool(name: str, description: str, props: dict | None = None, required=()) -> dict:
    """A strict client tool: the API guarantees its input matches the schema."""
    return {"name": name, "description": description, "strict": True, "input_schema": {
        "type": "object", "properties": props or {}, "required": list(required), "additionalProperties": False}}


def _plain(o):
    if hasattr(o, "model_dump"):
        return o.model_dump()
    if hasattr(o, "__dict__"):
        return vars(o)
    return str(o)


def upper_bound(**request) -> int:
    """A cheap upper bound on a request's prompt tokens: every token covers at least one UTF-8 byte."""
    body = {k: request.get(k) for k in ("system", "messages", "tools")}
    return len(json.dumps(body, ensure_ascii=False, default=_plain).encode("utf-8")) + REQUEST_OVERHEAD


def prompt_tokens(c, **request) -> int:
    """The prompt size the cap is checked against: the byte bound when that is under the cap, else the
    API's own count."""
    bound = upper_bound(**request)
    if bound <= INPUT_CAP:
        return bound
    counted = {k: v for k, v in request.items() if k in ("model", "system", "messages", "tools", "tool_choice",
                                                          "thinking", "output_config")}
    return c.messages.count_tokens(**counted).input_tokens


def create(purpose: str, *, effort: str, max_tokens: int, messages: list, system: str | None = None,
           tools: list | None = None, tool_choice: dict | None = None, output_schema: dict | None = None,
           client=None):
    """One Messages API call under the policy. Returns the Message once stop_reason is usable ("end_turn",
    "tool_use", ...); raises LLMRefusal / LLMTruncated otherwise, LLMPolicyError before any call the policy
    refuses, and the SDK's typed errors (logged by class) when the API fails."""
    if effort not in EFFORTS:
        raise ValueError(f"effort must be one of {EFFORTS}")
    if not messages or messages[-1]["role"] != "user":
        raise ValueError("the last message must be a user turn (no assistant prefill)")
    mdl = model()
    c = client if client is not None else _new_client()
    output_config: dict = {"effort": effort}
    if output_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": output_schema}
    request = {"model": mdl, "max_tokens": max_tokens, "messages": messages, "thinking": {"type": "adaptive"},
               "output_config": output_config}
    if system is not None:
        request["system"] = system
    if tools:
        request["tools"] = tools
    if tool_choice is not None:
        request["tool_choice"] = tool_choice
    if prompt_tokens(c, **request) > INPUT_CAP:
        log.warning("llm %s refused: prompt over the %d-token cap", purpose, INPUT_CAP)
        raise LLMPolicyError("prompt too large for the cost policy")
    try:
        resp = c.messages.create(**request)
    except anthropic.BadRequestError as e:
        log.warning("llm %s %s rejected the request (%s)", purpose, mdl, type(e).__name__)
        raise
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        log.error("llm %s %s: credentials refused (%s)", purpose, mdl, type(e).__name__)
        raise
    except anthropic.RateLimitError as e:
        log.warning("llm %s %s: rate limited after retries (%s)", purpose, mdl, type(e).__name__)
        raise
    except anthropic.APIStatusError as e:
        log.warning("llm %s %s: API error %s (%s)", purpose, mdl, e.status_code, type(e).__name__)
        raise
    except anthropic.APIConnectionError as e:
        log.warning("llm %s %s: connection failed (%s)", purpose, mdl, type(e).__name__)
        raise
    _record(purpose, resp)
    if resp.stop_reason == "refusal":
        raise LLMRefusal(f"the model declined the {purpose} request")
    if resp.stop_reason == "max_tokens":
        raise LLMTruncated(f"the {purpose} reply hit max_tokens={max_tokens}")
    return resp


def _record(purpose: str, resp) -> None:
    u = resp.usage
    usage = CallUsage(purpose=purpose, model=resp.model, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                      stop_reason=resp.stop_reason)
    log.info("llm %s %s in=%d out=%d stop=%s", purpose, usage.model, usage.input_tokens, usage.output_tokens,
             usage.stop_reason)
    for hook in list(_usage_hooks):
        try:
            hook(usage)
        except Exception as e:
            log.warning("usage hook failed (%s)", type(e).__name__)


def prompt_size(resp) -> int:
    """Tokens the next request in the same conversation starts from: this prompt plus this reply."""
    u = resp.usage
    return (u.input_tokens + (getattr(u, "cache_read_input_tokens", 0) or 0)
            + (getattr(u, "cache_creation_input_tokens", 0) or 0) + u.output_tokens)


def text(resp) -> str:
    """The reply's text blocks (read by type: a reply can start with thinking blocks)."""
    return "".join(b.text for b in resp.content if b.type == "text")


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def json_reply(resp):
    """The reply's JSON: the text itself, or the outermost {...} when the model wrapped it in a fence or prose."""
    raw = _FENCE.sub("", text(resp).strip())
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end < start:
            raise
        return json.loads(raw[start:end + 1])
