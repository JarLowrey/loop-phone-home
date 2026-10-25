"""Hands one task to a local worker model, run through Claude Code headless, with phone alerts through ntfy.

Usage, from anywhere (the config file says where the project is):
  python worker_task.py <task-id> [feedback-file]   run a task (a feedback file means a retry)
  python worker_task.py --status                    print the live or last run's status
  python worker_task.py --stop                      stop a running hand-off's Claude process tree
  python worker_task.py --revert                    save, then undo, uncommitted changes inside the scope paths,
                                                    and delete the new out-of-scope files the last run made

Configuration: a JSON file, by default worker.json in the current folder, or the path in WORKER_CONFIG. Every key is
described in DEFAULTS below and shown in worker.example.json. Relative paths in it are relative to the config file.

Before a run it checks: Claude Code exists, the model server answers, the worker model exists, the task exists, no
other hand-off is running, and, for a first attempt, no leftover edits inside the scope paths. A failed check stops the
run with a clear message and a phone alert, instead of a confusing error later.

While it runs it tracks what the worker is doing from Claude Code's event stream: thinking (thinking-token events
arriving), writing, running a tool (a tool call with no result yet), or waiting for the model (a tool result went back
and nothing has come out since). When the stream goes quiet it reads the GPU load (nvidia-smi, when present), so a
model that is still computing is told apart from one that has stopped.

Loop help: when the worker goes round in circles (the same tool call 3 times with no edit between, the same line of
text 4 times, the tests 3 times in a row with the same failures, or loop_idle_minutes of thinking with no tool call),
the script stops that session and asks a helper model (Claude, through the normal Claude login, read-only tools) for
advice. The worker then starts a new session with the advice and the current test output. At most max_assists times.

Live status: <logs_dir>/worker-run.json (task, attempt, process ids, phase, counts, loop help, changed files, result).

Logs, in <logs_dir>:
  <id>.log            raw Claude Code stream (JSON lines), without the one-per-token thinking events, which are counted
  <id>.readable.log   one line per event: time, source and text.
                      Sources: [worker] [tool] [claude-code] [helper] [script]
  <id>.helper-<n>.md  the helper's advice, one file per loop
  <id>.notify.log     every phone message sent

Phone messages go to the topic named in topic_file. Nothing is sent if that file is missing. While a run is going, the
inbox commands STATUS and MAN (any case) are answered; other commands are left for the orchestrating loop.

Environment overrides: WORKER_CONFIG (config path), WORKER_MODEL (worker model), CLAUDE_EXE (Claude Code path),
WORKER_NOTIFY_DRY=1 (print messages instead of sending), and for testing this script: WORKER_FINAL_TESTS=0 (skip its
own test runs), WORKER_STATUS_FILE (move the status file).
"""
import collections
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

DEFAULTS = {
    # where the project is, and the files the loop uses (relative to the config file)
    "project_root": ".",                   # the git repository root
    "tasks_file": "loop/TASKS.md",         # the task queue
    "task_heading": r"^### \[.\] {id} ",   # regex for a task's heading; {id} is replaced by the escaped task id
    "logs_dir": "loop/logs",
    "inbox_seen_file": "loop/inbox-seen.txt",
    # phone
    "topic_file": "~/.claude/hooks/ntfy-topic.txt",   # holds the alerts topic; the inbox is <topic>-inbox
    "ntfy_server": "https://ntfy.sh",
    # what the worker may change, and how to test it
    "scope": [],                           # required: path prefixes, relative to project_root, e.g. ["src/", "tests/"]
    "test_command": [],                    # required: run from project_root; prints failures and a totals line
    "totals_marker": "passed, ",           # text that marks the totals line in the test output
    "pass_marker": " 0 failed",            # text in the totals line when everything passed
    "failure_regex": r"FAIL (\w+)",        # names a failing test in the test output; used to spot loops
    "test_count_glob": "",                 # optional: test files, e.g. "tests/**/test_*.py"
    "test_count_marker": "",               # optional: text that starts one test in those files, e.g. "def test_"
    # the worker session
    "project_rules": "",                   # extra lines for the worker's prompt: language, style, shell notes
    "system_prompt": ("You are a coding agent. Read only the files the task names, and search with Grep before "
                      "opening anything large."),
    "worker_model": "",                    # required: the model name on the model server
    "model_server": "http://localhost:11434",   # an Anthropic-compatible server, such as Ollama
    "claude_exe": "claude",
    "allowed_tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
    # the helper
    "helper_model": "haiku",
    "helper_timeout": 600,
    "max_assists": 2,
    "max_review_rounds": 3,                       # reviews in all; round 1 also sends back minor issues
    "review_references": ["CLAUDE.md", "loop/DESIGN.md"],   # the documents the reviewer checks against
    # limits and timers, in seconds unless the name says minutes
    "max_minutes": 60,
    "progress_interval": 600,
    "early_progress_interval": 240,
    "early_progress_reports": 2,
    "stuck_check_interval": 210,
    "loop_idle_minutes": 15,
    "gpu_busy_percent": 20,
    "inbox_poll_interval": 60,
}
REQUIRED = ("scope", "test_command", "worker_model")

LOOP_REPEAT = 3
LOOP_REPEAT_TEXT = 4
BLOCK_WORDS = ("requires approval", "not allowed")
MODEL_ERROR_WORDS = ("issue with the selected model", "API Error")

