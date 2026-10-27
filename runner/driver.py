"""The loop driver: runs the open tasks in the task file one at a time, without an orchestrating session.

Usage, from the folder that holds worker.json (or with WORKER_CONFIG set):
  python driver.py             the supervisor: runs the driver, and starts it again after a crash (waiting longer each time)
  python driver.py --run       the driver alone, with no supervisor (the supervisor starts it this way)
  python driver.py --worker    as the first form, but the worker does each task (worker_task.py) and Haiku reviews it
  python driver.py --shutdown  ends the supervisor, the driver and its session, reverts the unfinished task, and stops
  python driver.py --model sonnet [high]   who does the tasks from the next one on (the MODEL command)
  python driver.py --approve | --deny      answers every approval the driver is waiting for (the phone's buttons)

Each task is done by one session of the task model (task_model in worker.json, sonnet by default; MODEL changes it). A
task is committed only after the driver's own checks pass: nothing changed outside the scope, no test removed, the test
run passes now, and the task's Done when line holds. A task that fails is undone and tried once more by the escalation
model (escalate_model in the driver-settings block) with the failure as feedback; a second failure blocks it. A task
whose Who line says Opus is a review or planning task, and Opus does it.

Every automated call to Opus at medium effort or more, and every call to Fable, waits for an Approve button on the phone
(or --approve on the machine). A denial pauses the driver and the task stays open.

Commands from the phone, in any case, as the whole message:
  STATUS              a progress report now
  MAN                 the list of commands
  STOP or PAUSE       no new task starts until RESUME; the driver stays up
  RESUME or RESTART   carry on
  WIND DOWN           the driver ends after the current step
  SHUTDOWN NOW        the running session is killed, and the driver ends
  APPROVE <code> / DENY <code>   the answer to an approval request (its buttons send these)
  MODEL [<model> [effort]]       who does the tasks, or with no model, a report; EFFORT <level> sets only the effort

Config: worker.json (see worker_task.py DEFAULTS). The driver reads loop_file, progress_file, open_task_pattern,
rules_file, project_brief, sync, task_model, task_turns, task_timeout, deny_tools, usage_log and token_file from it.
loop_file holds a driver-settings block (poll_seconds, max_attempts, retry_states, escalate_model, daily_budget_usd,
approval_hours), which is read again at each pass, so an edit takes effect without a restart.
A task heading is "### [ ] ID title" while it is open, and "### [x] ID title" once it is done.
"""
import datetime
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import worker_task as W  # noqa: E402  (the config, paths and helpers the worker uses)

C = W.C
COMMANDS = ("STATUS", "MAN", "STOP", "PAUSE", "RESUME", "RESTART", "WIND DOWN", "SHUTDOWN NOW")
MAN_TEXT = """Commands (any case, the whole message):
STATUS - progress report now
MAN - this list
STOP or PAUSE - no new task starts until RESUME; the driver stays up
RESUME or RESTART - carry on
WIND DOWN - the driver ends after the current step
SHUTDOWN NOW - kill the running session now and end the driver
APPROVE <code> or DENY <code> - answer an approval request (its buttons send these)
MODEL - who does the tasks. MODEL <haiku|sonnet|opus|fable> [effort] changes it; EFFORT <low..max|default>
Automated Opus (medium effort or more) and Fable calls wait for your Approve."""
SETTING_DEFAULTS = {
    "poll_seconds": "60",
    "max_attempts": "3",
    "retry_states": "tests_failed, failed, looped, timed_out, tests_did_not_run",
    "escalate_model": "opus",
    "daily_budget_usd": "120",
    "approval_hours": "12",
}
TASK_TOOLS = "Bash,Read,Edit,Write,Glob,Grep"
# the models that may do the tasks, by the name the MODEL command takes, with the name their commits carry
MODEL_NAMES = {"haiku": "Claude Haiku 5.5", "sonnet": "Claude Sonnet 5.5", "opus": "Claude Opus 5.5",
               "fable": "Claude Fable 5.1"}
MODEL_RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 2}
# the most one task session may spend, by model. A session that reaches it ends, and the task counts as failed
MODEL_BUDGET_USD = {"haiku": 3.0, "sonnet": 8.0, "opus": 20.0, "fable": 20.0}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
TASK_PROMPT = """You are doing one task in {brief}, alone: no other model reviews your work. In this session:
1. Read the task below. Where it gives example values or expected results, a test must check exactly those. If a test
   you write disagrees with the task, the test is wrong: fix the test to match the task.
2. Change files only under: {scope}. Create only the files the task names. Never delete, skip or weaken an existing
   test. Do not change files under {loop}, except a file the task's Done when line names.
3. Run the tests with: {test_command}
   Fix any failure and run them again, until a run reports no failures.
4. Check the task's **Done when.** line yourself, if it has one: the driver checks the same line and blocks the task
   if any part is missing.
5. Then stop. Your last message is the list of files you changed and the totals line from the last run, nothing else.

Your shell starts in the project root. Run every command from there and write every path from there: never cd, which
makes relative paths land in the wrong folder. Every command that reads, searches or runs the project's scripts is
allowed, read-only git included (status, diff, log, show, stash list). Git commands that change files or the index are
refused, and a command line that chains a refused command with others is refused whole.

The project rules ({rules_file}):
{rules}

The task:
{task}
"""
FEEDBACK_NOTE = """

An earlier attempt at this task, by {model}, failed and was undone, so you start from a clean tree. Why it failed:
{reason}

That attempt's test output (the end of it):
{tests}
"""
USAGE_RESUME_MARGIN = 240  # why: restart 4 minutes after usage reopens, so the first call is not refused again
USAGE_LIMIT = re.compile(r"session limit|usage limit|subscription access", re.I)
DONE_WHEN_ITEM = re.compile(r"\b(test|file|output) `([^`]+)`")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
APPROVAL_POLL_SECONDS = 15
ALREADY_RUNNING = 3
# the supervisor: a crash within STABLE_SECONDS of a start counts toward MAX_QUICK_CRASHES, and each one doubles the wait
RESTART_WAIT = 60
RESTART_WAIT_MAX = 1800
STABLE_SECONDS = 600
MAX_QUICK_CRASHES = 5
MAX_TASK_ERRORS = 3
OPUS_MODEL = os.environ.get("OPUS_MODEL", "opus")
OPUS_EFFORT = "high"
OPUS_TASK_TURNS = 200
OPUS_TASK_TIMEOUT = 7200
OPUS_TASK_BUDGET_USD = 25.0
OPUS_TASK_PROMPT = """You are the senior engineer reviewing this project, and you have one task from the build loop's queue.
Nobody is watching: do not ask questions.

Rules:
- Read the task below and do what it says. Read files with Read, Grep and Glob. Use the shell to read git history
  (git log, git show, git diff) and to run the tests.
- Change only the files the task names. Never delete, skip or loosen a test. If you change code, run the tests
  ({test_command}) until they pass: the driver runs them too and undoes everything on a fail.
  Never change this task's own heading.
- Never run a git command that changes anything (add, commit, reset, checkout, stash). The driver commits your work.
- When you add tasks to the task file, use its format. Give every task a **Done when.** line whose items are `test`,
  `file` or `output` names in backticks.
- Your last message: the files you wrote and how many tasks you added, in one short paragraph.

The project rules:
{rules}

The task:
{task}
"""


