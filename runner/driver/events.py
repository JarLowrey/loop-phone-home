"""The driver's structured record: <logs>/events.jsonl, one JSON object per line, for audits that look for what makes
the loop slow, stuck or wasteful. driver.log says what happened in words; this file says it as data, with a duration on
every phase, so <runner>/driver/report.py (or a model) can add up where the hours and dollars went.

Every event has: time (local, to the second), t (epoch seconds, for arithmetic), event (its kind), and, when a worker
thread emits it, task, slot and run (the task's run number: 1 for its first start, 2 after a requeue or a restart).
The kinds and their fields:
  driver_start   pid, branch, commit (of the main tree), settings
  driver_stop    reason
  settings       changed (the driver settings that changed in the loop file)
  pause          title, body          resume   reason
  schedule_wait  free_slots, next, reason (why no task starts in a free slot), running
  task_start     title, base, files, solo
  session        model, effort, kind, seconds, turns, cost_usd, is_error, subtype, terminal_reason, tokens_in,
                 tokens_out, cache_read, cache_write, tools ({name: calls}), tool_errors, denials ([reason]),
                 bash_timeouts, test_runs, log (the stream's file)
  escalate       from, to, reason
  catch_up       seconds, landed (commits that landed while the task ran), result
  gate           seconds, test_seconds, totals, reason ('' when passed), log (the test output's file)
  land           seconds, lock_wait, rebased, retest_seconds, owner_wait, result, commit
  push           seconds, ok, detail
  task_end       outcome (landed, blocked, requeued, paused, stopped, crashed), reason, seconds
  requeue        reason, wait_for, count          block   reason, outcome
  approval       what, seconds, answer           opus_write   seconds, result
  monitor_run    seconds, clean (the clean checkout's verdict), tasks, monitor_exit
Adding a field is safe: readers take what they know. Renaming or dropping one breaks report.py and old audits."""
import json
import threading
import time

import loop_common as C
import runtime as R

LOCK = threading.Lock()


def log_path():
    """<logs>/events.jsonl, found when written, so a test that moves the logs moves this file too."""
    return C.LOGS / "events.jsonl"


def emit(event, **fields):
    """Appends one event. The thread's task, slot and run are added when it has them. Never raises: a record that
    cannot be written must not stop the loop, so the failure goes to driver.log instead."""
    context = R.ctx()
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "t": round(time.time(), 3), "event": event}
    for key in ("task", "slot", "run"):
        if context.get(key) is not None and key not in fields:
            record[key] = context[key]
    record.update(fields)
    try:
        line = json.dumps(record, default=str)
        with LOCK:
            path = log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as out:
                out.write(line + "\n")
    except Exception as error:
        R.log(f"could not write an event ({event}): {error}")


class Timer:
    """Seconds since it was made, rounded for the record."""

    def __init__(self):
        self.start = time.time()

    def seconds(self):
        return round(time.time() - self.start, 1)


def stamp():
    """A file-name-safe time, so each attempt's logs get their own file instead of overwriting the last one's."""
    return time.strftime("%Y%m%d-%H%M%S")


R.EMIT = emit