PROMPT_HEAD = """Do this task from {tasks_file}. Change only what the task asks for.
Never delete, skip or weaken a test.
Create and change files only under: {scope}. No debug scripts, summaries or notes anywhere else.
{project_rules}
Run the tests only with this command, from the project root: {test_command}
Do not run commands in the background. Fix any failures, then run the tests again. Do not report success until they
report no failures.
Finish with the totals line from the last test run and the list of files you changed. Nothing else.
"""
HELPER_PROMPT = """A smaller coding model (the worker) is working on a task and got stuck in a loop.
It was stopped because it {reason}.

Your job is to get it unstuck. Read the files you need with Read, Grep and Glob. Do not change anything. Reply with at
most 10 short bullet points, plain text:
- what the worker is misunderstanding,
- the exact next steps: which file, which function, what to change and why,
- any test that is itself wrong (a test new in this task may be wrong; committed tests must not be weakened).
Do not write whole files. Short code lines are fine where they make a step exact.

The task:
{spec}

Current test output:
{tests}

What the worker has changed so far (git diff of the scope paths; new untracked files are not shown):
{diff}

The worker's last actions (time, source, text):
{recent}
"""
# The same list the loop sends for MAN. Keep the loop prompt's copy in step with this one.
COMMANDS_HELP = """Commands (any case):
STATUS - progress report now
MAN - this list
STOP - end the loop after this tick
PAUSE - start no new task until RESUME
RESUME - carry on after PAUSE
ANSWER: <text> - answer the last NEED_INPUT question"""

C = SimpleNamespace()   # the loaded configuration; set by configure()


# ---------------------------------------------------------------------------------------------------------------------
# Configuration

def configure():
    """Loads the config file, applies environment overrides, and fills C. Exits with a message if it cannot."""
    path = Path(os.environ.get("WORKER_CONFIG", "worker.json")).expanduser().resolve()
    if not path.exists():
        sys.exit(f"no config file at {path}. Copy worker.example.json there, or set WORKER_CONFIG.")
    settings = dict(DEFAULTS)
    settings.update(json.loads(path.read_text(encoding="utf-8")))
    if os.environ.get("WORKER_MODEL"):
        settings["worker_model"] = os.environ["WORKER_MODEL"]
    if os.environ.get("CLAUDE_EXE"):
        settings["claude_exe"] = os.environ["CLAUDE_EXE"]
    missing = [key for key in REQUIRED if not settings.get(key)]
    if missing:
        sys.exit(f"config {path} is missing: {', '.join(missing)}")

    base = path.parent

    def resolve(value):
        resolved = Path(value).expanduser()
        return resolved if resolved.is_absolute() else (base / resolved).resolve()

    C.__dict__.update(settings)
    C.config_path = path
    C.project = resolve(settings["project_root"])
    C.tasks = Path(settings["tasks_file"]) if Path(settings["tasks_file"]).is_absolute()         else C.project / settings["tasks_file"]
    C.logs = C.project / settings["logs_dir"]
    C.inbox_seen = C.project / settings["inbox_seen_file"]
    C.topic_path = Path(settings["topic_file"]).expanduser()
    C.status_file = Path(os.environ.get("WORKER_STATUS_FILE", C.logs / "worker-run.json"))
    C.stop_flag = C.status_file.with_suffix(".stop")   # --stop leaves it, so the run knows the end was asked for
    C.scope = tuple(p.replace("\\", "/") for p in settings["scope"])
    C.scope_dirs = [p.rstrip("/") for p in C.scope]
    C.test_command_text = (settings["test_command"] if isinstance(settings["test_command"], str)
                           else " ".join(settings["test_command"]))
    C.notify_dry = os.environ.get("WORKER_NOTIFY_DRY", "0") == "1"
    C.final_tests = os.environ.get("WORKER_FINAL_TESTS", "1") != "0"
    C.topic = "".join(C.topic_path.read_text(encoding="utf-8").split()) if C.topic_path.exists() else ""


# ---------------------------------------------------------------------------------------------------------------------
# Small helpers: phone, inbox, status file, processes, git

def notify(log_path, title, body, priority="default"):
    """Sends one phone message, and records it in the notify log. Failures never stop the run."""
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as record:
            record.write(f"{time.strftime('%H:%M:%S')} [{priority}] {title} | {body}\n")
    if C.notify_dry:
        print(f"[notify] {title} | {body}", flush=True)
        return
    if not C.topic:
        return
    request = urllib.request.Request(f"{C.ntfy_server}/{C.topic}", data=body[:500].encode("utf-8"),
                                     headers={"Title": title, "Priority": priority}, method="POST")
    try:
        urllib.request.urlopen(request, timeout=20).read()
    except Exception:
        pass


def poll_inbox():
    """(id, text) for each message on the inbox topic in the last 10 minutes. Empty if there is no topic or no network."""
    if not C.topic:
        return []
    try:
        body = urllib.request.urlopen(f"{C.ntfy_server}/{C.topic}-inbox/json?poll=1&since=10m", timeout=20).read()
    except Exception:
        return []
    messages = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "message":
            messages.append((event.get("id", ""), event.get("message", "")))
    return messages


def read_seen():
    if not C.inbox_seen.exists():
        return set()
    return set(C.inbox_seen.read_text(encoding="utf-8").split())


def mark_seen(message_id):
    C.inbox_seen.parent.mkdir(parents=True, exist_ok=True)
    with open(C.inbox_seen, "a", encoding="utf-8") as seen:
        seen.write(message_id + "\n")


