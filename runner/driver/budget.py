"""Spending and usage limits: the account's usage limit, the daily budget, and what one session may spend."""
import datetime
import json
import re
import time

import phone as P
import runtime as R
import settings as S
import worker_task as W

C = W.C


def spend_on(day=None):
    """The dollars the usage log records for one day (today by default), summed over every session."""
    day = day or datetime.date.today().isoformat()
    path = C.logs / C.usage_log
    total = 0.0
    if not path.exists():
        return total
    with open(path, encoding="utf-8", errors="replace") as usage:
        for line in usage:
            if not line.startswith('{"time": "' + day):
                continue
            try:
                total += float(json.loads(line).get("cost_usd") or 0.0)
            except (ValueError, TypeError, AttributeError):
                continue
    return total


def reset_time(error):
    """The epoch time the usage limit resets, read from a message such as 'resets 1:20pm (America/Los_Angeles)', or None.
    A time already past today means tomorrow. Without the zone's data, the machine's local time is used."""
    match = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)(?:\s*\(([^)]+)\))?", error, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "pm" else 0)
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(match.group(4)) if match.group(4) else None
    except Exception:
        zone = None
    now_at = datetime.datetime.now(zone)
    reset = now_at.replace(hour=hour, minute=int(match.group(2) or 0), second=0, microsecond=0)
    if reset <= now_at:
        reset += datetime.timedelta(days=1)
    return reset.timestamp()


def hit_usage_limit(task_id, error, state):
    """True when a session failed because usage is exhausted. The driver pauses with the task kept open, sends an urgent
    phone alert with the restart time, and restarts itself USAGE_RESUME_MARGIN seconds after the limit resets."""
    if not S.USAGE_LIMIT.search(error):
        return False
    resume = reset_time(error)
    state["resume_at"] = resume + S.USAGE_RESUME_MARGIN if resume else None
    if state["resume_at"]:
        when = time.strftime("%a %H:%M", time.localtime(state["resume_at"]))
        text = (f"Session usage is exhausted, so the loop stopped during {task_id}. It restarts at {when} (machine time), "
                f"4 minutes after usage reopens. Send RESUME to restart it sooner. {error[:200]}")
    else:
        when = "unknown"
        text = (f"Session usage is exhausted, so the loop stopped during {task_id}. No restart time could be read from "
                f"the message, so it waits for RESUME. {error[:200]}")
    R.log(f"usage limit reached during {task_id}: restart at {when}, the task stays open")
    P.pause(state, "Loop stopped: session usage exhausted", text, "urgent")
    return True


def over_budget(state):
    """True when today's spending in the usage log has reached daily_budget_usd. The driver then pauses until just after
    midnight, and the phone is told once."""
    budget = S.get("daily_budget_usd")
    spent = spend_on()
    if budget <= 0 or spent < budget:
        return False
    tomorrow = datetime.datetime.combine(datetime.date.today() + datetime.timedelta(days=1), datetime.time())
    state["resume_at"] = tomorrow.timestamp() + S.USAGE_RESUME_MARGIN
    R.log(f"daily budget reached: ${spent:.2f} of ${budget:g}")
    P.pause(state, "Loop paused: daily budget reached", f"${spent:.2f} spent today, and the budget is ${budget:g} "
            "(daily_budget_usd in the loop file). It restarts after midnight, or send RESUME.", "urgent")
    return True


def session_budget(cap):
    """The most the next session may spend: its own cap, or what is left of today's budget when that is less, so a
    session cannot carry the day past daily_budget_usd. Remembers whether the day's budget cut it."""
    budget = S.get("daily_budget_usd")
    left = budget - spend_on() if budget > 0 else cap
    R.RUNTIME["budget_cut"] = left < cap
    return round(max(min(cap, left), 0.01), 2)


def cut_by_daily_budget(error):
    """True when a session ended at a budget that today's remaining budget had cut. That is not the task's fault: the
    caller undoes the work, the task stays open, and the driver pauses until midnight."""
    if not (R.RUNTIME.get("budget_cut") and "budget" in error.lower()):
        return False
    R.log(f"a session stopped at the day's remaining budget: {error[:200]}")
    if not over_budget(R.RUNTIME["state"]):
        P.pause(R.RUNTIME["state"], "Loop paused: daily budget", "A session stopped at what was left of today's "
                "budget. Send RESUME to carry on.")
    return True
