"""The driver's running state: driver.json, the log, the pid files and the task's snapshot. The paths depend on the
config, so init_paths() sets them after worker_task.configure()."""
import datetime
import json
import os
import re
import threading
import time

import worker_task as W

C = W.C
PATHS = {}
# the session that is running now, so a shutdown can kill it
CHILD = {"pid": None}
# state: the driver.json dict; settings: the driver-settings block, parsed; snap: the task's snapshot; stopping: a
# shutdown is under way; budget_cut: the day's budget cut the last session's cap; tests_before: the test count
RUNTIME = {"state": {}, "settings": None, "last": None}
STATE_LOCK = threading.RLock()
LOG_LOCK = threading.Lock()
SUPERVISED = os.environ.get("DRIVER_SUPERVISED") == "1"
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def init_paths():
    """Sets the paths that depend on the config. The logs folder is made first, so the pid file can be written at once."""
    C.logs.mkdir(parents=True, exist_ok=True)
    PATHS["pid"] = C.logs / "driver.pid"
    PATHS["supervisor"] = C.logs / "driver-supervisor.pid"
    PATHS["state"] = C.logs / "driver.json"
    PATHS["log"] = C.logs / "driver.log"
    PATHS["request"] = C.logs / "driver-request.txt"
    PATHS["loop"] = C.project / C.loop_file
    PATHS["progress"] = C.project / C.progress_file
    PATHS["worker"] = os.path.abspath(W.__file__)


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    """One line in driver.log, without control characters. The first line of a new day moves the old log to
    driver-<its date>.log first."""
    line = f"{now()} {CONTROL_CHARS.sub('', str(message))}"
    with LOG_LOCK:
        path = PATHS["log"]
        if path.exists():
            day = datetime.date.fromtimestamp(path.stat().st_mtime)
            target = path.with_name(f"driver-{day.isoformat()}.log")
            if day != datetime.date.today() and not target.exists():
                os.replace(path, target)
        with open(path, "a", encoding="utf-8") as record:
            record.write(line + "\n")
    print(f"[driver] {line}", flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_pid(path):
    try:
        return int(path.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def read_state():
    return read_json(PATHS["state"]) or {"paused": False}


def write_state(state):
    """Writes driver.json in one step (a temp file, then a rename), so a reader or a crash never leaves half a file."""
    with STATE_LOCK:
        state["heartbeat"] = now()
        state["child_pid"] = CHILD["pid"]
        data = json.dumps(state, indent=2) + "\n"
        temp = PATHS["state"].with_suffix(".tmp")
        for _ in range(5):
            try:
                temp.write_text(data, encoding="utf-8", newline="\n")
                os.replace(temp, PATHS["state"])
                return
            except PermissionError:
                # why: on Windows a reader that has the file open makes the rename fail for a moment
                time.sleep(0.2)
        log("could not write driver.json: it stayed locked")


def set_snap(state, snap):
    """Keeps the task's snapshot in memory and in driver.json, so --shutdown from another process reverts the same."""
    RUNTIME["snap"] = snap
    state["snap"] = {"head": snap["head"], "dirty": sorted(snap["dirty"])} if snap else None
    write_state(state)


def current_snap(state=None):
    """The running task's snapshot, from memory or else from driver.json. None between tasks."""
    if RUNTIME.get("snap"):
        return RUNTIME["snap"]
    saved = (state or {}).get("snap")
    return {"head": saved["head"], "dirty": set(saved["dirty"])} if saved else None