def read_status():
    try:
        return json.loads(C.status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_status(status):
    """Writes the status file in one step, so a reader never sees half a file."""
    C.status_file.parent.mkdir(parents=True, exist_ok=True)
    temp = C.status_file.with_suffix(".tmp")
    temp.write_text(json.dumps(status, indent=2), encoding="utf-8")
    os.replace(temp, C.status_file)


def pid_alive(pid):
    """Whether a process is running. On Windows os.kill(pid, 0) would end the process, so tasklist is used there."""
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, errors="replace").stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_tree(pid):
    """Ends a process and everything it started. Use these real process ids; Git Bash's kill uses other ids on Windows."""
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def gpu_percent():
    """GPU load in percent, or None if nvidia-smi is not available."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(out.strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def git(*args):
    return subprocess.run(["git", *args], cwd=C.project, capture_output=True, text=True, errors="replace").stdout


def changed_paths():
    """Every modified, added, deleted or untracked path in the repo, relative to its root."""
    paths = set()
    for line in git("status", "--porcelain=v1", "-uall").splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path.strip('"').replace("\\", "/"))
    return paths


def in_scope(path):
    return path.startswith(C.scope)


def count_tests():
    """How many tests exist, or None when test counting is not configured."""
    if not C.test_count_glob or not C.test_count_marker:
        return None
    return sum(f.read_text(encoding="utf-8", errors="replace").count(C.test_count_marker)
               for f in C.project.glob(C.test_count_glob) if f.is_file())


def git_diff_stat():
    lines = git("diff", "--stat", "--", *C.scope_dirs).strip().splitlines()
    return lines[-1].strip() if lines else "no changes yet"


def test_output():
    """The test command's printed output, or a note when the script's own test runs are switched off."""
    if not C.final_tests:
        return "(test runs are switched off: WORKER_FINAL_TESTS=0)"
    shell = isinstance(C.test_command, str)
    done = subprocess.run(C.test_command, shell=shell, cwd=C.project, capture_output=True, text=True,
                          errors="replace")
    return (done.stdout + done.stderr).strip()


def totals_line(output):
    totals = [line for line in output.splitlines() if C.totals_marker in line]
    return totals[-1] if totals else ""


def find_spec(task_id):
    """The task's section of the task file: from its heading to the next heading or a --- line."""
    section = []
    inside = False
    heading = re.compile(C.task_heading.replace("{id}", re.escape(task_id)))
    for line in C.tasks.read_text(encoding="utf-8").splitlines():
        if heading.match(line):
            inside = True
        elif inside and (line.startswith("#") or line == "---"):
            break
        if inside:
            section.append(line)
    return "\n".join(section)


def http_ok(url, payload=None):
    try:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                         method="POST" if data else "GET")
        urllib.request.urlopen(request, timeout=20).read()
        return True
    except Exception:
        return False


def minutes_text(seconds):
    return f"{int(seconds // 60)} min {int(seconds % 60)} s"


def helper_env():
    """The normal Claude login, without the model-server settings the worker sessions use."""
    env = dict(os.environ)
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
        env.pop(key, None)
    if not env.get("ANTHROPIC_API_KEY"):
        env.pop("ANTHROPIC_API_KEY", None)
    return env


REVIEW_PROMPT = """A worker model finished a task, and its tests pass. Review the change against the task, against the rules in
these documents: {references}, and against basic code quality.

Read the files you need with Read, Grep and Glob. Do not change anything.

Sort every problem into one of two lists:
- MAJOR: the change does not do what the task asks, a rule in the documents above is broken, the logic is wrong, or a
  test hides a bug or was weakened. Passing tests is not enough: a change can pass its tests by breaking a rule.
- MINOR: style, comments, naming, stale or redundant comments, small cleanups that do not change behaviour.

Reply in exactly this form, and nothing before it:
MAJOR
- file:line, what is wrong, which rule it breaks
MINOR
- file:line, what to change
Write "- none" under a heading that has no items.

The task:
{spec}

The diff of tracked files in the scope paths:
{diff}

New files in the scope paths (names only):
{new_files}
"""


def call_helper(prompt):
    """Runs the helper model with read-only tools on a prompt. Returns (text, error); one of them is empty."""
    # The prompt goes in on stdin: on the command line a large diff passes the Windows length limit
    command = [C.claude_exe, "-p", "--model", C.helper_model,
               "--tools", "Read,Grep,Glob", "--allowedTools", "Read", "Grep", "Glob",
               "--disable-slash-commands", "--strict-mcp-config", "--output-format", "json"]
    try:
        done = subprocess.run(command, input=prompt, cwd=C.project, env=helper_env(), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=C.helper_timeout)
    except subprocess.TimeoutExpired:
        return "", f"the helper took longer than {C.helper_timeout} s"
    except OSError as error:
        return "", f"the helper could not start: {error}"
    try:
        result = json.loads(done.stdout)
    except ValueError:
        return "", f"the helper's output was not JSON: {(done.stdout or done.stderr)[:300]}"
    if result.get("is_error") or not result.get("result"):
        return "", f"the helper reported an error: {str(result.get('result'))[:300]}"
    return str(result["result"]).strip(), ""


def ask_helper(spec, tests, diff, recent, reason):
    """Asks the helper about a loop. Returns (advice, error); one of them is empty."""
    prompt = HELPER_PROMPT.format(reason=reason, spec=spec, tests=tests or "(none)", diff=diff[:8000] or "(none)",
                                  recent=recent)
    return call_helper(prompt)


def parse_review(text):
    """Splits a review into (major, minor) item lists. A reply without the MAJOR/MINOR headers counts as one major issue."""
    major, minor, section, saw_header = [], [], None, False
    for raw in text.splitlines():
        line = raw.strip()
        heading = line.strip("#*: ").upper()
        if heading.startswith("MAJOR"):
            section, saw_header = major, True
            continue
        if heading.startswith("MINOR"):
            section, saw_header = minor, True
            continue
        if section is None or not line or line[0] not in "-*•":
            continue
        item = line.lstrip("-*• ").strip()
        if item and item.lower() != "none":
            section.append(item)
    if not saw_header and text.strip():
        major.append("the review did not follow the MAJOR and MINOR format")
    return major, minor


def review_summary(major, minor):
    """One line for the phone: the counts, then the first two major items."""
    head = f"{len(major)} major, {len(minor)} minor"
    if major:
        head += ". Major: " + " | ".join(item[:90] for item in major[:2])
    return head


