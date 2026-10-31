"""Monthly budget: cost from metered usage, the ok / alert / paused level, and its stored state (R5, R6)."""
from dataclasses import dataclass
from datetime import date, datetime

from server import usage
from server.config import Settings


def cost_inr(proxy_mb: float, ai_tokens: int, worker_seconds: float, s: Settings) -> float:
    """Rupees for the metered usage (no fixed monthly part)."""
    return (proxy_mb / 1000 * s.proxy_inr_per_gb
            + ai_tokens / 1000 * s.ai_inr_per_1k_tokens
            + worker_seconds / 60 * s.worker_inr_per_minute)


def _month_range(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1)


def month_cost(conn, s: Settings, month: str) -> float:
    """All users' usage in that IST month (deleted users' folded spend included) plus the fixed monthly cost."""
    first, nxt = _month_range(month)
    mb, tokens, secs = conn.execute(
        "select coalesce(sum(proxy_mb), 0), coalesce(sum(ai_tokens), 0), coalesce(sum(worker_seconds), 0) "
        "from usage where day >= %s and day < %s", (first, nxt)).fetchone()
    emb, etokens, esecs = conn.execute(
        "select coalesce(sum(proxy_mb), 0), coalesce(sum(ai_tokens), 0), coalesce(sum(worker_seconds), 0) "
        "from usage_erased where day >= %s and day < %s", (first, nxt)).fetchone()
    mb, tokens, secs = mb + emb, tokens + etokens, secs + esecs
    return cost_inr(float(mb), int(tokens), float(secs), s) + s.fixed_monthly_inr


def level_for(spent: float, s: Settings) -> str:
    if spent >= s.monthly_budget_inr * s.budget_pause_pct / 100:
        return "paused"
    if spent >= s.monthly_budget_inr * s.budget_alert_pct / 100:
        return "alert"
    return "ok"


@dataclass
class BudgetState:
    month: str
    spent_inr: float
    budget_inr: float
    pct: int
    level: str
    previous_level: str


def refresh_state(conn, s: Settings, now: datetime | None = None) -> BudgetState:
    """Recompute this IST month's level and store it. previous_level is what was stored before (ok if none)."""
    month = usage.month_of(now, s.app_tz)
    spent = month_cost(conn, s, month)
    level = level_for(spent, s)   # unrounded: only the stored figure is rounded
    # One transaction: make sure the row exists, then lock it, so overlapping ticks take turns and exactly one
    # of them sees each transition (the second reads the level the first stored).
    with conn.transaction():
        conn.execute("insert into budget_state (month, level, spent_inr, budget_inr) values (%s, 'ok', 0, %s) "
                     "on conflict (month) do nothing", (month, s.monthly_budget_inr))
        previous = conn.execute("select level from budget_state where month = %s for update", (month,)).fetchone()[0]
        conn.execute("update budget_state set level = %s, spent_inr = %s, budget_inr = %s, updated_at = now() "
                     "where month = %s", (level, round(spent, 2), s.monthly_budget_inr, month))
    pct = int(spent * 100 // s.monthly_budget_inr) if s.monthly_budget_inr > 0 else 0
    return BudgetState(month, round(spent, 2), float(s.monthly_budget_inr), pct, level, previous)


def current_level(conn, now: datetime | None = None, tz: str | None = None) -> str:
    """The stored level of the current IST month; a month with no row is ok."""
    row = conn.execute("select level from budget_state where month = %s", (usage.month_of(now, tz),)).fetchone()
    return row[0] if row else "ok"


def is_paused(conn, now: datetime | None = None, tz: str | None = None) -> bool:
    return current_level(conn, now, tz) == "paused"
