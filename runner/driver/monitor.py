"""The monitor: the clean-checkout check and the monitor command. Every monitor_every landed tasks (the driver-settings
block), the driver makes a clean copy of the main branch's last commit in the monitor slot (cache_dirs removed, as a new
clone has none) and runs the tests there. That catches work that passed in a slot only because a file its cache kept.
Then, when the config names a monitor_command (a benchmark, a game bot, a smoke run), it runs that too. Each run goes to
<logs>/monitor-history.jsonl and events.jsonl, and the phone hears when either fails. It runs in its own thread beside
the slots, and never blocks or undoes a task: the alert names the tasks since the last run, so a cause is easy to
find."""
import json
import subprocess
import threading
import time

import events as E
import loop_common as C
import phone as P
import runtime as R
import sessions as X
import settings as S
import slots as SL

HISTORY = C.LOGS / "monitor-history.jsonl"
MONITOR_TIMEOUT = 1800
THREAD = {"thread": None}


def running():
    return bool(THREAD["thread"] and THREAD["thread"].is_alive())


def due(state):
    """True when monitor_every tasks have landed since the last run (0 turns the monitor off)."""
    return S.MONITOR_EVERY > 0 and len(state.get("monitor_pending") or []) >= S.MONITOR_EVERY


def start(state):
    """Starts run() in its own thread, so the slots go on meanwhile."""
    def body():
        R.ctx()["prefix"] = "[monitor] " if S.PARALLEL_SLOTS > 1 else ""
        try:
            run(state)
        except Exception as error:
            R.log(f"monitor run failed: {error}")
            P.post("Monitor failed", f"The driver hit an error in the monitor run: {str(error)[:200]}", "high")
    THREAD["thread"] = threading.Thread(target=body, daemon=True, name="monitor")
    THREAD["thread"].start()


def clean_checkout(slot, tasks):
    """The tests in the clean copy: '' when they pass, else the failure. A failure here means a slot relied on
    something no commit holds."""
    failure = C.test_failure(C.test_output(root=slot.root))
    if not failure:
        R.log(f"clean checkout of {slot.base[:10]} passes")
        return ""
    R.log(f"clean checkout of {slot.base[:10]} fails: {failure}")
    P.post("Clean checkout fails", f"A fresh copy of {slot.base[:10]} fails its tests: {failure[:200]}. A slot's cache "
           f"may have hidden a missing file. Tasks since the last check: {', '.join(tasks)}.", "high")
    return failure


def monitor_command(slot, tasks):
    """The config's monitor_command in the clean copy: (exit code, its last lines). None when there is none."""
    command = C.CFG.monitor_command
    if not command:
        return None
    try:
        with X.polling():
            done = subprocess.run(command, shell=isinstance(command, str), cwd=slot.root, capture_output=True,
                                  text=True, errors="replace", timeout=MONITOR_TIMEOUT)
        code, output = done.returncode, (done.stdout + done.stderr).strip()
    except subprocess.TimeoutExpired:
        code, output = -1, f"timed out after {MONITOR_TIMEOUT} s"
    (C.LOGS / "monitor-latest.log").write_text(output + "\n", encoding="utf-8", newline="\n")
    tail = "\n".join(output.splitlines()[-5:])
    if code != 0:
        P.post("Monitor command failed", f"Exit {code} after {', '.join(tasks)}: {tail[-300:]}. Its output: "
               f"{C.CFG.logs_dir}/monitor-latest.log.", "high")
    return code, tail


def run(state):
    """The clean-checkout tests, then the monitor command, each recorded."""
    timer = E.Timer()
    with R.STATE_LOCK:
        tasks = list(state.get("monitor_pending") or [])
        state["monitor_pending"] = []
        R.write_driver_state(state)
    slot = SL.Slot("monitor")
    SL.prepare(slot, fresh=True)
    R.log(f"monitor run starts in a clean copy of {slot.base[:10]}, after {', '.join(tasks)}")
    clean = clean_checkout(slot, tasks)
    result = monitor_command(slot, tasks)
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "head": slot.base[:10], "tasks": tasks,
              "clean": clean or "pass", "monitor_exit": result[0] if result else None,
              "monitor_tail": result[1] if result else ""}
    with open(HISTORY, "a", encoding="utf-8") as history:
        history.write(json.dumps(record) + "\n")
    E.emit("monitor_run", seconds=timer.seconds(), clean=clean or "pass", tasks=tasks,
           monitor_exit=record["monitor_exit"])