def review_changes(run, spec, round_no):
    """Has the helper check the change. Returns (major, minor, text, error); the script decides what rejects."""
    diff = git("diff", "--", *C.scope_dirs)
    new_files = git("ls-files", "--others", "--exclude-standard", "--", *C.scope_dirs).strip() or "(none)"
    prompt = REVIEW_PROMPT.format(references=", ".join(C.review_references) or "the project's own rules",
                                  spec=spec, diff=diff[:20000] or "(none)", new_files=new_files)
    run.set_phase("review")
    run.script_note(f"review round {round_no} of {C.max_review_rounds}: reviewing with {C.helper_model}")
    text, error = call_helper(prompt)
    if error:
        return [], [], "", error
    text_out = text + "\n"
    (C.logs / f"{run.task_id}-{round_no}.md").write_text(text_out, encoding="utf-8")
    (C.logs / f"{run.task_id}.review.md").write_text(text_out, encoding="utf-8")
    run.note("helper", f"review round {round_no}:\n{text}")
    major, minor = parse_review(text)
    return major, minor, text, ""


# ---------------------------------------------------------------------------------------------------------------------
# Loop signs

class LoopWatch:
    """Spots the worker going round in circles within one session. reason is set the first time a sign shows."""

    def __init__(self):
        self.reason = ""
        self.recent_calls = collections.deque(maxlen=12)
        self.lines = collections.Counter()
        self.test_calls = set()
        self.test_failures = []

    def flag(self, reason):
        if not self.reason:
            self.reason = reason

    def tool_call(self, call_id, name, tool_input):
        text = json.dumps(tool_input, sort_keys=True)
        if C.test_command_text in text or any(part in text for part in C.test_command_text.split()[-1:]):
            self.test_calls.add(call_id)
            return
        if name in ("Edit", "Write"):
            # a change is progress, so earlier repeats no longer count
            self.recent_calls.clear()
            return
        key = f"{name} {text}"
        self.recent_calls.append(key)
        if self.recent_calls.count(key) >= LOOP_REPEAT:
            self.flag(f"made the same {name} call {LOOP_REPEAT} times with no edit between: {text[:150]}")

    def said(self, text):
        for line in text.splitlines():
            normal = " ".join(line.lower().split())
            if len(normal) < 25:
                continue
            self.lines[normal] += 1
            if self.lines[normal] >= LOOP_REPEAT_TEXT:
                self.flag(f"repeated itself {LOOP_REPEAT_TEXT} times: \"{line.strip()[:120]}\"")

    def tool_result(self, call_id, text):
        if call_id not in self.test_calls:
            return
        self.test_failures.append(frozenset(re.findall(C.failure_regex, text)))
        last = self.test_failures[-LOOP_REPEAT:]
        if len(last) == LOOP_REPEAT and last[0] and all(failures == last[0] for failures in last):
            self.flag(f"ran the tests {LOOP_REPEAT} times in a row with the same failures: {', '.join(sorted(last[0]))}")


# ---------------------------------------------------------------------------------------------------------------------
# One run