def parse_settings(values):
    """The driver settings as the types they are used as. Raises ValueError for a value that cannot be read."""
    escalate = values["escalate_model"].strip().lower()
    if escalate not in MODEL_NAMES and escalate not in ("", "none"):
        raise ValueError(f"escalate_model {escalate!r} is not one of {', '.join(MODEL_NAMES)} or none")
    return {"poll_seconds": max(5, int(values["poll_seconds"])), "max_attempts": int(values["max_attempts"]),
            "retry_states": tuple(part.strip() for part in values["retry_states"].split(",") if part.strip()),
            "escalate_model": "" if escalate == "none" else escalate,
            "daily_budget_usd": float(values["daily_budget_usd"]), "approval_hours": float(values["approval_hours"])}


RUNTIME = {"state": {}, "settings": parse_settings(SETTING_DEFAULTS), "last": None}
CHILD = {"pid": None}
PATHS = {}
STATE_LOCK = threading.RLock()
LOG_LOCK = threading.Lock()
SUPERVISED = os.environ.get("DRIVER_SUPERVISED") == "1"


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
    PATHS["worker"] = pathlib.Path(W.__file__).resolve()


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


def post(title, body, priority="default", actions=""):
    """Sends one message to the phone and logs it. The driver's own messages are always sent: the worker's phone_titles
    filter applies only to the worker's alerts."""
    log(f"phone [{priority}] {title}: {body[:120]}")
    if not C.topic:
        return
    headers = {"Title": title, "Priority": priority}
    if actions:
        headers["Actions"] = actions
    request = urllib.request.Request(f"{C.ntfy_server}/{C.topic}", data=body[:500].encode("utf-8"),
                                     headers=W.ntfy_headers(headers), method="POST")
    try:
        urllib.request.urlopen(request, timeout=20).read()
    except Exception as error:
        log(f"phone send failed: {error}")


def button(label, body):
    """An ntfy action button that posts body to the inbox topic, which is how the phone answers the driver."""
    return f"http, {label}, {C.ntfy_server}/{C.inbox_topic}, method=POST, body={body}, clear=true"


def pause(state, title, body, priority="high"):
    """Pauses the driver and tells the phone why."""
    state["paused"] = True
    write_state(state)
    post(title, body, priority)


def driver_env():
    """The environment for a worker run or a revert: the same config, and the flag that tells the worker the driver
    answers the phone's inbox."""
    env = dict(os.environ)
    env["WORKER_CONFIG"] = str(C.config_path)
    env["WORKER_UNDER_DRIVER"] = "1"
    return env


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


def task_model(state):
    """(model, effort) that do the tasks. effort is None for the model's own default."""
    return state.get("task_model") or C.task_model, state.get("task_effort")


def model_text(state):
    model, effort = task_model(state)
    return f"Tasks are done by {model}, effort {effort or 'default'}."


def needs_approval(model, effort):
    """True for an automated call that must wait for the phone's Approve: Fable at any effort, and Opus at medium
    effort or more. The model's default effort counts as more than medium."""
    return model == "fable" or (model == "opus" and effort != "low")


def set_model(state, words):
    """MODEL <model> [effort] or EFFORT <level>, as upper-case words. Returns the reply for the phone."""
    if words[0] == "EFFORT":
        if len(words) < 2:
            return f"Say EFFORT and a level: {', '.join(EFFORTS)} or default. " + model_text(state)
        words = ["MODEL", task_model(state)[0].upper(), *words[1:]]
    if len(words) == 1:
        return model_text(state)
    model = words[1].lower()
    effort = words[2].lower() if len(words) > 2 else (state.get("task_effort") or "default")
    if model not in MODEL_NAMES or effort not in EFFORTS + ("default",) or len(words) > 3:
        return f"Not changed. Models: {', '.join(MODEL_NAMES)}. Efforts: {', '.join(EFFORTS)}, default. " + model_text(state)
    state["task_model"] = model
    state["task_effort"] = None if effort == "default" else effort
    write_state(state)
    log(f"task model set: {model}, effort {effort}")
    note = " Each task asks for your approval first." if needs_approval(model, state["task_effort"]) else ""
    return "From the next task on. " + model_text(state) + note


def answer_approval(state, words, from_machine):
    """APPROVE <code> or DENY <code>. From the machine, with no code, it answers every waiting request."""
    answer = "approve" if words[0] == "APPROVE" else "deny"
    waiting = state.get("approvals") or {}
    codes = [words[1].lower()] if len(words) > 1 else (list(waiting) if from_machine else [])
    hits = [code for code in codes if code in waiting and not waiting[code].get("answer")]
    for code in hits:
        waiting[code]["answer"] = answer
    write_state(state)
    if not hits:
        return f"No approval request is waiting for {' '.join(words[1:]) or 'an answer'}."
    return f"{answer.capitalize()}d: " + "; ".join(waiting[code]["what"][:120] for code in hits)


