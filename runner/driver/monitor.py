"""The monitor: what no single task's gate can see. In a clean copy of the main branch's last commit (cache_dirs removed,
as a new clone has none) it runs the tests, which catches work that passed in a slot only because a file its cache kept,
and then the config's monitor commands (a benchmark, a smoke run of the built program, a slow end-to-end suite). Each
command is judged by its exit code: one that passed in the last record and fails now sends a "Monitor got worse" alert
naming what landed since. In a command, {head} is the commit under test, {previous} the last one the monitor recorded
and {commits} how many commits lie between them, so a benchmark can compare the two.

Two things start it. The driver runs it in its own thread every monitor_every landed tasks (the driver-settings
block), and `python <runner>/driver --monitor`, which a scheduled task (--install-monitor-schedule) runs each night,
runs it in a slot of its own whenever the main branch's last commit has no record yet. That covers the commits made by
hand, which the driver never sees. One record a run goes to <logs>/monitor-history.jsonl, and a lock file keeps two
runs apart. It never blocks or undoes a task. tasks.status_text shows how far the last record is behind."""
import datetime
import json
import os
import subprocess
import sys
import threading
import time

import core as C
import sessions as X
import slots as SL

HISTORY = C.LOGS / "monitor-history.jsonl"
LOCK = C.LOGS / "monitor.lock"
MONITOR_TIMEOUT = 1800
SINCE_SHOWN = 8
TAIL_LINES = 5
THREAD = {"thread": None}
SCHEDULE_NAME = f"{C.PROJECT.name} monitor"
LOGON_DELAY = "PT10M"


def commands():
    """The monitor's commands as [{"name", "command", "timeout"}]: monitor_commands, then monitor_command as "monitor"."""
    found = [dict(item) for item in C.CFG.monitor_commands or [] if item.get("command")]
    if C.CFG.monitor_command:
        found.append({"name": "monitor", "command": C.CFG.monitor_command})
    for index, item in enumerate(found):
        item.setdefault("name", f"command {index + 1}")
    return found


def read_history():
    if not HISTORY.exists():
        return []
    found = []
    for line in HISTORY.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found


def record_of(head):
    """The newest record of this commit, or None when it has none."""
    return next((record for record in reversed(read_history()) if record.get("head") == head[:10]), None)


def due(state):
    """True when monitor_every tasks have landed since the last run (0 turns the monitor off). tasks.py counts them in
    task_committed."""
    if C.MONITOR_EVERY <= 0 or len(state.get("monitor_pending") or []) < C.MONITOR_EVERY:
        return False
    # why: while a scheduled --monitor holds the lock, a run started now would only find it held, once a pass
    holder = C.read_pid(LOCK) if LOCK.exists() else None
    return not (holder and C.pid_alive(holder))


def running():
    return bool(THREAD["thread"] and THREAD["thread"].is_alive())


def start(state):
    """Starts a run over the tasks landed since the last one, in its own thread, so the slots go on meanwhile."""
    def body():
        C.ctx()["prefix"] = "[monitor] " if C.PARALLEL_SLOTS > 1 else ""
        try:
            with C.STATE_LOCK:
                tasks = list(state.get("monitor_pending") or [])
                state["monitor_pending"] = []
                C.write_driver_state(state)
            if not take_lock():
                C.log("monitor: another monitor run holds the lock; its tasks wait for the next run")
                with C.STATE_LOCK:
                    state["monitor_pending"] = tasks + list(state.get("monitor_pending") or [])
                    C.write_driver_state(state)
                return
            try:
                slot = SL.Slot("monitor")
                SL.prepare(slot, fresh=True)
                watch(slot, ", ".join(tasks), tasks)
            finally:
                LOCK.unlink(missing_ok=True)
        except Exception as error:
            C.log(f"monitor run failed: {error}")
            C.post("Monitor failed", f"The driver hit an error in the monitor run: {str(error)[:200]}", "high")
    THREAD["thread"] = threading.Thread(target=body, daemon=True, name="monitor")
    THREAD["thread"].start()