class Run:
    """One hand-off, made of one or more Claude Code sessions. The stream thread calls observe(); the watcher thread
    reads the state it keeps."""

    def __init__(self, task_id, attempt):
        self.task_id = task_id
        self.attempt = attempt
        self.raw_path = C.logs / f"{task_id}.log"
        self.readable_path = C.logs / f"{task_id}.readable.log"
        self.notify_path = C.logs / f"{task_id}.notify.log"
        self.readable = None
        self.lock = threading.Lock()
        self.start = time.time()
        self.worker_pid = None
        self.state = "starting"
        self.tool_calls = 0
        self.thinking_total = 0
        self.blocked_alerted = False
        self.model_alerted = False
        self.timed_out = False
        self.last_claude_line = ""
        self.session = 0
        self.assists = 0
        self.loop_reasons = []
        self.review_feedback = ""
        self.last_tests = ""
        self.result = {}
        self.new_session()

    def new_session(self):
        now = time.time()
        self.session += 1
        self.phase = "starting"
        self.phase_since = now
        self.last_event = now
        self.last_action = now
        self.current_tool = ""
        self.thinking_burst = 0
        self.burst_start = 0.0
        self.loop = LoopWatch()
        self.loop_stopped = False

    # ---- logging -----------------------------------------------------------------------------------------------------

    def note(self, source, text):
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            for line in (str(text).splitlines() or [""]):
                self.readable.write(f"{stamp} [{source}] {line}\n")
            self.readable.flush()
        if source == "claude-code":
            self.last_claude_line = str(text).splitlines()[0] if str(text) else ""

    def script_note(self, message):
        self.note("script", message)
        print(f"[script] {message}", flush=True)

    def alert(self, title, body, priority="default"):
        notify(self.notify_path, title, body, priority)

    def recent_lines(self, count=80):
        with self.lock:
            self.readable.flush()
        lines = self.readable_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(line[:400] for line in lines[-count:])[-12000:]

    # ---- the event stream ------------------------------------------------------------------------------------------

    def set_phase(self, phase):
        if phase != self.phase:
            self.phase = phase
            self.phase_since = time.time()

    def end_burst(self):
        if self.thinking_burst:
            self.note("worker", f"(thinking: {self.thinking_burst} tokens over {time.time() - self.burst_start:.0f} s)")
            self.thinking_burst = 0

    def observe(self, raw_line):
        """Reads one stream line. Returns False for lines that are counted instead of logged raw."""
        self.last_event = time.time()
        line = raw_line.rstrip("\n")
        self.check_words(line)
        if not line.startswith("{"):
            self.note("claude-code", line)
            return True
        try:
            event = json.loads(line)
        except ValueError:
            self.note("claude-code", line)
            return True
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "system" and subtype == "thinking_tokens":
            if not self.thinking_burst:
                self.burst_start = time.time()
            self.thinking_burst += 1
            self.thinking_total += 1
            self.set_phase("thinking")
            return False
        self.end_burst()
        if kind == "system":
            detail = event.get("description") or event.get("summary") or event.get("model") or ""
            status = f" ({event.get('status')})" if event.get("status") else ""
            self.note("claude-code", f"{subtype}{status}: {str(detail)[:200]}")
        elif kind == "assistant":
            for part in event.get("message", {}).get("content", []):
                if part.get("type") == "text":
                    self.note("worker", part.get("text", ""))
                    self.loop.said(part.get("text", ""))
                    self.set_phase("writing")
                elif part.get("type") == "tool_use":
                    self.tool_calls += 1
                    self.last_action = time.time()
                    self.current_tool = f"{part.get('name')} {json.dumps(part.get('input'))[:150]}"
                    self.note("worker", f"tool call {part.get('name')} {json.dumps(part.get('input'))[:300]}")
                    self.loop.tool_call(part.get("id"), part.get("name"), part.get("input"))
                    self.set_phase("tool")
        elif kind == "user":
            content = event.get("message", {}).get("content")
            for part in content if isinstance(content, list) else []:
                if part.get("type") == "tool_result":
                    text = str(part.get("content"))
                    blocked = any(word in text.lower() for word in BLOCK_WORDS)
                    self.note("claude-code" if blocked else "tool", text[:1000])
                    self.loop.tool_result(part.get("tool_use_id"), text)
                    self.current_tool = ""
                    self.last_action = time.time()
                    self.set_phase("waiting")
        elif kind == "result":
            self.note("claude-code", f"result subtype={subtype} is_error={event.get('is_error')} "
                                     f"turns={event.get('num_turns')} result={str(event.get('result'))[:300]}")
            self.set_phase("done")
        self.check_loop()
        return True

    def check_words(self, line):
        lowered = line.lower()
        if not self.blocked_alerted and any(word in lowered for word in BLOCK_WORDS):
            self.alert("Worker blocked", f"{self.task_id}: a tool call needed approval, so the worker could not run "
                                         f"it. See {self.readable_path.name}", "high")
            self.blocked_alerted = True
        if not self.model_alerted and any(word.lower() in lowered for word in MODEL_ERROR_WORDS):
            self.alert("Worker model error", f"{self.task_id}: Claude Code reported a model or API error. "
                                             f"See {self.readable_path.name}", "high")
            self.model_alerted = True

    def check_loop(self):
        """When a loop sign shows, stop this session. The main thread then asks the helper, or ends the run."""
        if not self.loop.reason or self.loop_stopped:
            return
        self.loop_stopped = True
        self.loop_reasons.append(self.loop.reason)
        if self.assists < C.max_assists:
            follow = f"Stopping this session and asking {C.helper_model} for advice."
        else:
            follow = f"The helper has been asked {C.max_assists} times already, so the run ends here."
        self.alert("Worker in a loop", f"{self.task_id}: the worker {self.loop.reason}. {follow}", "high")
        self.script_note(f"loop found: the worker {self.loop.reason}. {follow}")
        kill_tree(self.worker_pid)

    # ---- reporting ---------------------------------------------------------------------------------------------------

    def describe_phase(self):
        held = minutes_text(time.time() - self.phase_since)
        if self.phase == "thinking":
            return f"thinking ({self.thinking_burst} tokens in this burst, {held})"
        if self.phase == "tool":
            return f"running a tool for {held}: {self.current_tool}"
        if self.phase == "waiting":
            return f"waiting for the model for {held}"
        if self.phase == "helper":
            return f"asking {C.helper_model} for advice for {held}"
        return f"{self.phase} for {held}"

    def progress_text(self):
        return (f"{self.task_id} attempt {self.attempt}, session {self.session}: "
                f"{minutes_text(time.time() - self.start)} in. Now {self.describe_phase()}. {self.tool_calls} tool "
                f"calls, {self.thinking_total} thinking tokens, helper asked {self.assists} times. {git_diff_stat()}")

    def status(self):
        return {
            "task": self.task_id, "attempt": self.attempt, "session": self.session, "model": C.worker_model,
            "state": self.state, "script_pid": os.getpid(), "worker_pid": self.worker_pid,
            "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.start)),
            "phase": self.phase, "phase_detail": self.describe_phase(),
            "seconds_since_output": int(time.time() - self.last_event),
            "tool_calls": self.tool_calls, "thinking_tokens": self.thinking_total,
            "helper_asked": self.assists, "loops": self.loop_reasons, **self.result,
        }

    def classify_quiet(self, quiet):
        """What a quiet stream means. Returns (kind, title, text, priority)."""
        held = minutes_text(quiet)
        if self.phase == "tool":
            return ("tool", "Worker tool still running",
                    f"{self.task_id}: one tool call has run for {held}: {self.current_tool}", "high")
        gpu = gpu_percent()
        if gpu is not None and gpu >= C.gpu_busy_percent:
            return ("busy", "Worker quiet but working",
                    f"{self.task_id}: no output for {held}, but the GPU is at {gpu}%, so the model is still "
                    f"computing (a long prompt or long thinking). Phase: {self.describe_phase()}", "default")
        load = f"the GPU is at {gpu}%" if gpu is not None else "the GPU load is unknown"
        return ("idle", "Worker may be stuck",
                f"{self.task_id}: no output for {held} and {load}. Stop it with: worker_task.py --stop", "high")

    def watch(self, stop):
        """Background thread: quiet-stream and loop checks, time limit, progress messages, inbox, status file."""
        last_report = last_inbox = self.start
        last_quiet_check = 0.0
        last_quiet_kind = None
        reports = 0
        while not stop.wait(5):
            now = time.time()
            in_session = self.phase not in ("helper", "done")
            quiet = now - self.last_event
            if in_session and quiet >= C.stuck_check_interval:
                if now - last_quiet_check >= C.stuck_check_interval:
                    last_quiet_check = now
                    kind, title, text, priority = self.classify_quiet(quiet)
                    if kind != last_quiet_kind:
                        self.alert(title, text, priority)
                        self.script_note(f"quiet stream: {text}")
                        last_quiet_kind = kind
            else:
                last_quiet_check = 0.0
                last_quiet_kind = None
            if (in_session and self.phase not in ("tool", "starting")
                    and now - self.last_action >= C.loop_idle_minutes * 60):
                self.loop.flag(f"went {C.loop_idle_minutes} min without a tool call")
                self.check_loop()
            if not self.timed_out and now - self.start >= C.max_minutes * 60:
                self.timed_out = True
                self.alert("Worker run timed out", f"{self.task_id}: stopped after {C.max_minutes} min. "
                                                   f"{self.progress_text()}", "high")
                self.script_note(f"time limit of {C.max_minutes} min reached, stopping the worker")
                kill_tree(self.worker_pid)
            wait_for = C.early_progress_interval if reports < C.early_progress_reports else C.progress_interval
            if now - last_report >= wait_for:
                last_report = now
                reports += 1
                self.alert("Worker progress", self.progress_text())
            if now - last_inbox >= C.inbox_poll_interval:
                last_inbox = now
                self.handle_inbox()
            write_status(self.status())

    def handle_inbox(self):
        """Answers STATUS and MAN from the phone inbox. Other commands are left unread for the loop to handle."""
        seen = read_seen()
        for message_id, text in poll_inbox():
            if message_id in seen:
                continue
            command = text.strip().upper()
            if command == "STATUS":
                self.alert("Worker status", self.progress_text())
                mark_seen(message_id)
            elif command == "MAN":
                self.alert("Commands", COMMANDS_HELP)
                mark_seen(message_id)