def approved(state, what):
    """Asks the phone to approve an automated Opus or Fable call, and waits for the answer. True when it is approved. No
    answer within approval_hours counts as a denial, and so does a shutdown or WIND DOWN while waiting."""
    code = secrets.token_hex(3)
    hours = RUNTIME["settings"]["approval_hours"]
    with STATE_LOCK:
        state.setdefault("approvals", {})[code] = {"what": what, "asked": now(), "answer": None}
        write_state(state)
    log(f"approval {code} asked: {what}")
    post("Approve this call?", f"{what}. Approve or deny (code {code}). No answer in {hours:g} h is a denial.", "high",
         f"{button('Approve', 'APPROVE ' + code)}; {button('Deny', 'DENY ' + code)}")
    deadline = time.time() + hours * 3600
    answer = None
    while time.time() < deadline and not RUNTIME.get("stopping"):
        handle_commands(state)
        answer = state["approvals"][code].get("answer")
        if answer or state.get("wind_down"):
            break
        time.sleep(APPROVAL_POLL_SECONDS)
    with STATE_LOCK:
        state["approvals"].pop(code, None)
        write_state(state)
    log(f"approval {code}: {answer or 'no answer, so denied'}")
    return answer == "approve"


def status_text(state):
    """One line for the phone: whether the loop runs, what the current task is doing, who does the tasks, today's spend."""
    paused = "paused" if state.get("paused") else "running"
    if not state.get("busy"):
        text = f"Loop {paused}. No task is running."
    else:
        text = f"Loop {paused}. Task {state.get('task')} ({state.get('mode')}), started {state.get('task_started')}."
        if not state.get("solo"):
            run = W.read_status()
            if run.get("task") == state.get("task"):
                text += f" Attempt {run.get('attempt')}: {run.get('phase_detail') or run.get('state', '')}."
    waiting = len(state.get("approvals") or {})
    text += (f" {model_text(state)} Spent today: ${spend_on():.2f} of "
             f"${RUNTIME['settings']['daily_budget_usd']:g}.")
    return text + (f" Waiting for {waiting} approval(s)." if waiting else "")


def machine_requests():
    """The commands --model, --approve and --deny left in the request file, as (id, text) like the phone's, with an
    empty id. The file is emptied."""
    path = PATHS["request"]
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    path.unlink(missing_ok=True)
    return [("", line) for line in lines if line.strip()]


def handle_commands(state):
    """Reads the phone's inbox and the machine's request file, and does each command in them. Each message is read
    once. A message that is not a command is ignored; a short one is logged, in case it was a typo, and a long one (such
    as the driver's own replies) is not."""
    with STATE_LOCK:
        seen = W.read_seen()
        for message_id, text in machine_requests() + W.poll_inbox():
            if message_id:
                if message_id in seen:
                    continue
                W.mark_seen(message_id)
                seen.add(message_id)
            words = text.upper().split()
            command = " ".join(words)
            if words and words[0] in ("MODEL", "EFFORT"):
                log(f"command {command}")
                post("Loop task model", set_model(state, words))
                continue
            if words and words[0] in ("APPROVE", "DENY"):
                log(f"command {command}")
                post("Approval", answer_approval(state, words, not message_id))
                continue
            if command not in COMMANDS:
                if len(text) <= 40:
                    log(f"inbox message is not a command, ignored: {text!r}")
                continue
            log(f"command {command}")
            if command == "STATUS":
                post("Loop status", status_text(state))
            elif command == "MAN":
                post("Commands", MAN_TEXT)
            elif command == "WIND DOWN":
                state["wind_down"] = True
                write_state(state)
                post("Loop will exit", "The driver ends after the current step.")
            elif command == "SHUTDOWN NOW":
                post("Loop shutting down", "Killing the running session and ending the driver now.", "high")
                shut_down(state, "SHUTDOWN NOW from the phone")
                raise SystemExit(0)
            elif command in ("STOP", "PAUSE"):
                state["paused"] = True
                write_state(state)
                post("Loop paused", "No new task starts until you send RESUME.")
            else:
                state.update({"paused": False, "resume_at": None})
                write_state(state)
                post("Loop resumed", "Carrying on with the next open task.")


def poll_while(stop):
    """Runs in its own thread while a session runs: answers the phone every poll_seconds, so STATUS, STOP, WIND DOWN
    and SHUTDOWN NOW work in the middle of a task. SHUTDOWN NOW ends the whole driver from here."""
    while not stop.wait(RUNTIME["settings"]["poll_seconds"]):
        try:
            handle_commands(RUNTIME["state"])
            write_state(RUNTIME["state"])
        except SystemExit:
            PATHS["pid"].unlink(missing_ok=True)
            log("driver ended by SHUTDOWN NOW")
            os._exit(0)
        except Exception as error:
            log(f"command poll failed: {error}")


def shut_down(state, reason):
    """Kills the running session, undoes the unfinished task, and marks the driver paused. The stopping flag is set
    first, so the thread that was waiting on the session does not also undo or mark the task."""
    RUNTIME["stopping"] = True
    W.kill_tree(CHILD["pid"])
    CHILD["pid"] = None
    if state.get("busy"):
        revert_unfinished()
    state.update({"busy": False, "paused": True})
    write_state(state)
    log(f"shut down: {reason}")


def running_driver():
    """The pid of another driver or supervisor that is still running from this folder, or 0."""
    for key in ("supervisor", "pid"):
        other = read_pid(PATHS[key])
        if other and other != os.getpid() and W.pid_alive(other):
            return other
    return 0


def cli_shutdown():
    """python driver.py --shutdown: stops the supervisor, the driver and its session, from the machine they run on. The
    supervisor goes first, so it does not start again the driver it sees end."""
    state = read_state()
    for pid in (read_pid(PATHS["supervisor"]), read_pid(PATHS["pid"]), state.get("child_pid")):
        if pid and W.pid_alive(pid):
            W.kill_tree(pid)
    if state.get("busy"):
        revert_unfinished()
    state.update({"busy": False, "paused": True, "child_pid": None, "approvals": {}})
    write_state(state)
    PATHS["supervisor"].unlink(missing_ok=True)
    PATHS["pid"].unlink(missing_ok=True)
    log("shut down from the machine")
    print("driver shut down; nothing is running from it. Start it again with python driver.py")