def take_lock():
    """True when this process now holds the monitor's lock. A lock whose process has ended is taken over."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            handle = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = C.read_pid(LOCK)
            if holder and C.pid_alive(holder):
                return False
            LOCK.unlink(missing_ok=True)
            continue
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(str(os.getpid()))
        return True
    return False


def clean_checkout(slot):
    """The tests in the clean copy: '' when they pass, else why not. A failure here means a slot relied on something no
    commit holds."""
    with X.polling():
        output = C.test_output(root=slot.root, env=X.test_env())
    failure = C.test_failure(output)
    if failure:
        C.log(f"monitor: clean checkout of {slot.base[:10]} fails: {failure}")
    else:
        C.log(f"monitor: clean checkout of {slot.base[:10]}: {C.totals_line(output)}")
    return failure


def filled(command, values):
    """The command with {head}, {previous} and {commits} put in, as a string or a list of words."""
    def fill(text):
        for key, value in values.items():
            text = text.replace("{" + key + "}", str(value))
        return text
    if isinstance(command, str):
        return fill(command)
    words = [fill(str(word)) for word in command]
    if words and words[0] in ("python", "python3", "py"):
        words[0] = sys.executable
    return words


def run_command(slot, item, values):
    """One monitor command in the slot: (exit code, its last lines). A run past its timeout reads exit -1."""
    command = filled(item["command"], values)
    timeout = item.get("timeout") or MONITOR_TIMEOUT
    try:
        with X.polling():
            code, output = C.command_result(command, slot.root, timeout, X.test_env(), what=item["name"])
    except OSError as error:
        code, output = -1, f"{item['name']} could not start: {error}"
    log = C.LOGS / f"monitor-{item['name'].replace(' ', '-')}.log"
    log.write_text(output + "\n", encoding="utf-8", newline="\n")
    return code, "\n".join(output.splitlines()[-TAIL_LINES:])


def commits_since(previous, head, root=None):
    """(how many commits lie from previous to head, "<hash> <subject>" text of them, the newest first, cut to
    SINCE_SHOWN)."""
    if not previous:
        return 0, ""
    found = C.git("log", "--format=%h %s", f"{previous}..{head}", root=root).stdout.strip().splitlines()
    more = f" and {len(found) - SINCE_SHOWN} more" if len(found) > SINCE_SHOWN else ""
    return len(found), "; ".join(found[:SINCE_SHOWN]) + more


def judge(history, record):
    """The lines the alert carries for this record against the history before it: the clean checkout failing, and
    each command that fails now and did not fail in its last record. Empty when nothing got worse."""
    found = []
    if record["clean"] != "pass":
        found.append(f"the clean checkout's tests fail: {record['clean'][:200]}")
    for name, result in record["commands"].items():
        if result["exit"] == 0:
            continue
        before = next((old["commands"][name] for old in reversed(history) if name in (old.get("commands") or {})),
                      None)
        if before is None or before.get("exit") == 0:
            found.append(f"{name} exits {result['exit']}: {result['tail'][-200:]}")
    return found


def watch(slot, since, tasks=()):
    """Runs the clean checkout and every monitor command over the slot's commit, records it, and alerts the phone when
    something got worse. since names what landed since the last record. Returns the record."""
    timer = C.Timer()
    history = read_history()
    previous = history[-1].get("head", "") if history else ""
    head = slot.base[:10]
    count, _ = commits_since(previous, slot.base, root=slot.root)
    values = {"head": head, "previous": previous or head, "commits": count}
    C.log(f"monitor run starts in a clean copy of {head}, after {since or 'nothing named'}")
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "head": head, "previous": previous, "since": since,
              "tasks": list(tasks), "clean": clean_checkout(slot) or "pass", "commands": {}}
    for item in commands():
        code, tail = run_command(slot, item, values)
        record["commands"][item["name"]] = {"exit": code, "tail": tail}
        C.log(f"monitor: {item['name']} exits {code}")
    found = judge(history, record)
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY, "a", encoding="utf-8") as out:
        out.write(json.dumps(record) + "\n")
    C.emit("monitor_run", seconds=timer.seconds(), head=head, clean=record["clean"], tasks=list(tasks),
           commands={name: result["exit"] for name, result in record["commands"].items()}, worse=found)
    if found:
        C.log(f"monitor: worse at {head}: {'; '.join(found)}")
        C.post("Monitor got worse", f"{'; '.join(found)} at {head}. Since the last record: {since or 'nothing named'}. "
               f"Records in {C.LOGS_REL}monitor-history.jsonl.", "high")
    return record


def cli_monitor(args):
    """python <runner>/driver --monitor [--force]: runs the monitor over the main branch's last commit in a slot of its
    own, unless it already has a record (--force runs it again). Returns the exit code."""
    problem = C.start_problem()
    if problem:
        print(f"no monitor run: {problem}")
        return 2
    head = SL.main_tip()
    if not head:
        print("no monitor run: the main branch has no commit")
        return 2
    if record_of(head) and "--force" not in args:
        C.log(f"monitor: {head[:10]} already has a record; nothing to do")
        return 0
    if not take_lock():
        C.log("monitor: another monitor run holds the lock; this one is skipped")
        return 3
    try:
        slot = SL.Slot("monitor-cli")
        SL.prepare(slot, fresh=True)
        history = read_history()
        _, since = commits_since(history[-1].get("head", "") if history else "", slot.base, root=slot.root)
        record = watch(slot, since)
    finally:
        LOCK.unlink(missing_ok=True)
    return 0 if record["clean"] == "pass" and all(item["exit"] == 0 for item in record["commands"].values()) else 1


def status_lines(root=None):
    """The status file's lines that say how far the last monitor record is behind the main branch, so a monitor that
    stopped running is seen."""
    history = read_history()
    if not history:
        return ["No monitor record yet. `python <runner>/driver --monitor` makes one."]
    last = history[-1]
    behind = C.git("rev-list", "--count", f"{last.get('head', '')}..{SL.main_tip() or 'HEAD'}", root=root)
    count = behind.stdout.strip() if behind.returncode == 0 else "?"
    when = last.get("time", "?")
    failing = [name for name, result in (last.get("commands") or {}).items() if result.get("exit") != 0]
    if last.get("clean", "pass") != "pass":
        failing.insert(0, "the clean checkout")
    line = (f"Last record: `{last.get('head', '?')}` on {when}, {count} commit(s) behind the main branch; "
            + (f"failing: {', '.join(failing)}." if failing else "everything passed."))
    try:
        days = (datetime.datetime.now() - datetime.datetime.strptime(when, "%Y-%m-%d %H:%M:%S")).days
    except ValueError:
        days = 0
    if count not in ("0", "?") and days >= 2:
        line += f" It is {days} days old: is the scheduled task (`--install-monitor-schedule`) still there?"
    return [line]


def install_schedule():
    """Runs --monitor each night at monitor_schedule_hour, and LOGON_DELAY after each logon: on Windows a scheduled task
    (started on the next wake when the PC was off at that hour); elsewhere it prints the crontab line to add. Returns
    the exit code."""
    hour = C.CFG.monitor_schedule_hour or "03:00"
    config = C.CFG.config_path
    if os.name != "nt":
        hours, minutes = (hour.split(":") + ["0"])[:2]
        print(f'add this line with crontab -e:\n{int(minutes)} {int(hours)} * * * cd "{C.PROJECT}" && '
              f'WORKER_CONFIG="{config}" "{sys.executable}" "{C.DRIVER_DIR}" --monitor')
        return 0
    argument = (f'/c set "WORKER_CONFIG={config}" && start "{SCHEDULE_NAME}" /min "{sys.executable}" '
                f'"{C.DRIVER_DIR}" --monitor').replace("'", "''")
    script = "\n".join([
        f"$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '{argument}' -WorkingDirectory '{C.PROJECT}'",
        f"$night = New-ScheduledTaskTrigger -Daily -At {hour}",
        "$logon = New-ScheduledTaskTrigger -AtLogOn -User \"$env:USERDOMAIN\\$env:USERNAME\"",
        f"$logon.Delay = '{LOGON_DELAY}'",
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Hours 3)",
        f"Register-ScheduledTask -TaskName '{SCHEDULE_NAME}' -Action $a -Trigger $night,$logon -Settings $s -Force "
        "| Out-Null",
    ])
    done = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True)
    if done.returncode != 0:
        print(f"the scheduled task was not made: {done.stderr.strip()[:400]}")
        return 1
    print(f"'{SCHEDULE_NAME}' runs --monitor each night at {hour} and after each logon")
    return 0


def remove_schedule():
    if os.name != "nt":
        print("remove the --monitor line with crontab -e")
        return 0
    done = subprocess.run(["powershell", "-NoProfile", "-Command",
                           f"Unregister-ScheduledTask -TaskName '{SCHEDULE_NAME}' -Confirm:$false"],
                          capture_output=True, text=True)
    print("scheduled task removed" if done.returncode == 0 else f"not removed: {done.stderr.strip()[:300]}")
    return done.returncode