# ---------------------------------------------------------------------------------------------------------------------
# Commands

def preflight(task_id, feedback):
    """Problems that would make a run fail in a confusing way. Empty when the run can start."""
    problems = []
    previous = read_status()
    if previous.get("state") == "running" and pid_alive(previous.get("script_pid")):
        problems.append(f"another hand-off is running ({previous.get('task')}, pid {previous.get('script_pid')}). "
                        "Wait, or stop it with --stop.")
    claude = Path(C.claude_exe)
    if not (claude.exists() or subprocess.run(["where" if os.name == "nt" else "which", C.claude_exe],
                                              capture_output=True).returncode == 0):
        problems.append(f"Claude Code not found at {C.claude_exe}. Set claude_exe in the config or CLAUDE_EXE.")
    if not http_ok(f"{C.model_server}/api/version") and not http_ok(f"{C.model_server}/v1/models"):
        problems.append(f"the model server does not answer at {C.model_server}. Start it first.")
    elif http_ok(f"{C.model_server}/api/version") and not http_ok(f"{C.model_server}/api/show",
                                                                  {"model": C.worker_model}):
        problems.append(f"the model server has no model {C.worker_model}.")
    if not C.tasks.exists():
        problems.append(f"no task file at {C.tasks}")
    elif not find_spec(task_id):
        problems.append(f"no task {task_id} in {C.tasks.name}")
    if feedback is None:
        leftovers = sorted(p for p in changed_paths() if in_scope(p))
        if leftovers:
            problems.append(f"{len(leftovers)} leftover changes in the scope paths (for example {leftovers[0]}). "
                            "A first attempt starts clean: run --revert, or pass a feedback file for a retry.")
    return problems


def worker_command():
    # The prompt goes in on stdin, not the command line, which Windows limits to 32,767 characters
    return [
        C.claude_exe, "-p",
        "--model", C.worker_model,
        "--permission-mode", "acceptEdits",
        "--tools", ",".join(C.allowed_tools),
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--system-prompt", C.system_prompt,
        "--allowedTools", *C.allowed_tools,
        "--output-format", "stream-json", "--verbose",
    ]


def worker_env():
    """Points this Claude Code session at the model server instead of Anthropic's API."""
    env = dict(os.environ)
    env.update({
        "ANTHROPIC_BASE_URL": C.model_server,
        "ANTHROPIC_AUTH_TOKEN": "local",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": C.worker_model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": C.worker_model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": C.worker_model,
    })
    return env


def run_session(run, prompt, raw):
    """One Claude Code session for the worker. Returns its exit code."""
    extra = {} if os.name == "nt" else {"start_new_session": True}   # its own process group, so --stop ends it all
    proc = subprocess.Popen(worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=worker_env(), cwd=C.project, text=True, encoding="utf-8", errors="replace", bufsize=1,
                            **extra)
    run.worker_pid = proc.pid
    proc.stdin.write(prompt)
    proc.stdin.close()
    write_status(run.status())

    def pump():
        for line in proc.stdout:
            if run.observe(line):
                raw.write(line)
                raw.flush()

    # The stream is read on its own thread, and the end of the session is the process exiting, not the pipe closing.
    # A child process that keeps the pipe open must not hold the script.
    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    code = proc.wait()
    reader.join(timeout=60)
    if reader.is_alive():
        run.script_note("output pipe still open 60 s after the worker exited; carrying on")
    run.end_burst()
    run.script_note(f"session {run.session}: the worker exited with {code} after "
                    f"{minutes_text(time.time() - run.start)}")
    return code