def queue_request(text):
    """--model, --approve, --deny: leaves the command for the driver, which reads it on its next poll or start."""
    with open(PATHS["request"], "a", encoding="utf-8", newline="\n") as requests:
        requests.write(text + "\n")
    print(f"queued {text!r}: the driver applies it on its next poll, or when it next starts. "
          "The reply goes to the phone and driver.log.")


def refresh_settings():
    """Reads the driver-settings block in the loop file. A value that cannot be read puts every setting back to its
    default, and a change to the settings is logged."""
    values = dict(SETTING_DEFAULTS)
    try:
        text = PATHS["loop"].read_text(encoding="utf-8")
    except OSError as error:
        log(f"could not read {PATHS['loop'].name}: {error}; using the defaults")
        text = ""
    match = re.search(r"```driver-settings\n(.*?)```", text, re.S)
    for line in (match.group(1).splitlines() if match else []):
        key, separator, value = line.partition(":")
        if not separator or key.strip().startswith("#"):
            continue
        if key.strip() in values:
            values[key.strip()] = value.strip()
        else:
            log(f"unknown driver setting {key.strip()!r} in {PATHS['loop'].name} is ignored")
    try:
        settings = parse_settings(values)
    except ValueError as error:
        log(f"a driver setting in {PATHS['loop'].name} is not valid ({error}); using the defaults")
        settings = parse_settings(SETTING_DEFAULTS)
    if settings != RUNTIME["last"]:
        log(f"driver settings: {settings}")
        RUNTIME["last"] = settings
    RUNTIME["settings"] = settings


def next_task():
    """(id, title) of the first open task in the task file, or None."""
    pattern = re.compile(C.open_task_pattern)
    for line in C.tasks.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1), match.group(2).strip()
    return None


def set_heading(task_id, mark):
    """Sets the open heading of a task to done (x) or blocked (!)."""
    lines = C.tasks.read_text(encoding="utf-8").split("\n")
    prefix = f"### [ ] {task_id} "
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = line.replace("### [ ]", f"### [{mark}]", 1)
            break
    else:
        raise RuntimeError(f"no open heading for {task_id} in {C.tasks_file}")
    C.tasks.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def add_progress(row=None, blocked=None):
    """Adds a table row for a finished task, or a line under Blocked. Does nothing if the progress file does not exist."""
    if not PATHS["progress"].exists():
        return
    lines = PATHS["progress"].read_text(encoding="utf-8").split("\n")
    if row:
        dated = [index for index, line in enumerate(lines) if line.startswith("|")]
        lines.insert((dated[-1] if dated else len(lines) - 1) + 1, row)
    if blocked:
        if "## Blocked" not in lines:
            lines += ["", "## Blocked", ""]
        lines.insert(lines.index("## Blocked") + 2, blocked)
    PATHS["progress"].write_text("\n".join(lines), encoding="utf-8", newline="\n")


