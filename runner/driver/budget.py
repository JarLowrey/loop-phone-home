"""Spending and usage limits: the account's usage limit, the daily budget, and what one session may spend."""
import datetime
import re
import threading
import time

import loop_common as C
import runtime as R
import settings as S

# what each running session may still spend, by its thread: a new session's cap leaves room for the others' caps
RESERVED = {}
RESERVED_LOCK = threading.Lock()


def reset_time(error):
    """The epoch time the usage limit resets, read from a message such as 'resets 1:20pm (America/Los_Angeles)', or None.
    A time already past today means tomorrow. Without the time zone's data, the PC's local time is used."""
    match = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)(?:\s*\(([^)]+)\))?", error, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "pm" else 0)
    zone = None
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(match.group(4)) if match.group(4) else None
    except Exception:
        zone = None
    current = datetime.datetime.now(zone)
    reset = current.replace(hour=hour, minute=int(match.group(2) or 0), second=0, microsecond=0)
    if reset <= current:
        reset += datetime.timedelta(days=1)
    return reset.timestamp()


def hit_usage_limit(task_id, error):
    """True when a session failed because the account's usage limit is reached. The task stays open and the driver
    pauses, since every task would fail the same way until the limit resets."""
    if not any(words in error for words in ("session limit", "usage limit", "subscription access")):
        return False
    state = R.RUNTIME["state"]
    resume = reset_time(error)
    state["resume_at"] = resume + S.USAGE_RESUME_MARGIN if resume else None
    if state["resume_at"]:
        when = time.strftime("%a %H:%M", time.localtime(state["resume_at"]))
        text = (f"Session usage is exhausted, so the loop stopped during {task_id}. It restarts at {when} (PC time), "
                f"4 minutes after usage reopens. Send RESUME to restart it sooner. {error[:200]}")
    else:
        when = "unknown"
        text = (f"The session stopped during {task_id}, so the loop stopped too. No restart time could be read from "
                f"the message, so it waits for RESUME. {error[:200]}")
    R.log(f"usage limit reached during {task_id}: restart at {when}, the task stays open")
    R.pause(state, "Loop stopped: session usage exhausted", text, "urgent")
    return True


def never_started(task_id, error):
    """True when the last session failed before it took a turn, within NOT_STARTED_SECONDS: the account, the network or
    Claude Code is at fault, and every task would fail the same way. On 2026-10-26 one such error blocked 97 tasks in
    five minutes. The task stays open, and the driver pauses and tries again after NOT_STARTED_RETRY_SECONDS."""
    session = R.ctx().get("last_session") or {}
    if session.get("turns") or session.get("seconds", S.NOT_STARTED_SECONDS) >= S.NOT_STARTED_SECONDS:
        return False
    state = R.RUNTIME["state"]
    state["resume_at"] = time.time() + S.NOT_STARTED_RETRY_SECONDS
    minutes = S.NOT_STARTED_RETRY_SECONDS // 60
    R.log(f"the session for {task_id} never started; pausing {minutes} min: {error[:200]}")
    R.pause(state, "Loop paused: sessions cannot start", f"The session for {task_id} failed before it took a turn, so "
            f"the fault is not the task's. It stays open, and the driver tries again in {minutes} minutes (send RESUME "
            f"to try now). {error[:250]}", "urgent")
    return True


def over_budget(state):
    """True when today's spending in usage.jsonl has reached daily_budget_usd. The driver then pauses until just after
    midnight, and the phone is told once."""
    spent = C.spend_on()
    if S.DAILY_BUDGET_USD <= 0 or spent < S.DAILY_BUDGET_USD:
        return False
    tomorrow = datetime.datetime.combine(datetime.date.today() + datetime.timedelta(days=1), datetime.time())
    state["resume_at"] = tomorrow.timestamp() + S.USAGE_RESUME_MARGIN
    R.log(f"daily budget reached: ${spent:.2f} of ${S.DAILY_BUDGET_USD:g}")
    R.pause(state, "Loop paused: daily budget reached", f"${spent:.2f} spent today, and the budget is "
            f"${S.DAILY_BUDGET_USD:g} (daily_budget_usd in the loop file). It restarts after midnight, or send RESUME.",
            "urgent")
    return True


def session_budget(cap):
    """The most the next session may spend: its own cap, or what is left of today's budget after the other running
    sessions' caps when that is less, so slots side by side cannot carry the day past daily_budget_usd. The session's
    cap is reserved until release_reservation(). Remembers, for this thread, whether the day's budget cut it."""
    with RESERVED_LOCK:
        others = sum(value for key, value in RESERVED.items() if key != threading.get_ident())
        left = S.DAILY_BUDGET_USD - C.spend_on() - others if S.DAILY_BUDGET_USD > 0 else cap
        R.ctx()["budget_cut"] = left < cap
        budget = round(max(min(cap, left), 0.01), 2)
        RESERVED[threading.get_ident()] = budget
    return budget


def release_reservation():
    """Frees this thread's reserved cap once its session has ended (its spending is then in usage.jsonl)."""
    with RESERVED_LOCK:
        RESERVED.pop(threading.get_ident(), None)


def cut_by_daily_budget(error):
    """True when a session ended at a budget that today's remaining budget had cut. That is not the task's fault: the
    caller undoes the work, the task stays open, and the driver pauses until midnight."""
    if not (R.ctx().get("budget_cut") and "budget" in error.lower()):
        return False
    R.log(f"a session stopped at the day's remaining budget: {error[:200]}")
    if not over_budget(R.RUNTIME["state"]):
        R.pause(R.RUNTIME["state"], "Loop paused: daily budget", "A session stopped at what was left of today's "
                "budget. Send RESUME to carry on.")
    return True
