"""The loop driver: runs the open tasks in the task file one at a time, without an orchestrating session.

Usage, from the folder that holds worker.json (or with WORKER_CONFIG set):
  python driver.py             Haiku alone does each task in one session, with tools and no review (the default)
  python driver.py --worker    the worker does each task; Haiku reviews it and sends it back (worker_task.py)
  python driver.py --shutdown  ends a running driver and its session, reverts the unfinished task, and stops

A task is committed only after the driver's own checks pass: nothing changed outside the scope, no test removed, and
the test run passes now. A task that fails is reverted and marked blocked. The phone is not told about that, because
the driver moves on by itself. The phone gets each commit, a finished queue, a paused driver, a failed sync, and the
answers to commands. Commands are read from the phone in any case, as the whole message:
  STATUS              a progress report now
  MAN                 the list of commands
  STOP or PAUSE       no new task starts until RESUME; the driver stays up
  RESUME or RESTART   carry on
  WIND DOWN           the driver ends after the current step
  SHUTDOWN NOW        the running session is killed, and the driver ends

Config: the worker's worker.json. The driver reads loop_file, progress_file, open_task_pattern, rules_file,
haiku_brief, sync, haiku_turns and haiku_timeout from it. loop_file holds a driver-settings block (poll_seconds,
max_attempts, retry_states), which is read again at each pass, so an edit takes effect without a restart.
A task heading is "### [ ] ID title" while it is open, and "### [x] ID title" once it is done.
"""
import datetime
import json
import os
import pathlib
import re
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
SHUTDOWN NOW - kill the running session now and end the driver"""
SETTING_DEFAULTS = {
    "poll_seconds": "60",
    "max_attempts": "3",
    "retry_states": "tests_failed, failed, looped, timed_out, tests_did_not_run",
}
HAIKU_TOOLS = "Bash,Read,Edit,Write,Glob,Grep"
CO_AUTHOR_HAIKU = "Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>"
HAIKU_PROMPT = """You are doing one task in {brief}, alone: no other model reviews your work. In this session:
1. Read the task below. Where it gives example values or expected results, a test must check exactly those. If a test
   you write disagrees with the task, the test is wrong: fix the test to match the task.
2. Change files only under: {scope}. Create only the files the task names. Your shell starts in the project root, and
   every path is written from there. Never delete, skip or weaken an existing test. Do not change files under {loop}.
3. Run the tests with: {test_command}
   Fix any failure and run them again, until a run reports no failures.
4. Then stop. Your last message is the list of files you changed and the totals line from the last run, nothing else.
Do not run git commands that change files or the index.

The project rules ({rules_file}):
{rules}

The task:
{task}
"""


USAGE_RESUME_MARGIN = 240  # why: restart 4 minutes after usage reopens, so the first call is not refused again
USAGE_LIMIT = re.compile(r"session limit|usage limit", re.I)
DONE_WHEN_ITEM = re.compile(r"\b(test|file|output) `([^`]+)`")
OPUS_MODEL = os.environ.get("OPUS_MODEL", "opus")
OPUS_EFFORT = "high"
OPUS_TASK_TURNS = 200
OPUS_TASK_TIMEOUT = 7200
OPUS_TASK_BUDGET_USD = 25.0
CO_AUTHOR_OPUS = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
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
    """The driver settings as the types they are used as. Raises ValueError for a value that is not a number."""
    return {"poll_seconds": max(5, int(values["poll_seconds"])), "max_attempts": int(values["max_attempts"]),
            "retry_states": tuple(part.strip() for part in values["retry_states"].split(",") if part.strip())}


RUNTIME = {"state": {}, "settings": parse_settings(SETTING_DEFAULTS), "last": None}
CHILD = {"pid": None}
PATHS = {}


def init_paths():
    """Sets the paths that depend on the config. The logs folder is made first, so the pid file can be written at once."""
    C.logs.mkdir(parents=True, exist_ok=True)
    PATHS["pid"] = C.logs / "driver.pid"
    PATHS["state"] = C.logs / "driver.json"
    PATHS["log"] = C.logs / "driver.log"
    PATHS["loop"] = C.project / C.loop_file
    PATHS["progress"] = C.project / C.progress_file
    PATHS["worker"] = pathlib.Path(W.__file__).resolve()


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    with open(PATHS["log"], "a", encoding="utf-8") as record:
        record.write(f"{now()} {message}\n")
    print(f"[driver] {message}", flush=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_state():
    return read_json(PATHS["state"]) or {"paused": False}


def write_state(state):
    state["heartbeat"] = now()
    state["child_pid"] = CHILD["pid"]
    PATHS["state"].write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8", newline="\n")


def post(title, body, priority="default"):
    """Sends one message to the phone and logs it. The driver's own messages are always sent: the worker's phone_titles
    filter applies only to the worker's alerts."""
    log(f"phone [{priority}] {title}: {body[:120]}")
    if not C.topic:
        return
    request = urllib.request.Request(f"{C.ntfy_server}/{C.topic}", data=body[:500].encode("utf-8"),
                                     headers={"Title": title, "Priority": priority}, method="POST")
    try:
        urllib.request.urlopen(request, timeout=20).read()
    except Exception as error:
        log(f"phone send failed: {error}")