def get_advice(run, spec):
    """Asks the helper about the loop. Returns the next session's prompt addition, or an empty string."""
    run.set_phase("helper")
    run.assists += 1
    tests = test_output()
    diff = git("diff", "--", *C.scope_dirs)
    run.script_note(f"asking {C.helper_model} for advice (help {run.assists} of {C.max_assists})")
    advice, error = ask_helper(spec, tests, diff, run.recent_lines(), run.loop_reasons[-1])
    if error:
        run.script_note(f"the helper gave no advice: {error}")
        run.alert("Helper failed", f"{run.task_id}: {error}", "high")
        return ""
    (C.logs / f"{run.task_id}.helper-{run.assists}.md").write_text(advice + "\n", encoding="utf-8")
    run.note("helper", advice)
    run.alert(f"{C.helper_model} advice for the worker", advice[:450])
    return (f"\n\nYour last session was stopped because you {run.loop_reasons[-1]}.\n"
            f"A reviewer ({C.helper_model}) read the code and the tests. Its advice:\n{advice}\n\n"
            f"Current test output:\n{tests}\n")


def finish(run, before, tests_before, spec, round_no):
    """Rule checks, the closing test run and the review for one round. Sets run.state. A review that rejects with
    rounds left sets review_rejected and keeps its text in run.review_feedback."""
    new_paths = sorted(changed_paths() - before)
    out_of_scope = [p for p in new_paths if not in_scope(p)]
    tests_now = count_tests()
    tests_removed = max(0, tests_before - tests_now) if tests_before is not None and tests_now is not None else 0
    run.result.update({"changed": new_paths, "out_of_scope": out_of_scope, "tests_removed": tests_removed})
    if out_of_scope or tests_removed:
        problems = []
        if out_of_scope:
            problems.append(f"changed files outside the scope paths: {', '.join(out_of_scope)}")
        if tests_removed:
            problems.append(f"{tests_removed} tests fewer than before")
        run.script_note("rule check: " + "; ".join(problems))
        run.alert("Worker broke a rule", f"{run.task_id}: " + "; ".join(problems), "high")

    # The worker's own claim of success is not trusted. The script runs the tests and reports what they say.
    if not C.final_tests:
        run.script_note("closing test run skipped (WORKER_FINAL_TESTS=0)")
        return
    output = test_output()
    run.last_tests = output
    totals = totals_line(output)
    run.result["totals"] = totals
    run.script_note(f"test totals after the run: {totals or 'none printed'}")
    took = minutes_text(time.time() - run.start)
    if not totals:
        run.state = "tests_did_not_run"
        run.alert("Worker finished, tests could not run",
                  f"{run.task_id} finished after {took}, but the test command printed no totals.", "high")
    elif C.pass_marker in totals:
        major, minor, text, error = review_changes(run, spec, round_no)
        if error:
            run.state = "review_failed"
            run.alert("Worker review could not run", f"{run.task_id}: the tests pass, but the review failed: {error}",
                      "high")
            return
        # Round 1 rejects on minor issues too, so the worker can tidy them. Later rounds reject only on major issues.
        rejects = bool(major) or (round_no == 1 and bool(minor))
        run.result.update({"review_round": round_no, "review_major": len(major), "review_minor": len(minor)})
        summary = review_summary(major, minor)
        if not rejects:
            run.state = "passed"
            left = f" Minor items left for you: {len(minor)}." if minor else ""
            run.alert("Worker finished, tests pass and review approved",
                      f"{run.task_id} finished after {took}: {totals}. Review round {round_no}: {summary}.{left}")
        elif round_no < C.max_review_rounds:
            run.state = "review_rejected"
            run.review_feedback = text
            run.alert(f"Worker review round {round_no}: sent back",
                      f"{run.task_id}: the review found issues, so the worker tries again (round {round_no + 1} of "
                      f"{C.max_review_rounds}). {summary}", "high")
        else:
            run.state = "review_failed"
            run.alert("Worker review rejected the change",
                      f"{run.task_id}: still {len(major)} major issues after {round_no} review rounds. {summary}. "
                      f"Details: {C.logs.name}/{run.task_id}-{round_no}.md", "high")
    else:
        run.state = "tests_failed"
        run.alert("Worker finished, tests fail", f"{run.task_id} finished after {took}: {totals}", "high")


