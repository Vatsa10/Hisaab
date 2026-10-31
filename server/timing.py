"""Speed budgets: per-phase timings for a job (open, session_check, fetch, parse, store).

Logs carry phase names and milliseconds only. A job over its budget logs `slow_job site=... phase=...` and is
flagged in jobs.timings; it is never a user-facing failure."""
import contextlib
import contextvars
import logging
import time

log = logging.getLogger("timing")
PHASES = ("open", "session_check", "fetch", "parse", "store")
_current: contextvars.ContextVar["Recorder | None"] = contextvars.ContextVar("timing_recorder", default=None)


class Recorder:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.phases: list[dict] = []
        self.on_phase = None  # optional callback(phase_ms dict), e.g. to publish job progress

    @contextlib.contextmanager
    def phase(self, name: str):
        t0 = self.clock()
        try:
            yield
        finally:
            self.phases.append({"phase": name, "ms": int(round((self.clock() - t0) * 1000))})
            if self.on_phase:
                try:
                    self.on_phase(self.phase_ms())
                except Exception:
                    pass

    def phase_ms(self) -> dict:
        out: dict[str, int] = {}
        for p in self.phases:
            out[p["phase"]] = out.get(p["phase"], 0) + p["ms"]
        return out

    def total_ms(self) -> int:
        return sum(p["ms"] for p in self.phases)

    def summary(self, site: str, budget_s: float) -> dict:
        total = self.total_ms()
        slow = total > budget_s * 1000
        if slow:
            by = self.phase_ms()
            worst = max(by, key=by.get) if by else "none"
            log.warning("slow_job site=%s phase=%s total_ms=%d budget_s=%s", site, worst, total, budget_s)
        return {"phases": list(self.phases), "total_ms": total, "slow": slow}


@contextlib.contextmanager
def recording(rec: Recorder):
    token = _current.set(rec)
    try:
        yield rec
    finally:
        _current.reset(token)


@contextlib.contextmanager
def Phase(name: str):
    """Record `name` on the job's recorder; a no-op outside a job."""
    rec = _current.get()
    if rec is None:
        yield
    else:
        with rec.phase(name):
            yield


def current_phase_ms() -> dict:
    """Milliseconds per phase so far on this job's recorder ({} outside a job)."""
    rec = _current.get()
    return rec.phase_ms() if rec else {}


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(pct / 100 * (len(v) - 1))))]
