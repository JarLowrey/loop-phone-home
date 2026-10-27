"""The driver's running state: driver.json, the log, the pid files, the running sessions and each worker thread's
context. Every other module reads and writes the driver's state through here."""
import datetime
import json
import os
import re
import threading
import time

import loop_common as C
import phone as P
import workspace as W

DRIVER_STATE = C.LOGS / "driver.json"
DRIVER_LOG = C.LOGS / "driver.log"
DRIVER_PID = C.LOGS / "driver.pid"
SUPERVISOR_PID = C.LOGS / "driver-supervisor.pid"
# why: the PC's --model, --approve and --deny write here, so a running driver, which owns driver.json, picks them up
REQUEST_FILE = C.LOGS / "driver-request.txt"
# the Claude sessions running now, by who runs them (a slot's name, or "opus-write"), so a shutdown can kill them all
CHILDREN = {}
# state: the driver.json dict; stopping: a shutdown is under way; scheduler: the running Scheduler
RUNTIME = {"state": None}
# each thread's own context: slot and task (the slot's name and the task it runs), run (the task's run number), prefix
# (its log lines' tag), outcome and reason (how the task ended, for events.jsonl), last_session, budget_cut
LOCAL = threading.local()
# events.emit, set by events.py when it loads, so this module can record pauses without importing it
EMIT = None
STATE_LOCK = threading.RLock()
LOG_LOCK = threading.Lock()
SUPERVISED = os.environ.get("DRIVER_SUPERVISED") == "1"
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ctx():
    """This thread's context: the slot it works in, its log tag, its last session and whether the day's budget cut it.
    Each worker thread has its own, so two slots never read each other's."""
    if not hasattr(LOCAL, "ctx"):
        LOCAL.ctx = {}
    return LOCAL.ctx


def log(message):
    """One line in driver.log, without control characters, tagged with the slot when slots run side by side. The first
    line of a new day moves the old log to driver-<its date>.log first."""
    line = f"{now()} {ctx().get('prefix', '')}{CONTROL_CHARS.sub('', str(message))}"
    print(line, flush=True)
    with LOG_LOCK:
        DRIVER_LOG.parent.mkdir(parents=True, exist_ok=True)
        if DRIVER_LOG.exists():
            day = datetime.date.fromtimestamp(DRIVER_LOG.stat().st_mtime)
            if day != datetime.date.today():
                target = DRIVER_LOG.parent / f"driver-{day.isoformat()}.log"
                if not target.exists():
                    os.replace(DRIVER_LOG, target)
        with open(DRIVER_LOG, "a", encoding="utf-8") as out:
            out.write(line + "\n")


P.log = log
W.log = log


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_pid(path):
    try:
        return int(path.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def read_driver_state():
    return read_json(DRIVER_STATE) or {"paused": False}


def write_driver_state(state):
    """Writes driver.json in one step (a temp file, then a rename), so a reader or a crash never leaves half a file."""
    with STATE_LOCK:
        DRIVER_STATE.parent.mkdir(parents=True, exist_ok=True)
        state["heartbeat"] = now()
        state["child_pids"] = sorted(CHILDREN.values())
        state.pop("child_pid", None)
        data = json.dumps(state, indent=2) + "\n"
        temp = DRIVER_STATE.with_suffix(".tmp")
        for _ in range(5):
            try:
                temp.write_text(data, encoding="utf-8", newline="\n")
                os.replace(temp, DRIVER_STATE)
                return
            except PermissionError:
                # why: on Windows a reader that has the file open makes the rename fail for a moment
                time.sleep(0.2)
        log("could not write driver.json: it stayed locked")


def pause(state, title, body, priority="high"):
    """Pauses the driver and tells the phone why. A pause from a task's thread is that task's outcome, unless it already
    has one."""
    state["paused"] = True
    write_driver_state(state)
    if ctx().get("task") and not ctx().get("outcome"):
        ctx().update(outcome="paused", reason=title)
    if EMIT:
        EMIT("pause", title=title, body=body[:500])
    P.post(title, body, priority)