def git(*args, stdin=None):
    return subprocess.run(["git", *args], cwd=C.project, input=stdin, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def sync():
    """Pushes the branch after a commit, as a plain push: a rejected push is reported, never forced."""
    if not C.sync:
        return
    try:
        done = subprocess.run(["git", "push", "origin", "HEAD"], cwd=C.project, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
    except subprocess.TimeoutExpired:
        log("sync timed out")
        post("Sync failed", "git push took longer than 3 minutes. The commit is local only.", "high")
        return
    if done.returncode == 0:
        log("synced to origin")
        return
    detail = (done.stderr or done.stdout).strip()
    log(f"sync failed: {detail[-300:]}")
    post("Sync failed", f"The commit is local only: {detail[-200:]}", "high")


def commit(message, paths):
    """Commits the paths that exist or were deleted, with the message, and then syncs if sync is on."""
    changed = W.changed_paths()
    git("add", "-A", "--", *[path for path in dict.fromkeys(paths)
                             if (C.project / path).exists() or path in changed])
    done = git("commit", "-q", "-F", "-", stdin=message)
    if done.returncode != 0:
        raise RuntimeError(f"git commit failed: {(done.stderr or done.stdout).strip()[-300:]}")
    log(f"committed: {message.splitlines()[0]}")
    sync()


def loop_folder():
    """The folder of the loop file, with a trailing slash."""
    return C.loop_file.rsplit("/", 1)[0] + "/"


def outside_scope():
    """Changed paths outside the scope and the loop folder. A task must not change these."""
    allowed = tuple(C.scope) + (loop_folder(),)
    return sorted(path for path in W.changed_paths() if not path.startswith(allowed))


def leftover_changes():
    """Uncommitted changes in the scope: what a task would build on if it started now."""
    return sorted(path for path in W.changed_paths() if path.startswith(tuple(C.scope)))


def undo_outside(paths):
    """Saves, then undoes, changes a task made outside the scope: tracked files go back to HEAD, new files are deleted,
    and the folders that leaves empty are removed."""
    if not paths:
        return
    backup = C.logs / f"revert-{time.strftime('%Y%m%d-%H%M%S')}"
    for path in paths:
        source = C.project / path
        if source.is_file():
            target = backup / "files" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        if git("cat-file", "-e", f"HEAD:{path}").returncode == 0:
            git("checkout", "HEAD", "--", path)
        else:
            source.unlink(missing_ok=True)
            folder = source.parent
            while folder != C.project and folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
                folder = folder.parent
    log(f"undid changes outside the scope, saved in {backup}: {', '.join(paths[:5])}")


def revert_unfinished():
    """Undoes an unfinished task: the scope's changes, through the worker's --revert (which keeps a backup), and then
    anything the task changed outside the scope and the loop folder."""
    done = subprocess.run([sys.executable, str(PATHS["worker"]), "--revert"], cwd=C.project, env=driver_env(),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    log(f"revert: {(done.stdout or done.stderr).strip()[-300:]}")
    undo_outside(outside_scope())


def clear_leftovers(task_id):
    """A task starts from a clean scope. Changes outside the scope and the loop folder are not the driver's to undo, so
    they stop the run for the person to look at. Leftovers in the scope are saved and reverted. True means go on."""
    outside = outside_scope()
    if outside:
        log(f"not starting {task_id}: uncommitted changes outside the scope: {', '.join(outside[:5])}")
        return False
    if not leftover_changes():
        return True
    log(f"leftover changes in the scope before {task_id}: {', '.join(leftover_changes()[:5])}")
    revert_unfinished()
    if leftover_changes():
        log(f"still dirty before {task_id} after the revert: {', '.join(leftover_changes()[:5])}")
        return False
    return True


def done_when(spec):
    """The task's Done when checks as (kind, value) pairs: test `name`, file `path` or output `text`. The line may wrap."""
    lines = spec.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("**Done when.**"):
            paragraph = []
            for part in lines[index:]:
                if not part.strip():
                    break
                paragraph.append(part)
            return DONE_WHEN_ITEM.findall(" ".join(paragraph))
    return []


def done_files(spec):
    """The files a task's Done when line names. They are committed with the scope."""
    return [value for kind, value in done_when(spec) if kind == "file"]


def check_done_when(spec, output, required=True):
    """The first Done when check that does not hold, or ''. A test is a function of that name, in any language, in a file
    under the scope. Without the line, the task is refused when required is set."""
    checks = done_when(spec)
    if not checks:
        return "the task has no **Done when.** line, so the driver cannot tell that it was done" if required else ""
    for kind, value in checks:
        if kind == "file":
            if not (C.project / value).exists() or not git("status", "--porcelain", "--", value).stdout.strip():
                return f"Done when: {value} was not written"
        elif kind == "test":
            pattern = re.compile(rf"\b{re.escape(value)}\s*\(")
            files = [path for folder in C.scope for path in (C.project / folder).rglob("*") if path.is_file()]
            if not any(pattern.search(path.read_text(encoding="utf-8", errors="replace")) for path in files):
                return f"Done when: no test function {value} in the scope"
        elif value not in output:
            return f"Done when: the test run did not print {value!r}"
    return ""


def check_passed(run, spec="", require_done_when=False):
    """What the project requires before a commit, checked here and not taken from the worker's word: nothing changed
    outside the scope, no test removed, the test run passes now, and the task's Done when line holds. The tests run
    once. Returns (reason, totals); the reason is empty when the change passes."""
    extra = outside_scope()
    if extra:
        return f"changed outside the scope: {', '.join(extra[:3])}", ""
    if run.get("tests_removed"):
        return f"{run['tests_removed']} tests were removed", ""
    before, after = RUNTIME.get("tests_before"), W.count_tests()
    if before is not None and after is not None and after < before:
        return f"tests were removed: {before} before the task, {after} now", ""
    output = W.test_output()
    totals = W.totals_line(output)
    if not totals or C.pass_marker not in totals:
        return f"the driver's own test run: {totals or 'no totals printed'}", totals
    return check_done_when(spec, output, require_done_when), totals


def block(task_id, reason, outcome):
    """Undoes an unfinished task and marks it blocked. The phone is not told: the driver moves on by itself."""
    if RUNTIME.get("stopping"):
        return
    revert_unfinished()
    set_heading(task_id, "!")
    add_progress(blocked=f"- {task_id}: {reason}")
    commit(f"{task_id}: blocked\n\n{reason}\n", [C.tasks_file, C.progress_file])
    log(f"blocked {task_id} ({outcome}): {reason}")


def finish_with_commit(task_id, title, totals, body, trailer="", extra_paths=()):
    """Marks a task done, records it in the progress file, commits it, and tells the phone."""
    set_heading(task_id, "x")
    add_progress(row=f"| {datetime.date.today().isoformat()} | {task_id} | done | {totals} | {title} |")
    commit(f"{task_id}: {title}\n\n{body}{trailer}\n",
           list(C.scope) + list(extra_paths) + [C.tasks_file, C.progress_file])
    post("Committed", f"{task_id} {title}: {totals}")


def event_parts(event):
    """What one stream-json event says, as (kind, value) pairs: ("say", text), ("step", tool and input),
    ("tool_result", text), ("system", subtype and message) and ("result", the event). Any other shape gives nothing,
    so an event the parser does not know never stops a session."""
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    message = event.get("message")
    if kind == "result":
        return [("result", event)]
    if kind == "system":
        # why: a system event (such as permission_denied) carries its message as a plain string
        return [("system", f"{event.get('subtype', 'system')}: {message}")] if isinstance(message, str) else []
    content = message.get("content") if isinstance(message, dict) else None
    parts = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if kind == "assistant" and part.get("type") == "text" and str(part.get("text", "")).strip():
            parts.append(("say", str(part["text"]).strip()))
        elif kind == "assistant" and part.get("type") == "tool_use":
            parts.append(("step", f"{part.get('name')} {json.dumps(part.get('input'))[:160]}"))
        elif kind == "user" and part.get("type") == "tool_result":
            parts.append(("tool_result", str(part.get("content"))[:120].replace("\n", " | ")))
    return parts


def run_session(prompt, task_id, model, effort, turns, timeout, budget, kind):
    """Runs one Claude session with tools. Each step is logged as it happens, and the raw stream is kept in
    <task>.<kind>.jsonl. Returns (text, error); one of them is empty."""
    command = [C.claude_exe, "-p", "--model", model, "--tools", TASK_TOOLS, "--allowedTools", *TASK_TOOLS.split(","),
               "--disable-slash-commands", "--strict-mcp-config", "--output-format", "stream-json", "--verbose",
               "--max-turns", str(turns), "--max-budget-usd", str(budget)]
    if effort:
        command += ["--effort", effort]
    if C.deny_tools:
        command += ["--disallowedTools", *C.deny_tools]
    group = {} if os.name == "nt" else {"start_new_session": True}   # its own process group, so the kill reaches it all
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               cwd=C.project, env=W.helper_env(), text=True, encoding="utf-8", errors="replace",
                               bufsize=1, **group)
    CHILD["pid"] = process.pid
    stop_polling = threading.Event()
    threading.Thread(target=poll_while, args=(stop_polling,), daemon=True).start()
    timer = threading.Timer(timeout, W.kill_tree, args=(process.pid,))
    timer.start()
    process.stdin.write(prompt)
    process.stdin.close()
    started = time.time()
    steps = 0
    result = {}
    texts = []
    try:
        with open(C.logs / f"{task_id}.{kind}.jsonl", "w", encoding="utf-8", newline="\n") as raw:
            for line in process.stdout:
                raw.write(line)
                try:
                    event = json.loads(line)
                except ValueError:
                    log(f"{model} output: {line.rstrip()[:200]}")
                    continue
                for part, value in event_parts(event):
                    if part == "result":
                        result = value
                    elif part == "say":
                        texts.append(value)
                        log(f"{model} says: {value[:200]}")
                    elif part == "step":
                        steps += 1
                        log(f"{model} step {steps}: {value}")
                    elif part == "tool_result":
                        log(f"{model} tool result: {value}")
                    else:
                        log(f"{model} {value[:200]}")
        code = process.wait()
    finally:
        CHILD["pid"] = None
        stop_polling.set()
        timer.cancel()
    seconds = round(time.time() - started)
    if result:
        W.log_usage({"who": model, "kind": kind, "effort": effort, "is_error": bool(result.get("is_error")),
                     "turns": result.get("num_turns"), "seconds": seconds,
                     "cost_usd": result.get("total_cost_usd"), "usage": result.get("usage")})
        log(f"{model} finished in {seconds} s: {result.get('num_turns')} turns, cost {result.get('total_cost_usd')} USD, "
            f"error {bool(result.get('is_error'))}, {result.get('subtype', '')}")
    if not result or result.get("is_error") or not result.get("result"):
        detail = str(result.get("result") or result.get("subtype") or "no result")[:300]
        return "", f"the {model} session ended with exit {code} after {seconds} s: {detail}"
    return str(result["result"]).strip() or "\n".join(texts), ""


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
    if not USAGE_LIMIT.search(error):
        return False
    resume = reset_time(error)
    state["resume_at"] = resume + USAGE_RESUME_MARGIN if resume else None
    if state["resume_at"]:
        when = time.strftime("%a %H:%M", time.localtime(state["resume_at"]))
        text = (f"Session usage is exhausted, so the loop stopped during {task_id}. It restarts at {when} (machine time), "
                f"4 minutes after usage reopens. Send RESUME to restart it sooner. {error[:200]}")
    else:
        when = "unknown"
        text = (f"Session usage is exhausted, so the loop stopped during {task_id}. No restart time could be read from "
                f"the message, so it waits for RESUME. {error[:200]}")
    log(f"usage limit reached during {task_id}: restart at {when}, the task stays open")
    pause(state, "Loop stopped: session usage exhausted", text, "urgent")
    return True


def over_budget(state):
    """True when today's spending in the usage log has reached daily_budget_usd. The driver then pauses until just after
    midnight, and the phone is told once."""
    budget = RUNTIME["settings"]["daily_budget_usd"]
    spent = spend_on()
    if budget <= 0 or spent < budget:
        return False
    tomorrow = datetime.datetime.combine(datetime.date.today() + datetime.timedelta(days=1), datetime.time())
    state["resume_at"] = tomorrow.timestamp() + USAGE_RESUME_MARGIN
    log(f"daily budget reached: ${spent:.2f} of ${budget:g}")
    pause(state, "Loop paused: daily budget reached", f"${spent:.2f} spent today, and the budget is ${budget:g} "
          "(daily_budget_usd in the loop file). It restarts after midnight, or send RESUME.", "urgent")
    return True


def model_attempt(task_id, title, spec, model, effort, feedback):
    """One session of the model does the task. Returns None when nothing more is to be done (committed, stopped, or the
    usage limit paused the driver), or the reason it failed."""
    W.CURRENT_TASK["id"] = task_id
    rules_path = C.project / C.rules_file
    rules = rules_path.read_text(encoding="utf-8") if rules_path.exists() else "(no rules file)"
    prompt = TASK_PROMPT.format(brief=C.project_brief, scope=", ".join(C.scope), loop=loop_folder(),
                                test_command=C.test_command_text, rules_file=C.rules_file, rules=rules, task=spec)
    if feedback:
        prompt += FEEDBACK_NOTE.format(model=feedback[0], reason=feedback[1], tests=W.test_output()[-4000:])
    log(f"task starts: {task_id}: {title} ({model}, effort {effort or 'default'}, {len(prompt)} characters, "
        f"up to {C.task_turns} turns, ${MODEL_BUDGET_USD[model]:g})")
    text, error = run_session(prompt, task_id, model, effort, C.task_turns, C.task_timeout, MODEL_BUDGET_USD[model],
                              "task")
    if RUNTIME.get("stopping"):
        return None
    (C.logs / f"{task_id}.{model}.md").write_text((text or error) + "\n", encoding="utf-8", newline="\n")
    if error:
        if hit_usage_limit(task_id, error, RUNTIME["state"]):
            return None
        return f"{model} could not do it: {error}"
    reason, totals = check_passed({}, spec)
    if reason:
        log(f"task check failed: {reason}")
        return reason
    name = MODEL_NAMES[model]
    body = f"Written by {name} (effort {effort or 'default'}), checked by the driver.\n\nIts own summary:\n{text[:1500]}"
    finish_with_commit(task_id, title, totals, body, f"\n\nCo-Authored-By: {name} <noreply@anthropic.com>",
                       done_files(spec))
    return None


def model_task(task_id, title, spec, state):
    """The task model does the task. If it fails, its changes are undone and the escalation model tries once, with the
    failure as feedback. A second failure blocks the task. An Opus or Fable attempt waits for approval first."""
    model, effort = task_model(state)
    escalate = RUNTIME["settings"]["escalate_model"]
    plan = [(model, effort)]
    if escalate and MODEL_RANK[escalate] > MODEL_RANK[model]:
        plan.append((escalate, effort))
    reason = ""
    for index, (who, level) in enumerate(plan):
        if needs_approval(who, level) and not approved(state, f"{task_id} {title}: {who}, effort {level or 'default'}, "
                                                              f"up to ${MODEL_BUDGET_USD[who]:g}"):
            if index == 0:
                pause(state, "Loop paused: not approved", f"{task_id} stays open. Send RESUME to ask again, or MODEL "
                      "sonnet to use another model.")
                return
            reason += f" (the retry by {who} was not approved)"
            break
        if index:
            revert_unfinished()
            log(f"{task_id}: {plan[index - 1][0]} failed ({reason}); {who} tries it once")
        reason = model_attempt(task_id, title, spec, who, level, (plan[index - 1][0], reason) if index else None)
        if reason is None:
            return
    block(task_id, reason, "task_failed")


def run_worker(task_id, state, feedback=None):
    """Runs worker_task.py for a task and returns its status once it has ended."""
    command = [sys.executable, str(PATHS["worker"]), task_id]
    if feedback:
        command.append(str(feedback))
    log(f"worker starts: {task_id}" + (" (a retry, with the test output)" if feedback else ""))
    before = C.status_file.stat().st_mtime if C.status_file.exists() else 0
    group = {} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(command, cwd=C.project, env=driver_env(), **group)
    CHILD["pid"] = process.pid
    last_poll = 0.0
    while process.poll() is None:
        if time.time() - last_poll >= RUNTIME["settings"]["poll_seconds"]:
            last_poll = time.time()
            handle_commands(state)
            write_state(state)
        time.sleep(2)
    CHILD["pid"] = None
    if not C.status_file.exists() or C.status_file.stat().st_mtime <= before:
        log(f"worker did not start {task_id} (exit {process.returncode}); its status file did not change")
        return {"state": "not_started"}
    run = read_json(C.status_file)
    log(f"worker ends: {task_id}: state {run.get('state')}, exit {process.returncode}")
    return run


def worker_task(task_id, title, spec, state):
    """Runs the worker on a task. A state that allows it is retried with the test output, up to max_attempts. A task
    that passes is committed; anything else is blocked."""
    feedback = None
    while True:
        run = run_worker(task_id, state, feedback)
        outcome = run.get("state")
        if outcome == "passed":
            reason, totals = check_passed(run, spec)
            if reason:
                block(task_id, reason, outcome)
                return
            finish_with_commit(task_id, title, totals, f"Written by the worker (attempt {run.get('attempt', 1)}).",
                               extra_paths=done_files(spec))
            return
        if outcome in ("stopped", "not_started"):
            state["paused"] = True
            write_state(state)
            log(f"{task_id} was {outcome}: paused, the task stays open")
            return
        if outcome in RUNTIME["settings"]["retry_states"] and run.get("attempt", 1) < RUNTIME["settings"]["max_attempts"]:
            feedback = C.logs / f"{task_id}.feedback.txt"
            feedback.write_text(f"The last attempt ended in state {outcome}. Its test output:\n"
                                f"{W.test_output()[-4000:]}\n", encoding="utf-8", newline="\n")
            log(f"{task_id} will be retried with its test output (attempt {run.get('attempt', 1) + 1})")
            continue
        block(task_id, f"state {outcome}" + (f", {run.get('totals')}" if run.get("totals") else ""), outcome)
        return


def is_opus_task(spec):
    """A task whose Who line says Opus is a review or planning task: Opus does it once, at high effort."""
    return bool(re.search(r"^\*\*Who\.\*\* Opus\b", spec, re.M))


def do_opus_task(task_id, title, spec, state):
    """Runs a task marked Opus in one session, once the phone approves it. Changes outside the scope and the loop
    folder are refused. If code in the scope changed, the tests must pass. The task's Done when checks must hold, and
    then every change is committed."""
    if not approved(state, f"{task_id} {title}: Opus, effort {OPUS_EFFORT}, up to ${OPUS_TASK_BUDGET_USD:g}"):
        pause(state, "Loop paused: not approved", f"{task_id} stays open. Send RESUME to ask again.")
        return
    W.CURRENT_TASK["id"] = task_id
    rules_path = C.project / C.rules_file
    rules = rules_path.read_text(encoding="utf-8") if rules_path.exists() else "(no rules file)"
    prompt = OPUS_TASK_PROMPT.format(rules=rules, test_command=C.test_command_text, task=spec)
    log(f"opus task starts: {task_id}: {title}")
    text, error = run_session(prompt, task_id, OPUS_MODEL, OPUS_EFFORT, OPUS_TASK_TURNS, OPUS_TASK_TIMEOUT,
                              OPUS_TASK_BUDGET_USD, "opus")
    if RUNTIME.get("stopping"):
        return
    (C.logs / f"{task_id}.opus.md").write_text((text or error) + "\n", encoding="utf-8", newline="\n")
    if error and hit_usage_limit(task_id, error, state):
        revert_unfinished()
        return
    reason, totals = "", "-"
    if error:
        reason = f"Opus could not do it: {error}"
    elif outside_scope():
        reason = f"changed outside the scope: {', '.join(outside_scope()[:3])}"
    elif leftover_changes():
        reason, totals = check_passed({}, spec, require_done_when=True)
        totals = totals or "-"
    else:
        reason = check_done_when(spec, "")
    if reason:
        block(task_id, reason, "opus_failed")
        return
    set_heading(task_id, "x")
    add_progress(row=f"| {datetime.date.today().isoformat()} | {task_id} | done | {totals} | {title} · Opus |")
    paths = sorted(set(W.changed_paths()) | {C.tasks_file, C.progress_file})
    commit(f"{task_id}: {title}\n\n{(text or '')[:1500]}\n\nCo-Authored-By: {MODEL_NAMES['opus']} "
           "<noreply@anthropic.com>\n", paths)
    post("Committed", f"{task_id} {title} (Opus)")


def do_task(task_id, title, state, solo):
    spec = W.find_spec(task_id)
    if not clear_leftovers(task_id):
        pause(state, "Driver paused", f"Could not start {task_id}: changes are left over from an earlier run. Look at "
              "the driver log, then send RESUME.")
        return
    RUNTIME["tests_before"] = W.count_tests()
    if is_opus_task(spec):
        do_opus_task(task_id, title, spec, state)
    elif solo:
        model_task(task_id, title, spec, state)
    else:
        worker_task(task_id, title, spec, state)


def task_crashed(task_id, error, errors_in_a_row, state):
    """A task raised an error the driver did not expect. Its changes are undone and it is blocked with the error, so the
    loop goes on. If blocking fails too, or MAX_TASK_ERRORS tasks in a row hit an error, the driver pauses."""
    log(f"driver error during {task_id}: {error}\n{traceback.format_exc()[-1500:]}")
    if RUNTIME.get("stopping"):
        return
    try:
        block(task_id, f"the driver hit an error during the task: {str(error)[:300]}", "driver_error")
    except Exception as second:
        log(f"could not block {task_id} after the error: {second}")
        pause(state, "Driver paused: error", f"{task_id}: {str(error)[:200]}. Blocking it failed too ({second}). "
              "Look at the driver log, then send RESUME.", "urgent")
        return
    if errors_in_a_row >= MAX_TASK_ERRORS:
        pause(state, "Driver paused: errors", f"{errors_in_a_row} tasks in a row hit a driver error, the last in "
              f"{task_id}: {str(error)[:200]}. Look at the driver log, then send RESUME.", "urgent")


def main(solo=True):
    other = read_pid(PATHS["pid"])
    if other and other != os.getpid() and W.pid_alive(other):
        print(f"a driver is already running (pid {other}). Stop it with: python driver.py --shutdown, or send "
              f"SHUTDOWN NOW. If pid {other} is not a driver, delete {PATHS['pid']}")
        return ALREADY_RUNNING
    state = read_state()
    state.update({"paused": False, "resume_at": None, "wind_down": False, "busy": False, "solo": solo, "approvals": {},
                  "mode": ("model alone" if solo else "worker with Haiku review")
                  + (", supervised" if SUPERVISED else "")})
    RUNTIME["state"] = state
    PATHS["pid"].write_text(str(os.getpid()), encoding="utf-8", newline="\n")
    write_state(state)
    refresh_settings()
    log(f"driver started ({state['mode']}), not paused. {model_text(state)}")
    was_paused = False
    errors_in_a_row = 0
    while True:
        refresh_settings()
        handle_commands(state)
        if state.get("wind_down"):
            state["wind_down"] = False
            write_state(state)
            post("Loop exited", "The driver has ended. Start it again to continue.")
            log("driver ends on request")
            return 0
        if state.get("paused") and state.get("resume_at") and time.time() >= state["resume_at"]:
            state.update({"paused": False, "resume_at": None})
            write_state(state)
            log("the pause has run out: resuming")
            post("Loop resumed", "The usage limit or the daily budget has reset, so the driver carries on.")
        if state.get("paused") or over_budget(state):
            if not was_paused:
                log("paused: no new task starts until RESUME")
            was_paused = True
            time.sleep(RUNTIME["settings"]["poll_seconds"])
            continue
        was_paused = False
        found = next_task()
        if found is None:
            pause(state, "Loop finished", "No open tasks are left in the task file. Send RESUME after you add some.",
                  "default")
            continue
        task_id, title = found
        state.update({"busy": True, "task": task_id, "task_started": now()})
        write_state(state)
        try:
            do_task(task_id, title, state, solo)
            errors_in_a_row = 0
        except Exception as error:
            errors_in_a_row += 1
            task_crashed(task_id, error, errors_in_a_row, state)
        state.update({"busy": False, "task": None})
        write_state(state)


def supervise(args):
    """Runs the driver (--run) and starts it again when it crashes. A clean end (WIND DOWN, SHUTDOWN NOW) ends the
    supervisor too. Each crash soon after a start doubles the wait, and after MAX_QUICK_CRASHES it gives up."""
    other = running_driver()
    if other:
        print(f"a driver is already running (pid {other}). Stop it with python driver.py --shutdown first.")
        return ALREADY_RUNNING
    PATHS["supervisor"].write_text(str(os.getpid()), encoding="utf-8", newline="\n")
    env = {**os.environ, "DRIVER_SUPERVISED": "1", "WORKER_CONFIG": str(C.config_path)}
    quick_crashes = 0
    try:
        while True:
            started = time.time()
            code = subprocess.call([sys.executable, str(pathlib.Path(__file__).resolve()), "--run", *args], env=env)
            if code in (0, ALREADY_RUNNING):
                log(f"supervisor ends: the driver ended with exit {code}")
                return code
            quick_crashes = quick_crashes + 1 if time.time() - started < STABLE_SECONDS else 1
            if quick_crashes > MAX_QUICK_CRASHES:
                log(f"supervisor gives up after {MAX_QUICK_CRASHES} quick crashes")
                post("Loop driver down", f"It crashed {MAX_QUICK_CRASHES} times soon after starting, so the supervisor "
                     "stopped restarting it. Look at the driver log, then start python driver.py.", "urgent")
                return code
            wait = min(RESTART_WAIT * 2 ** (quick_crashes - 1), RESTART_WAIT_MAX)
            log(f"supervisor: the driver ended with exit {code}; restarting it in {wait} s")
            time.sleep(wait)
    finally:
        if read_pid(PATHS["supervisor"]) == os.getpid():
            PATHS["supervisor"].unlink(missing_ok=True)


def run_driver(solo):
    """--run: the driver itself. An error it did not catch is logged and sent to the phone, and the process ends with
    exit 1, which the supervisor restarts."""
    try:
        return main(solo)
    except KeyboardInterrupt:
        log("driver stopped by Ctrl+C")
        return 0
    except Exception as error:
        log(f"driver crashed: {error}\n{traceback.format_exc()[-1500:]}")
        then = ("The supervisor starts it again shortly." if SUPERVISED
                else "It is not running: start it on the machine with python driver.py.")
        post("Loop driver crashed", f"{error}. {then}", "urgent")
        return 1
    finally:
        if read_pid(PATHS["pid"]) == os.getpid():
            PATHS["pid"].unlink(missing_ok=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    W.configure()
    init_paths()
    argv = sys.argv[1:]
    if "--shutdown" in argv:
        cli_shutdown()
        sys.exit(0)
    for flag, word in (("--model", "MODEL"), ("--approve", "APPROVE"), ("--deny", "DENY")):
        if flag in argv:
            queue_request(" ".join([word, *argv[argv.index(flag) + 1:]]))
            sys.exit(0)
    mode_args = ["--worker"] if "--worker" in argv else []
    sys.exit(run_driver(solo=not mode_args) if "--run" in argv else supervise(mode_args))