def driver_env():
    """The environment for a worker run or a revert: the same config, and the flag that tells the worker the driver
    answers the phone's inbox."""
    env = dict(os.environ)
    env["WORKER_CONFIG"] = str(C.config_path)
    env["WORKER_UNDER_DRIVER"] = "1"
    return env


def status_text(state):
    """One line for the phone: whether the loop runs, and what the current task is doing."""
    paused = "paused" if state.get("paused") else "running"
    if not state.get("busy"):
        return f"Loop {paused}. No task is running."
    text = f"Loop {paused}. Task {state.get('task')} ({state.get('mode')}), started {state.get('task_started')}."
    if not state.get("solo"):
        run = W.read_status()
        if run.get("task") == state.get("task"):
            text += f" Attempt {run.get('attempt')}: {run.get('phase_detail') or run.get('state', '')}."
    return text


def handle_commands(state):
    """Reads the phone's inbox and does each command in it. Each message is read once. A message that is not a command
    is ignored; a short one is logged, in case it was a typo, and a long one (such as the driver's own replies) is not."""
    seen = W.read_seen()
    for message_id, text in W.poll_inbox():
        if message_id in seen:
            continue
        W.mark_seen(message_id)
        seen.add(message_id)
        command = " ".join(text.split()).upper()
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
            state["paused"] = False
            write_state(state)
            post("Loop resumed", "Carrying on with the next open task.")