def run_task(task_id, feedback):
    problems = preflight(task_id, feedback)
    if problems:
        for problem in problems:
            print(f"[script] not started: {problem}")
        notify(C.logs / f"{task_id}.notify.log", "Worker run not started", f"{task_id}: {problems[0]}", "high")
        return 1

    C.stop_flag.unlink(missing_ok=True)
    previous = read_status()
    attempt = 1
    if feedback is not None:
        attempt = previous.get("attempt", 1) + 1 if previous.get("task") == task_id else 2

    spec = find_spec(task_id)
    head = PROMPT_HEAD.format(tasks_file=C.tasks.name, scope=", ".join(C.scope), project_rules=C.project_rules,
                              test_command=C.test_command_text)
    prompt = head + "\n" + spec
    if feedback is not None and feedback.exists():
        tail = feedback.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
        prompt += "\n\nThe last attempt failed. Its test output:\n" + "\n".join(tail)

    C.logs.mkdir(parents=True, exist_ok=True)
    run = Run(task_id, attempt)
    before = changed_paths()
    tests_before = count_tests()
    stop = threading.Event()
    with open(run.raw_path, "w", encoding="utf-8") as raw, open(run.readable_path, "w", encoding="utf-8") as readable:
        run.readable = readable
        try:
            run.state = "running"
            run.alert("Worker run started", f"{task_id} attempt {attempt}, model {C.worker_model}")
            run.script_note(f"run started: task {task_id}, attempt {attempt}, model {C.worker_model}")
            watcher = threading.Thread(target=run.watch, args=(stop,), daemon=True)
            watcher.start()

            session_prompt = prompt
            while True:
                code = run_session(run, session_prompt, raw)
                if C.stop_flag.exists() or run.timed_out or not run.loop_stopped or run.assists >= C.max_assists:
                    break
                addition = get_advice(run, spec)
                if not addition:
                    break
                session_prompt = head + "\n" + spec + addition
                run.new_session()
                run.script_note(f"session {run.session} starts with the helper's advice")

            stop.set()
            watcher.join(timeout=10)

            if C.stop_flag.exists():
                C.stop_flag.unlink(missing_ok=True)
                run.state = "stopped"
                run.script_note("stopped on request (--stop)")
            elif run.timed_out:
                run.state = "timed_out"
            elif run.loop_stopped:
                run.state = "looped"
                run.alert("Worker run ended in a loop",
                          f"{task_id}: still looping after {run.assists} helper advice. Last loop: the worker "
                          f"{run.loop_reasons[-1]}", "high")
            elif code != 0:
                run.state = "failed"
                run.alert("Worker run failed", f"{task_id} exited with {code} after "
                                               f"{minutes_text(time.time() - run.start)}. "
                                               f"Last Claude Code line: {run.last_claude_line[:200]}", "high")
            exit_code = code or 1
            if run.state == "running":
                # Verify, and when the review sends the change back, let the worker fix it: up to max_review_rounds
                # reviews in all. Each fix is a fresh session that gets the review and the current test output.
                round_no = 1
                while True:
                    finish(run, before, tests_before, spec, round_no)
                    if run.state != "review_rejected":
                        break
                    round_no += 1
                    if C.stop_flag.exists() or run.timed_out:
                        run.state = "stopped" if C.stop_flag.exists() else "timed_out"
                        break
                    run.new_session()
                    run.script_note(f"review round {round_no}: the worker starts again with the review")
                    session_prompt = (head + "\n" + spec +
                                      f"\n\nThe reviewer sent your change back (review round {round_no - 1} of "
                                      f"{C.max_review_rounds}). Fix every MAJOR item. Fix the MINOR items where the fix "
                                      "is clear. Change nothing the review did not mention, and keep the tests passing."
                                      f"\n\nReview:\n{run.review_feedback}\n\nCurrent test output:\n{run.last_tests}")
                    run_session(run, session_prompt, raw)
                    run.state = "running"
                exit_code = 0 if run.state == "passed" else 1
                if run.state == "running":
                    run.state = "finished"
            write_status(run.status())
            return exit_code
        except KeyboardInterrupt:
            run.state = "stopped"
            run.script_note("interrupted; stopping the worker")
            kill_tree(run.worker_pid)
            write_status(run.status())
            return 130
        except Exception as error:
            run.state = "script_error"
            run.alert("Worker hand-off error", f"{task_id}: the hand-off script failed: {error}", "high")
            kill_tree(run.worker_pid)
            write_status(run.status())
            raise
        finally:
            stop.set()


def stop_run():
    status = read_status()
    if status.get("state") != "running":
        print(f"no hand-off is running (last state: {status.get('state', 'none')})")
        return 0
    C.stop_flag.touch()
    if pid_alive(status.get("worker_pid")):
        kill_tree(status.get("worker_pid"))
        print(f"stopped the worker (pid {status.get('worker_pid')}) for {status.get('task')}; the hand-off script will "
              "report the end of the run")
    elif pid_alive(status.get("script_pid")):
        kill_tree(status.get("script_pid"))
        print(f"the worker was gone; stopped the hand-off script (pid {status.get('script_pid')})")
    else:
        print("the recorded processes are gone; marking the run stopped")
    if not pid_alive(status.get("script_pid")):
        status["state"] = "stopped"
        write_status(status)
        C.stop_flag.unlink(missing_ok=True)
    notify(C.logs / f"{status.get('task')}.notify.log", "Worker run stopped", f"{status.get('task')}: stopped on request")
    return 0


def show_status():
    status = read_status()
    if not status:
        print("no hand-off has run yet")
        return 0
    status["script_alive"] = pid_alive(status.get("script_pid"))
    status["worker_alive"] = pid_alive(status.get("worker_pid"))
    print(json.dumps(status, indent=2))
    return 0


def revert():
    """Saves, then undoes, uncommitted changes in the scope paths, and new out-of-scope files from the last run."""
    status = read_status()
    if status.get("state") == "running" and pid_alive(status.get("script_pid")):
        print("a hand-off is running; stop it first with --stop")
        return 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = C.logs / f"revert-{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "changes.patch").write_text(git("diff", "--", *C.scope_dirs), encoding="utf-8")
    untracked = git("ls-files", "--others", "--exclude-standard", "--", *C.scope_dirs).splitlines()
    strays = [p for p in status.get("out_of_scope", []) if (C.project / p).exists() and p not in
              git("ls-files", "--", p).splitlines()]
    for path in untracked + strays:
        source = C.project / path
        if source.is_file():
            target = backup / "files" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    git("checkout", "HEAD", "--", *C.scope_dirs)
    git("clean", "-fd", "--", *C.scope_dirs)
    for path in strays:
        (C.project / path).unlink(missing_ok=True)
    print(f"reverted the scope paths; removed {len(untracked)} new files there and {len(strays)} out-of-scope files. "
          f"Saved everything in {backup}")
    return 0


def main(argv):
    if not argv or len(argv) > 2 or (argv[0].startswith("--") and argv[0] not in ("--status", "--stop", "--revert")):
        print(__doc__)
        return 2
    configure()
    if argv == ["--status"]:
        return show_status()
    if argv == ["--stop"]:
        return stop_run()
    if argv == ["--revert"]:
        return revert()
    feedback = Path(argv[1]) if len(argv) == 2 else None
    return run_task(argv[0], feedback)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