def poll_while(stop):
    """Runs in its own thread while a Haiku session runs: answers the phone every poll_seconds, so STATUS, STOP,
    WIND DOWN and SHUTDOWN NOW work in the middle of a task. SHUTDOWN NOW ends the whole driver from here."""
    while not stop.wait(RUNTIME["settings"]["poll_seconds"]):
        try:
            handle_commands(RUNTIME["state"])
            write_state(RUNTIME["state"])
        except SystemExit:
            PATHS["pid"].unlink(missing_ok=True)
            log("driver ended by SHUTDOWN NOW")
            os._exit(0)


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
    """The pid of another driver that is still running from this folder, or 0."""
    try:
        other = int(PATHS["pid"].read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    return other if other != os.getpid() and W.pid_alive(other) else 0


def cli_shutdown():
    """python driver.py --shutdown: stops a running driver and its session, from the machine it runs on."""
    state = read_state()
    try:
        pid = int(PATHS["pid"].read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = 0
    if state.get("child_pid") and W.pid_alive(state["child_pid"]):
        W.kill_tree(state["child_pid"])
    if pid and W.pid_alive(pid):
        W.kill_tree(pid)
    if state.get("busy"):
        revert_unfinished()
    state.update({"busy": False, "paused": True, "child_pid": None})
    write_state(state)
    PATHS["pid"].unlink(missing_ok=True)
    log("shut down from the machine")
    print("driver shut down; nothing is running from it. Start it again with python driver.py")


def refresh_settings():
    """Reads the driver-settings block in the loop file. A value that cannot be read keeps its default, and a change to
    the settings is logged."""
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
    """Commits the paths that exist, with the message, and then syncs if sync is on."""
    git("add", "--", *[path for path in paths if (C.project / path).exists()])
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
    """Saves, then undoes, changes a task made outside the scope: tracked files go back to HEAD, new files are deleted."""
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


def check_passed(run):
    """What the project requires before a commit, checked here and not taken from the worker's word: nothing changed
    outside the scope, no test removed, and the test run passes now. Returns (reason, totals). The reason is empty when
    the change passes."""
    extra = outside_scope()
    if extra:
        return f"changed outside the scope: {', '.join(extra[:3])}", ""
    if run.get("tests_removed"):
        return f"{run['tests_removed']} tests were removed", ""
    totals = W.totals_line(W.test_output())
    if not totals or C.pass_marker not in totals:
        return f"the driver's own test run: {totals or 'no totals printed'}", totals
    return "", totals


def block(task_id, reason, outcome):
    """Undoes an unfinished task and marks it blocked. The phone is not told: the driver moves on by itself."""
    if RUNTIME.get("stopping"):
        return
    revert_unfinished()
    set_heading(task_id, "!")
    add_progress(blocked=f"- {task_id}: {reason}")
    commit(f"{task_id}: blocked\n\n{reason}\n", [C.tasks_file, C.progress_file])
    log(f"blocked {task_id} ({outcome}): {reason}")


def finish_with_commit(task_id, title, totals, body, trailer=""):
    """Marks a task done, records it in the progress file, commits it, and tells the phone."""
    set_heading(task_id, "x")
    add_progress(row=f"| {datetime.date.today().isoformat()} | {task_id} | done | {totals} | {title} |")
    commit(f"{task_id}: {title}\n\n{body}{trailer}\n", list(C.scope) + [C.tasks_file, C.progress_file])
    post("Committed", f"{task_id} {title}: {totals}")


def haiku_stream(prompt, task_id, model=None, turns=None, timeout=None, extra=(), who="haiku"):
    """Runs one session with tools: Haiku by default, or the model given (Opus tasks). Each step is logged as it happens,
    and the raw stream is kept in <task>.<who>.jsonl. Returns (text, error); one of them is empty."""
    command = [C.claude_exe, "-p", "--model", model or C.helper_model, "--tools", HAIKU_TOOLS,
               "--allowedTools", *HAIKU_TOOLS.split(","), "--disable-slash-commands", "--strict-mcp-config",
               "--output-format", "stream-json", "--verbose", "--max-turns", str(turns or C.haiku_turns), *extra]
    if C.deny_tools:
        command += ["--disallowedTools", *C.deny_tools]
    extra = {} if os.name == "nt" else {"start_new_session": True}   # its own process group, so the kill reaches it all
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               cwd=C.project, env=W.helper_env(), text=True, encoding="utf-8", errors="replace",
                               bufsize=1, **extra)
    CHILD["pid"] = process.pid
    stop_polling = threading.Event()
    threading.Thread(target=poll_while, args=(stop_polling,), daemon=True).start()
    timer = threading.Timer(timeout or C.haiku_timeout, W.kill_tree, args=(process.pid,))
    timer.start()
    process.stdin.write(prompt)
    process.stdin.close()
    started = time.time()
    steps = 0
    result = {}
    with open(C.logs / f"{task_id}.{who}.jsonl", "w", encoding="utf-8", newline="\n") as raw:
        for line in process.stdout:
            raw.write(line)
            try:
                event = json.loads(line)
            except ValueError:
                log(f"haiku output: {line.rstrip()[:200]}")
                continue
            if not isinstance(event, dict):
                continue
            message = event.get("message")
            # why: a system event (such as permission_denied) carries its message as a plain string
            content = message.get("content") if isinstance(message, dict) else None
            parts = content if isinstance(content, list) else []
            if event.get("type") == "system" and isinstance(message, str):
                log(f"haiku {event.get('subtype', 'system')}: {message[:200]}")
            if event.get("type") == "assistant":
                for part in parts:
                    if part.get("type") == "text" and part.get("text", "").strip():
                        log(f"haiku says: {part['text'].strip()[:200]}")
                    elif part.get("type") == "tool_use":
                        steps += 1
                        log(f"haiku step {steps}: {part.get('name')} {json.dumps(part.get('input'))[:160]}")
            elif event.get("type") == "user":
                for part in parts:
                    if part.get("type") == "tool_result":
                        log(f"haiku tool result: {str(part.get('content'))[:120]!r}")
            elif event.get("type") == "result":
                result = event
    code = process.wait()
    CHILD["pid"] = None
    stop_polling.set()
    timer.cancel()
    seconds = round(time.time() - started)
    if result:
        W.log_usage({"who": who, "kind": "haiku-alone" if who == "haiku" else "opus-task",
                     "is_error": bool(result.get("is_error")),
                     "turns": result.get("num_turns"), "seconds": seconds,
                     "cost_usd": result.get("total_cost_usd"), "usage": result.get("usage")})
        log(f"haiku finished in {seconds} s: {result.get('num_turns')} turns, cost {result.get('total_cost_usd')} USD, "
            f"error {bool(result.get('is_error'))}")
    if not result or result.get("is_error") or not result.get("result"):
        return "", (f"Haiku's session ended with exit {code} after {seconds} s: "
                    f"{str(result.get('result', 'no result'))[:300]}")
    return str(result["result"]).strip(), ""


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
    state.update({"paused": True, "resume_at": resume + USAGE_RESUME_MARGIN if resume else None})
    write_state(state)
    if state["resume_at"]:
        when = time.strftime("%a %H:%M", time.localtime(state["resume_at"]))
        text = (f"Session usage is exhausted, so the loop stopped during {task_id}. It restarts at {when} (machine time), "
                f"4 minutes after usage reopens. Send RESUME to restart it sooner. {error[:200]}")
    else:
        when = "unknown"
        text = (f"Session usage is exhausted, so the loop stopped during {task_id}. No restart time could be read from "
                f"the message, so it waits for RESUME. {error[:200]}")
    log(f"usage limit reached during {task_id}: restart at {when}, the task stays open")
    post("Loop stopped: session usage exhausted", text, "urgent")
    return True


def haiku_alone(task_id, title, spec):
    """Haiku does the whole task in one session: no worker and no review. Its change goes through the same checks as a
    worker run, and is committed with Haiku's co-author line."""
    W.CURRENT_TASK["id"] = task_id
    rules_path = C.project / C.rules_file
    rules = rules_path.read_text(encoding="utf-8") if rules_path.exists() else "(no rules file)"
    prompt = HAIKU_PROMPT.format(brief=C.haiku_brief, scope=", ".join(C.scope), loop=loop_folder(),
                                 test_command=C.test_command_text, rules_file=C.rules_file, rules=rules, task=spec)
    log(f"haiku alone starts: {task_id}: {title} ({len(prompt)} characters, up to {C.haiku_turns} turns)")
    text, error = haiku_stream(prompt, task_id)
    if RUNTIME.get("stopping"):
        return
    (C.logs / f"{task_id}.haiku.md").write_text((text or error) + "\n", encoding="utf-8", newline="\n")
    if error:
        if hit_usage_limit(task_id, error, RUNTIME["state"]):
            return
        block(task_id, f"Haiku could not do it: {error}", "haiku_failed")
        return
    reason, totals = check_passed({})
    if reason:
        block(task_id, reason, "haiku_failed")
        return
    body = f"Written by Haiku alone (driver): no worker and no review.\n\nHaiku's own summary:\n{text[:1500]}"
    finish_with_commit(task_id, title, totals, body, f"\n\n{CO_AUTHOR_HAIKU}")


def run_worker(task_id, state, feedback=None):
    """Runs worker_task.py for a task and returns its status once it has ended."""
    command = [sys.executable, str(PATHS["worker"]), task_id]
    if feedback:
        command.append(str(feedback))
    log(f"worker starts: {task_id}" + (" (a retry, with the test output)" if feedback else ""))
    before = C.status_file.stat().st_mtime if C.status_file.exists() else 0
    extra = {} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(command, cwd=C.project, env=driver_env(), **extra)
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


def worker_task(task_id, title, state):
    """Runs the worker on a task. A state that allows it is retried with the test output, up to max_attempts. A task
    that passes is committed; anything else is blocked."""
    feedback = None
    while True:
        run = run_worker(task_id, state, feedback)
        outcome = run.get("state")
        if outcome == "passed":
            reason, totals = check_passed(run)
            if reason:
                block(task_id, reason, outcome)
                return
            finish_with_commit(task_id, title, totals, f"Written by the worker (attempt {run.get('attempt', 1)}).")
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


def check_done_when(spec):
    """The first Done when check that does not hold, or ''. A task without the line is refused."""
    checks = done_when(spec)
    if not checks:
        return "the task has no **Done when.** line, so the driver cannot tell that it was done"
    output = W.test_output()
    for kind, value in checks:
        if kind == "file":
            if not (C.project / value).exists() or not git("status", "--porcelain", "--", value).stdout.strip():
                return f"Done when: {value} was not written"
        elif kind == "test":
            tests = [path for folder in C.scope for path in (C.project / folder).rglob("*.gd")]
            if not any(f"func {value}(" in path.read_text(encoding="utf-8", errors="replace") for path in tests):
                return f"Done when: no test function {value} in the scope"
        elif value not in output:
            return f"Done when: the test run did not print {value!r}"
    return ""


def do_opus_task(task_id, title, spec, state):
    """Runs a task marked Opus in one session. Changes outside the scope and the loop folder are refused. If code in the
    scope changed, the tests must pass. The task's Done when checks must hold, and then every change is committed."""
    W.CURRENT_TASK["id"] = task_id
    rules_path = C.project / C.rules_file
    rules = rules_path.read_text(encoding="utf-8") if rules_path.exists() else "(no rules file)"
    prompt = OPUS_TASK_PROMPT.format(rules=rules, test_command=C.test_command_text, task=spec)
    log(f"opus task starts: {task_id}: {title}")
    text, error = haiku_stream(prompt, task_id, model=OPUS_MODEL, turns=OPUS_TASK_TURNS, timeout=OPUS_TASK_TIMEOUT,
                               extra=("--effort", OPUS_EFFORT, "--max-budget-usd", str(OPUS_TASK_BUDGET_USD)),
                               who="opus")
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
        reason, totals = check_passed({})
        totals = totals or "-"
    if not reason:
        reason = check_done_when(spec)
    if reason:
        block(task_id, reason, "opus_failed")
        return
    set_heading(task_id, "x")
    add_progress(row=f"| {datetime.date.today().isoformat()} | {task_id} | done | {totals} | {title} · Opus |")
    paths = sorted(set(W.changed_paths()) | {C.tasks_file, C.progress_file})
    commit(f"{task_id}: {title}\n\n{(text or '')[:1500]}\n\n{CO_AUTHOR_OPUS}\n", paths)
    post("Committed", f"{task_id} {title} (Opus)")


def do_task(task_id, title, state, solo):
    spec = W.find_spec(task_id)
    if not clear_leftovers(task_id):
        state["paused"] = True
        write_state(state)
        post("Driver paused", f"Could not start {task_id}: changes are left over from an earlier run. Look at the "
             "driver log, then send RESUME.", "high")
        return
    if is_opus_task(spec):
        do_opus_task(task_id, title, spec, state)
    elif solo:
        haiku_alone(task_id, title, spec)
    else:
        worker_task(task_id, title, state)


def main(solo=True):
    W.configure()
    init_paths()
    other = running_driver()
    if other:
        sys.exit(f"a driver is already running (pid {other}). Stop it with: python driver.py --shutdown, or send "
                 f"SHUTDOWN NOW. If pid {other} is not a driver, delete {PATHS['pid']}")
    state = read_state()
    state.update({"paused": False, "resume_at": None, "wind_down": False, "busy": False, "solo": solo,
                  "mode": "Haiku alone" if solo else "worker with Haiku review"})
    RUNTIME["state"] = state
    PATHS["pid"].write_text(str(os.getpid()), encoding="utf-8", newline="\n")
    PATHS["pid_written"] = True
    write_state(state)
    log(f"driver started ({state['mode']}), not paused")
    was_paused = False
    while True:
        refresh_settings()
        handle_commands(state)
        if state.get("wind_down"):
            state["wind_down"] = False
            write_state(state)
            post("Loop exited", "The driver has ended. Start it again to continue.")
            log("driver ends on request")
            return
        if state.get("paused") and state.get("resume_at") and time.time() >= state["resume_at"]:
            state.update({"paused": False, "resume_at": None})
            write_state(state)
            log("usage limit has reset: resuming")
            post("Loop resumed", "The usage limit has reset, so the driver carries on.")
        if state.get("paused"):
            if not was_paused:
                log("paused: no new task starts until RESUME")
            was_paused = True
            time.sleep(RUNTIME["settings"]["poll_seconds"])
            continue
        was_paused = False
        found = next_task()
        if found is None:
            post("Loop finished", "No open tasks are left in the task file. Send RESUME after you add some.")
            state["paused"] = True
            write_state(state)
            continue
        task_id, title = found
        state.update({"busy": True, "task": task_id, "task_started": now()})
        write_state(state)
        do_task(task_id, title, state, solo)
        state["busy"] = False
        write_state(state)


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    if "--shutdown" in sys.argv[1:]:
        W.configure()
        init_paths()
        cli_shutdown()
        sys.exit(0)
    try:
        main(solo="--worker" not in sys.argv[1:])
    except KeyboardInterrupt:
        log("driver stopped by Ctrl+C")
    except Exception as error:
        if "log" in PATHS:
            log(f"driver crashed: {error}\n{traceback.format_exc()[-800:]}")
        post("Loop driver crashed", f"{error}. It is not running: start it on the machine with python driver.py.",
             "urgent")
        raise
    finally:
        if PATHS.get("pid_written"):
            PATHS["pid"].unlink(missing_ok=True)
