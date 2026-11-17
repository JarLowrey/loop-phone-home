"""The driver's foundation: everything the rest of it reads. It imports no other module of the driver, which is what lets
every other module import it alone, as C.

- **The config**: worker.json in the current folder, or the file WORKER_CONFIG names (runner/worker.example.json shows
  every key). CONFIG_DEFAULTS lists the keys the driver reads, with what each is for. Nothing else in the driver names
  a project path, a language or a test tool.
- **Paths, git and the test run**: where the project is, what git says, how the suite is run, the Claude command, and
  the usage log. Keeps no state but CURRENT_TASK, which tags a usage record with the task it belongs to.
- **The phone**: messages to and from the ntfy topic. The topic and token are read from the files the config names,
  never from the repo.
- **The running state**: driver.json, the log, the pid files, the live sessions, and each worker thread's context.
- **The settings**: the driver-settings block of the loop file, read again at every pass so an edit takes effect with
  no restart; the rest are constants. Read them as C.NAME and never copy them, since refresh() changes them in place.
- **The event record**: <logs>/events.jsonl, one line per phase, which report.py reads.
"""

import datetime
import fnmatch
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from types import SimpleNamespace

# -------------------------------------------------------------------------------------------------------------------
# The config: worker.json, over these defaults.
# -------------------------------------------------------------------------------------------------------------------

CONFIG_DEFAULTS = {
    # where the project is, and the loop's files (relative to project_root)
    "project_root": ".",                    # the git repository root, relative to the config file
    "tasks_file": "loop/TASKS.md",          # the task queue
    "progress_file": "loop/PROGRESS.md",    # the log: a table row per task, a Blocked section, Open questions
    "status_file": "loop/STATUS.md",        # rewritten whenever the queue changes: left, blocked, done ('' for none)
    "archive_file": "loop/archive/TASKS-done.md",   # where --tidy moves the specs of landed tasks
    "loop_file": "loop/LOOP.md",            # holds the driver-settings block
    "logs_dir": "loop/logs",
    "inbox_seen_file": "loop/inbox-seen.txt",
    "rules_file": "CLAUDE.md",              # the project rules, given to every task session; a section whose heading
                                            # ends with "(owner)" is left out of a worker's prompt
    "design_file": "",                      # optional: the design document whose sections tasks name ("Design: ...")
    "project_brief": "a software project",  # one line about the project, for the task session's prompt
    "open_task_pattern": r"^### \[ \] (\S+) (.*)$",   # an open task's heading: group 1 its id, group 2 its title
    "task_pattern": r"^### \[(.)\] (\S+) (.*)$",      # any task's heading: its mark, its id and its title
    "task_id_regex": r"\b([A-Z][A-Z0-9]*-[A-Z0-9]+[a-z]?)\b",   # a task id in text (M3-07, T-12, I-02R): After lines
    "review_id_regex": r"-(?:R|A|V\d+)$",   # the ids of review and audit tasks, which get the review packet
    # what a task may change, and how to test it
    "scope": [],                            # required: folders, from project_root, a task may change
    "test_command": [],                     # required: run from the root of the tree under test
    "test_timeout": 1800,                   # seconds the driver's own test run may take before it counts as failed
    "totals_marker": "passed, ",            # text that marks the totals line in the test output
    "pass_marker": " 0 failed",             # text in the totals line when everything passed
    "fail_markers": [],                     # lines that start with one of these fail the run, e.g. "smoke: FAIL"
    "failure_regex": r"FAIL (\w+)",         # names a failing test in the test output (group 1), for the block reason
    "timing_regex": "",                     # optional: a line of the test output with (seconds, limit) groups,
                                            # e.g. "slowest shard ([\\d.]+) s of its (\\d+) s limit"
    "timeout_markers": ["TIMED OUT"],       # words in the test output that mean it ran out of time
    "test_jobs_env": "",                    # optional: an environment variable the test command reads for how many
                                            # processes to use; the driver sets it to the CPUs shared among the slots
    "test_one_hint": "",                    # optional: how a session runs one test file or the changed ones, for its
                                            # prompt, e.g. "python tests/run.py --only test_<file> runs one file."
    "test_call_regex": "",                  # optional: a Bash command that runs the tests; empty derives it from
                                            # test_command (the script it runs, or its words)
    "partial_test_flags": [],               # words that make a test call less than the full suite, e.g. "--only"
    "full_run_timeout_ms": 600000,          # a full test call's Bash timeout must be at least this (0: no check)
    "test_files": [],                       # globs of the test files, e.g. "tests/**/test_*.py"
    "test_function_regex": r"^\s*(?:async\s+)?(?:static\s+)?(?:def|func|function|fn)\s+(test_?\w+)\s*\(",
    "any_function_regex": r"^\s*(?:async\s+)?(?:static\s+)?(?:def|func|function|fn)\s+(\w+)\s*\(",
    "assert_regex": r"\bassert\w*\b|\bexpect\s*\(|\bfail\s*\(",
    # a name declared at the top level of a file (no indent): two in one file mean a merge doubled it
    "declaration_regex": (r"^(?:@\w+(?:\([^)\n]*\))?\s+)*(?:export\s+)?(?:static\s+)?(?:async\s+)?"
                          r"(?:def|func|function|fn|class|signal|const|var|let|enum)\s+(\w+)"),
    "class_name_regex": r"^(?:class_name|class)\s+(\w+)",   # the class a file declares, for Class.name in task specs
    "keyed_files": [".csv"],                # files whose first column is a key that must not repeat
    "source_extensions": [".py", ".gd", ".js", ".ts", ".jsx", ".tsx", ".rs", ".go", ".cs", ".java", ".kt", ".rb",
                          ".lua", ".c", ".cc", ".cpp", ".h", ".hpp", ".swift", ".php"],   # files the name checks read
    "file_extensions": [".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".cfg", ".ini", ".xml", ".html", ".css",
                        ".svg", ".png", ".jpg", ".sql", ".sh", ".ps1", ".bat", ".tres", ".tscn", ".godot"],
                                            # with source_extensions and keyed_files: a bare Files-line name with one
                                            # of these is a file, and `World.step` is not
    "files_prefix": "",                     # put in front of a Files-line path that names no top folder, e.g. "game/"
    "files_strip": [],                      # taken off the front of a Files-line path, e.g. "res://"
    "companion_files": {},                  # a file that must land with one beside it, by suffix: {".gd": ".uid"}
                                            # means x.gd needs x.gd.uid, and x.gd.uid must not outlive x.gd
    "gate_checks": [],                      # more checks the gate runs on a task's work, each
                                            # {"name", "command", "paths", "pass_paths", "timeout", "fail_regex"}
    "blind_outputs": {},                    # Done-when output the gate's test run never prints, by its first words:
                                            # {"bench:": "only tools/bench.py prints it"}; such a task is refused
    # the task sessions
    "claude_exe": "",                       # empty: CLAUDE_EXE, then claude on PATH, then the VS Code extension's copy
    "task_model": "sonnet",                 # the model that does each task; MODEL from the phone changes it
    "task_turns": 60,
    "task_timeout": 1800,
    "project_rules": "",                    # extra lines for the task prompt: language, style, shell notes
    "deny_tools": ["Bash(git restore:*)", "Bash(git checkout:*)", "Bash(git reset:*)", "Bash(git stash)",
                   "Bash(git stash push:*)", "Bash(git stash save:*)", "Bash(git stash pop:*)",
                   "Bash(git stash apply:*)", "Bash(git stash drop:*)", "Bash(git stash clear:*)",
                   "Bash(git clean:*)", "Bash(git rm:*)", "Bash(git mv:*)", "Bash(git switch:*)",
                   "Bash(git add:*)", "Bash(git commit:*)", "Bash(git rebase:*)", "Bash(git merge:*)",
                   "Bash(git cherry-pick:*)", "Bash(git revert:*)", "Bash(git worktree:*)", "Bash(git push:*)",
                   "Bash(git pull:*)", "Bash(git update-ref:*)"],
    # slots: the git worktrees tasks run in
    "slots_dir": "",                        # empty: <LOCALAPPDATA or ~/.cache>/loop-slots/<project folder name>
    "slot_files": {},                       # files written into each slot, {path: text}; {slot} is the slot's name
    "cache_dirs": [],                       # folders a fresh checkout would not have, removed for the clean check
    # the monitor (monitor.py): a clean checkout's tests, then these, every monitor_every landings and from --monitor
    "monitor_command": [],                  # one command; monitor_commands takes several
    "monitor_commands": [],                 # [{"name", "command", "timeout"}]; {head}, {commits} and {previous} in a
                                            # command become the commit, its count and the last monitored commit
    "monitor_schedule_hour": "03:00",       # when the scheduled --monitor runs each day
    # the phone
    "topic_file": "~/.claude/hooks/ntfy-topic.txt",
    "token_file": "~/.claude/hooks/ntfy-token.txt",
    "inbox_topic_suffix": "",
    "ntfy_server": "https://ntfy.sh",
    "sync": False,                          # push the branch after each landing
    "usage_log": "usage.jsonl",
}
REQUIRED = ("scope", "test_command")
EXTENSIONS = pathlib.Path.home() / ".vscode" / "extensions"


def load_config():
    """The config file's settings over CONFIG_DEFAULTS. Exits with a message when the file or a required key is
    missing."""
    path = pathlib.Path(os.environ.get("WORKER_CONFIG", "worker.json")).expanduser().resolve()
    if not path.exists():
        sys.exit(f"no config file at {path}. Copy runner/worker.example.json there, or set WORKER_CONFIG.")
    values = dict(CONFIG_DEFAULTS)
    values.update(json.loads(path.read_text(encoding="utf-8")))
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        sys.exit(f"config {path} is missing: {', '.join(missing)}")
    values["config_path"] = path
    return SimpleNamespace(**values)


CFG = load_config()


def resolve(value, base):
    found = pathlib.Path(value).expanduser()
    return found if found.is_absolute() else (base / found).resolve()


def posix(value):
    """A config path as the driver writes paths: from the project root, with forward slashes."""
    return pathlib.PurePosixPath(str(value).replace("\\", "/")).as_posix() if value else ""


# -------------------------------------------------------------------------------------------------------------------
# Paths, git, the test run, the task file and the Claude binary: what every module below reads.
# -------------------------------------------------------------------------------------------------------------------

PROJECT = resolve(CFG.project_root, CFG.config_path.parent)
LOGS = PROJECT / CFG.logs_dir
LOGS_REL = posix(CFG.logs_dir).rstrip("/") + "/"
TASKS = PROJECT / CFG.tasks_file
TASKS_REL = posix(CFG.tasks_file)
PROGRESS_REL = posix(CFG.progress_file)
STATUS_REL = posix(CFG.status_file)
ARCHIVE_REL = posix(CFG.archive_file)
LOOP_DIR = pathlib.PurePosixPath(TASKS_REL).parent.as_posix() + "/"
LOOP_FILE = PROJECT / CFG.loop_file
LOOP_REL = posix(CFG.loop_file)
RULES_REL = posix(CFG.rules_file)
CONFIG_SCOPE = [item.replace("\\", "/").rstrip("/") for item in CFG.scope]
USAGE_LOG = LOGS / CFG.usage_log
TEST_COMMAND = CFG.test_command
TEST_COMMAND_TEXT = TEST_COMMAND if isinstance(TEST_COMMAND, str) else " ".join(TEST_COMMAND)
TEST_FUNC = re.compile(CFG.test_function_regex, re.M)
ANY_FUNC = re.compile(CFG.any_function_regex, re.M)
ASSERT_CALL = re.compile(CFG.assert_regex)
DECLARATION = re.compile(CFG.declaration_regex, re.M)
CLASS_NAME = re.compile(CFG.class_name_regex, re.M)
TASK_ID = re.compile(CFG.task_id_regex)
REVIEW_ID = re.compile(CFG.review_id_regex)
FAILURE = re.compile(CFG.failure_regex, re.M) if CFG.failure_regex else None
# the driver's own code: its folder, and the git repository that holds it (the project's, or another one)
DRIVER_DIR = pathlib.Path(__file__).resolve().parent
INTERPRETERS = ("python", "python3", "py", "node", "bash", "sh", "ruby", "perl", "pwsh", "powershell", "deno", "bun")


def test_call_pattern():
    """The regex of a Bash command that runs the tests, rather than only naming the script: test_call_regex when the
    config sets one. Otherwise, for a command that runs a script (python tests/run.py), the script's name at the start of
    a command segment, after an optional interpreter; for one that runs no script (npm test, cargo test), its words."""
    if CFG.test_call_regex:
        return CFG.test_call_regex
    try:
        words = shlex.split(TEST_COMMAND_TEXT, posix=os.name != "nt")
    except ValueError:
        words = TEST_COMMAND_TEXT.split()
    if not words:
        return r"(?!)"
    start = r"(?:^|[;&|(]\s*)"
    end = r"(?=[\s;&|)]|$)"
    paths = [word.replace("\\", "/") for word in words]

    def is_script(word):
        return "/" in word or bool(re.search(r"\.[A-Za-z]\w*$", word))
    script = next((word for word in paths[1:] if is_script(word)), "")
    first = pathlib.PurePosixPath(paths[0]).stem.lower()
    if script and first in INTERPRETERS:
        name = re.escape(pathlib.PurePosixPath(script).name)
        # why: python, python3 and the Windows py launcher all run the same script
        family = r"(?:python|py)[\w.]*" if first.startswith("py") else re.escape(first.rstrip("0123456789")) + r"\S*"
        return rf"{start}(?:\S*{family}\s+)?\S*{name}{end}"
    if "/" in paths[0] and is_script(paths[0]):
        name = re.escape(pathlib.PurePosixPath(paths[0]).name)
        runners = "|".join(re.escape(word) for word in INTERPRETERS)
        return rf"{start}(?:\S*(?:{runners})[\w.]*\s+)?\S*{name}{end}"
    return start + r"\s+".join(re.escape(word) for word in words) + end


TEST_CALL = re.compile(test_call_pattern(), re.I)


def version_key(folder):
    """The numbers in an extension folder's version, so 2.1.310 sorts after 2.1.99."""
    match = re.search(r"claude-code-([\d.]+)", folder.name)
    return tuple(int(part) for part in match.group(1).split(".") if part) if match else ()


def find_claude():
    """The Claude Code executable: claude_exe in the config, CLAUDE_EXE, claude on PATH, else the newest VS Code
    extension's copy, so an extension update does not break the driver."""
    if CFG.claude_exe or os.environ.get("CLAUDE_EXE"):
        return CFG.claude_exe or os.environ["CLAUDE_EXE"]
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    found = sorted(EXTENSIONS.glob("anthropic.claude-code-*/resources/native-binary/claude*"),
                   key=lambda path: version_key(path.parent.parent.parent))
    return str(found[-1]) if found else "claude"


CLAUDE = find_claude()
# git commands that change files or the index. A session that runs one can wipe other files, so they are denied.
# Everything else in the shell is allowed: reading, searching, read-only git (git stash list included) and the scripts
DISALLOWED_TOOLS = list(CFG.deny_tools)
CURRENT_TASK = {"id": ""}


def rel(path, root=None):
    """path from the root of its tree, with forward slashes."""
    return pathlib.Path(path).resolve().relative_to((root or PROJECT).resolve()).as_posix()


def git(*args, stdin=None, root=None):
    """Runs git in the main tree, or in root (a slot's worktree)."""
    return subprocess.run(["git", *args], cwd=root or PROJECT, input=stdin, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def changed_paths(root=None):
    """Every modified, added, deleted or untracked path in the main tree (or root), relative to its root."""
    paths = set()
    for line in git("status", "--porcelain=v1", "-uall", root=root).stdout.splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path.strip('"').replace("\\", "/"))
    return paths


def process_line(pid):
    """What the system says of the process with this id ('' when there is none): tasklist's CSV row on Windows, where
    os.kill(pid, 0) would end the process, and ps's command name elsewhere."""
    if not pid:
        return ""
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, errors="replace").stdout
        return out.strip() if f'"{pid}"' in out else ""
    try:
        os.kill(pid, 0)
    except OSError:
        return ""
    found = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True, errors="replace")
    return found.stdout.strip() or "?"


def pid_alive(pid):
    """True while a process with this id runs."""
    return bool(process_line(pid))


def pid_is_python(pid):
    """True when the process with this pid is a Python one, as a driver is. A pid the system gave to another program
    after a driver died is not."""
    return process_line(pid).lstrip().lstrip('"').lower().startswith("python")


def kill_tree(pid):
    """Ends a process and everything it started, so no session or test run is left behind."""
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, text=True)
        return
    try:
        os.killpg(os.getpgid(pid), 15)
    except OSError:
        pass


def head(root=None):
    """The commit HEAD points at, in the main tree or in root."""
    return git("rev-parse", "HEAD", root=root).stdout.strip()


def main_branch():
    """The branch the main tree is on, which the driver lands work on ('HEAD' when it is on no branch)."""
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def driver_repo():
    """(the git repository that holds the driver's code, the driver's folder from its root), or (None, '') when the
    driver is in no repository."""
    found = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=DRIVER_DIR, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    if found.returncode != 0 or not found.stdout.strip():
        return None, ""
    top = pathlib.Path(found.stdout.strip()).resolve()
    return top, DRIVER_DIR.relative_to(top).as_posix()


DRIVER_REPO, DRIVER_REL = driver_repo()


def driver_in_project():
    """True when the driver's code is part of the project the tasks change, so a task can change it."""
    return DRIVER_REPO is not None and DRIVER_REPO == PROJECT.resolve()


def driver_tree():
    """The git tree the driver's folder has in the commit its repository's HEAD points at: it changes whenever a commit
    changes the driver's code. Empty when git cannot say."""
    if DRIVER_REPO is None:
        return ""
    return git("rev-parse", f"HEAD:{DRIVER_REL}", root=DRIVER_REPO).stdout.strip()


def driver_commit():
    """`<short hash> <subject>` of the driver repository's HEAD, which --check reports the running driver started
    from; '' outside a repository."""
    if DRIVER_REPO is None:
        return ""
    return git("log", "-1", "--format=%h %s", root=DRIVER_REPO).stdout.strip()


def driver_changed(paths):
    """True when any path (from the project root) is the driver's own code."""
    return driver_in_project() and any(path.startswith(DRIVER_REL + "/") for path in paths)


def test_output(timeout=None, root=None, args=(), env=None):
    """The test command's printed output, run from the root of the tree under test (a slot's worktree, or the main
    tree). A run longer than timeout (test_timeout by default) is ended with everything it started, and its output then
    has no totals line, so it counts as failed. env adds to the environment."""
    command = TEST_COMMAND if isinstance(TEST_COMMAND, str) else [*TEST_COMMAND, *args]
    if isinstance(command, str) and args:
        command = f"{command} {' '.join(args)}"
    return command_output(command, root or PROJECT, timeout or CFG.test_timeout, env, "test run")


def command_output(command, cwd, timeout, env=None, what="command"):
    """A command's stdout and stderr together, run in cwd; a run longer than timeout seconds is ended with everything it
    started and reads '<what> timed out after <n> s'."""
    return command_result(command, cwd, timeout, env, what)[1]


def command_result(command, cwd, timeout, env=None, what="command"):
    """(exit code, stdout and stderr together) of a command run in cwd. A run longer than timeout seconds is ended with
    everything it started, and reads (-1, '<what> timed out after <n> s')."""
    process = subprocess.Popen(command, shell=isinstance(command, str), cwd=cwd, stdout=subprocess.PIPE,
                               env={**os.environ, **env} if env else None,
                               stderr=subprocess.STDOUT, text=True, errors="replace",
                               start_new_session=os.name != "nt")
    try:
        out, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # why: a test command may start other programs, which a plain kill of the first would leave running
        kill_tree(process.pid)
        process.communicate()
        return -1, f"{what} timed out after {timeout} s"
    return process.returncode, out.strip()


def totals_line(output):
    totals = [line for line in (output or "").splitlines() if CFG.totals_marker in line]
    return totals[-1] if totals else ""


def suite_load(output):
    """How close the test run came to its time limit, from timing_regex: (seconds, limit), or None."""
    match = re.search(CFG.timing_regex, output or "") if CFG.timing_regex else None
    return (float(match.group(1)), int(match.group(2))) if match else None


def timed_out(output):
    """True when the test run (or a part of it) ran out of time: the driver's own limit, or a timeout_marker."""
    output = output or ""
    return "test run timed out" in output or any(marker in output for marker in CFG.timeout_markers or ())


def test_failure(output):
    """Why a test run's output is not a pass, or '': no totals line, a totals line without pass_marker, or a line that
    starts with one of fail_markers."""
    output = output or ""
    totals = totals_line(output)
    if not totals or CFG.pass_marker not in totals:
        return totals or output[:200] or "no totals printed"
    marked = next((line.strip() for line in output.splitlines() if CFG.fail_markers
                   and line.startswith(tuple(CFG.fail_markers))), "")
    return marked[:200]


def failing_tests(output):
    """The names of the failing tests failure_regex finds in a test run's output, each once, in order."""
    if FAILURE is None:
        return []
    # why: a fail_markers line ("smoke: FAIL the window did not open") holds FAIL too, but names no test
    markers = tuple(CFG.fail_markers or ())
    lines = [line for line in (output or "").splitlines() if not (markers and line.lstrip().startswith(markers))]
    return list(dict.fromkeys(match.group(1) for match in FAILURE.finditer("\n".join(lines))))


def matches(path, patterns):
    """True when path (from the project root) matches one of patterns: a glob, or a folder written ending in /."""
    return any(path.startswith(pattern) if pattern.endswith("/") else fnmatch.fnmatch(path, pattern)
               for pattern in patterns)


def is_test_file(path):
    """True for a path, from the project root, that the test_files globs match."""
    return matches(path, CFG.test_files)


def is_source(path):
    """True for a source file (source_extensions) or a keyed file (keyed_files): the files the name checks read."""
    return str(path).endswith(tuple(CFG.source_extensions) + tuple(CFG.keyed_files))


def is_keyed(path):
    return str(path).endswith(tuple(CFG.keyed_files))


def code_changed(paths):
    """True when any path is inside the scope: then the tests must run again."""
    return any(path.startswith(tuple(item.rstrip("/") + "/" for item in SCOPE)) for path in paths)


def tasks_text():
    """The task file as the main branch's last commit has it. The driver reads only committed tasks, so an edit the
    owner is still making is never half-read; it counts once it is committed."""
    shown = git("show", f"HEAD:{TASKS_REL}")
    return shown.stdout if shown.returncode == 0 else TASKS.read_text(encoding="utf-8")


def find_spec(task_id, text=None):
    """The task's section of the task file: from its heading to the next heading or a --- line."""
    section = []
    inside = False
    heading = re.compile(r"^### \[.\] " + re.escape(task_id) + r"(?:\s|$)")
    for line in (text if text is not None else tasks_text()).splitlines():
        if heading.match(line):
            inside = True
        elif inside and (line.startswith("### ") or line == "---"):
            break
        if inside:
            section.append(line)
    return "\n".join(section)


def task_context(spec):
    """The task file's 'How to read a task' section, the current task's section, and the parts of the design file the
    task names, so the session need not read either whole file."""
    match = re.search(r"^## How to read a task\n.*?(?=^## )", tasks_text(), re.M | re.S)
    rules = match.group(0).strip() if match else ""
    design = design_sections(spec)
    if design:
        design = f"\n\nThe parts of {CFG.design_file} this task names:\n\n{design}"
    return f"{rules}\n\n{spec}{design}"


# the most of the design file a prompt carries; a task that names more reads the rest itself
DESIGN_LIMIT = 12000


def design_sections(spec, design=None):
    """The sections of the design file a task names in its "Design:" references ("Combat", "Saving"): each heading
    that is or contains the name, to the next heading at its level or above. A name that is no heading is looked for as
    a bold label, from it to the next heading. Each section once, at most DESIGN_LIMIT characters."""
    if design is None:
        path = PROJECT / CFG.design_file if CFG.design_file else None
        design = path.read_text(encoding="utf-8") if path and path.is_file() else ""
    names = []
    for match in re.finditer(r"Design:\s*((?:\"[^\"]+\"[^\"\n]*(?:\n(?!\n))?)+)", spec):
        names += [" ".join(name.split()) for name in re.findall(r'"([^"]+)"', match.group(1))]
    heads = [(m.start(), len(m.group(1)), " ".join(m.group(2).split())) for m in re.finditer(r"^(#+) (.*)$", design,
                                                                                               re.M)]
    found, taken = [], set()
    for name in dict.fromkeys(names):
        lowered = name.lower()
        head = next((h for h in heads if h[2].lower() == lowered), None) or next(
            (h for h in heads if lowered in h[2].lower()), None)
        if head:
            start = head[0]
            end = next((h[0] for h in heads if h[0] > start and h[1] <= head[1]), len(design))
        else:
            label = re.search(rf"\*\*{re.escape(name)}[^*]*\*\*", design, re.I)
            if not label:
                continue
            start = design.rfind("\n", 0, label.start()) + 1
            end = next((h[0] for h in heads if h[0] > start), len(design))
        if start not in taken:
            taken.add(start)
            found.append(design[start:end].strip())
    text = "\n\n".join(found)
    return text if len(text) <= DESIGN_LIMIT else text[:DESIGN_LIMIT].rsplit("\n", 1)[0] + "\n[cut: read the rest]"


def rules_text(root=None):
    """The project rules file (rules_file) as the tree at root has it, or '' when the project has none."""
    path = (root or PROJECT) / CFG.rules_file
    return path.read_text(encoding="utf-8") if CFG.rules_file and path.is_file() else ""


def helper_env():
    """The normal Claude login, without any settings that point Claude Code at another server. SESSION_ENV marks the
    process as one the driver started, so a driver is never started from inside it."""
    env = dict(os.environ)
    env[SESSION_ENV] = "1"
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
        env.pop(key, None)
    if not env.get("ANTHROPIC_API_KEY"):
        env.pop("ANTHROPIC_API_KEY", None)
    return env


def log_usage(record):
    """Appends one line to the usage log: the time, the task and the use of one session."""
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "task": CURRENT_TASK["id"], **record}
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(USAGE_LOG, "a", encoding="utf-8") as usage_log:
        usage_log.write(json.dumps(record) + "\n")


def spend_on(day=None):
    """The dollars the usage log records for one day (today by default), summed over every session."""
    day = day or datetime.date.today().isoformat()
    total = 0.0
    if not USAGE_LOG.exists():
        return total
    with open(USAGE_LOG, encoding="utf-8", errors="replace") as usage_log:
        for line in usage_log:
            if not line.startswith('{"time": "' + day):
                continue
            try:
                total += float(json.loads(line).get("cost_usd") or 0.0)
            except (ValueError, TypeError, AttributeError):
                continue
    return total

# -------------------------------------------------------------------------------------------------------------------
# The phone: messages to and from the ntfy topic, and the files sent to it.
# -------------------------------------------------------------------------------------------------------------------


TOPIC_FILE = resolve(CFG.topic_file, CFG.config_path.parent)
TOKEN_FILE = resolve(CFG.token_file, CFG.config_path.parent)
SEEN = PROJECT / CFG.inbox_seen_file
SERVER = CFG.ntfy_server.rstrip("/")


def read_word(path):
    return "".join(path.read_text(encoding="utf-8").split()) if path.exists() else ""


def topic():
    return read_word(TOPIC_FILE)


def ntfy_headers(headers=None):
    """The headers of a call to ntfy, with the access token when token_file holds one."""
    headers = dict(headers or {})
    token = read_word(TOKEN_FILE)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def post(title, body, priority="default", actions=""):
    """One message to the phone. The driver's messages carry a title, which is how it skips its own replies."""
    name = topic()
    log(f"phone [{priority}] {title}: {body[:120]}")
    if not name:
        return
    headers = {"Title": title, "Priority": priority}
    if actions:
        headers["Actions"] = actions
    request = urllib.request.Request(f"{SERVER}/{name}", data=body[:500].encode("utf-8"),
                                     headers=ntfy_headers(headers), method="POST")
    try:
        urllib.request.urlopen(request, timeout=20).read()
    except Exception as error:
        log(f"phone send failed: {error}")


def button(label, body):
    """An ntfy action button that posts body to the commands topic, which is how the phone answers the driver."""
    return f"http, {label}, {SERVER}/{topic()}{CFG.inbox_topic_suffix}, method=POST, body={body}, clear=true"


def send_file(title, filename, data):
    """One phone message whose body is the file, so the phone shows it as an attachment. True when it was sent."""
    name = topic()
    log(f"phone [file] {title}: {filename}, {len(data)} bytes")
    if not name:
        return True
    request = urllib.request.Request(f"{SERVER}/{name}", data=data, method="PUT",
                                     headers=ntfy_headers({"Title": title, "Filename": filename}))
    try:
        urllib.request.urlopen(request, timeout=120).read()
        return True
    except Exception as error:
        log(f"phone file send failed: {error}")
        return False


def mark_seen(message_id):
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN, "a", encoding="utf-8") as seen:
        seen.write(message_id + "\n")


def poll_messages(window):
    """(id, text) for each phone message in the last window (such as 1h) that has not been seen. Messages with a title
    are the driver's own, so they are marked seen and skipped."""
    name = topic()
    if not name:
        return []
    request = urllib.request.Request(f"{SERVER}/{name}{CFG.inbox_topic_suffix}/json?poll=1&since={window}",
                                     headers=ntfy_headers())
    try:
        body = urllib.request.urlopen(request, timeout=20).read()
    except Exception:
        return []
    seen = set(SEEN.read_text(encoding="utf-8").split()) if SEEN.exists() else set()
    messages = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("event") != "message" or event.get("id") in seen:
            continue
        if event.get("title"):
            mark_seen(event.get("id", ""))
            continue
        messages.append((event.get("id", ""), str(event.get("message", ""))))
    return messages

# -------------------------------------------------------------------------------------------------------------------
# The driver's running state: driver.json, the log, the pid files, the running sessions, each worker thread's context.
# -------------------------------------------------------------------------------------------------------------------


DRIVER_STATE = LOGS / "driver.json"
DRIVER_LOG = LOGS / "driver.log"
DRIVER_PID = LOGS / "driver.pid"
SUPERVISOR_PID = LOGS / "driver-supervisor.pid"
# why: the PC's --model, --approve and --deny write here, so a running driver, which owns driver.json, picks them up
REQUEST_FILE = LOGS / "driver-request.txt"
# the Claude sessions running now, by who runs them (a slot's name, or "opus-write"), so a shutdown can kill them all
CHILDREN = {}
# state: the driver.json dict; stopping: a shutdown is under way; scheduler: the running Scheduler; receipt_keys: each
# slot's name to the key its task hook signs the session's test receipt with
RUNTIME = {"state": None}
# each thread's own context: slot and task (the slot's name and the task it runs), run (the task's run number), prefix
# (its log lines' tag), outcome and reason (how the task ended, for events.jsonl), last_session, budget_cut
LOCAL = threading.local()
STATE_LOCK = threading.RLock()
LOG_LOCK = threading.Lock()
SUPERVISED = os.environ.get("DRIVER_SUPERVISED") == "1"
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# set in the environment of every session the driver starts
SESSION_ENV = "LOOP_DRIVER_SESSION"


def utf8_console():
    """Makes stdout and stderr take any character. Every module that logs imports this one, so one call here covers the
    driver, the supervisor and check_driver.py."""
    # why: a cp1252 console raised UnicodeEncodeError on a model's arrow or dash and killed the driver mid-task,
    # blocking the task and losing its work; driver.log was already utf-8
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


utf8_console()


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
    try:
        print(line, flush=True)
    except (OSError, UnicodeError):
        # why: a log line must never be what ends a task, so a console that still refuses it is skipped, not raised
        pass
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


def start_problem():
    """Why a driver must not start here ('' when it may): a session the driver made (its environment names it) and a
    git worktree (a slot, where .git is a file) are not the owner's main tree. A driver started in either took a slot
    for the main tree and landed and committed in the real slots."""
    if os.environ.get(SESSION_ENV):
        return "this process belongs to a session the driver started; a driver started here would run beside the real one"
    if not (PROJECT / ".git").is_dir():
        return f"{PROJECT} is a git worktree, not the main tree; run the driver from the main tree"
    return ""


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
    emit("pause", title=title, body=body[:500])
    post(title, body, priority)

# -------------------------------------------------------------------------------------------------------------------
# The settings and limits, read from the loop file's driver-settings block at every pass.
# -------------------------------------------------------------------------------------------------------------------


LOOP_MD = LOOP_FILE
# the fallbacks for the driver-settings block of the loop file
SETTING_DEFAULTS = {
    "scope": ", ".join(CONFIG_SCOPE),
    "poll_seconds": "60",
    "inbox_window": "1h",
    "worker_turns": str(CFG.task_turns),
    "worker_timeout": str(CFG.task_timeout),
    "escalate_model": "opus",
    "daily_budget_usd": "250",
    "approval_hours": "12",
    "approvals": "on",
    "monitor_every": "5",
    "parallel_slots": "1",
    "parallel_window": "4",
    "shared_files": "",
    "max_requeues": "2",
    "commit_digest": "4",
}
SETTING_ALIASES = {"haiku_turns": "worker_turns", "haiku_timeout": "worker_timeout"}
SCOPE = list(CONFIG_SCOPE)
POLL_SECONDS = 60
INBOX_WINDOW = "1h"
WORKER_TURNS = 60
WORKER_TIMEOUT = 1800
ESCALATE_MODEL = "opus"
DAILY_BUDGET_USD = 250.0
APPROVAL_HOURS = 12.0
# why: "approvals: off" in the loop file lets every automated call run without asking the phone; budgets still apply
APPROVALS_ON = True
# the clean-checkout check and the monitor commands run after this many landed tasks (monitor.py); 0 turns them off
MONITOR_EVERY = 5
# slots: how many tasks run side by side, each in its own worktree (1 runs them one at a time, still in a worktree)
PARALLEL_SLOTS = 1
# how far the scheduler looks ahead: a task starts only within this many open tasks of the oldest running one
PARALLEL_WINDOW = 4
# files two running tasks may both change; the rebase merges their added lines (slots.additive_merge)
SHARED_FILES = []
# a task that could not land this many times (a conflict, a duplicate, a failed re-test) next runs alone
MAX_REQUEUES = 2
# landed tasks per "Tasks landed" message on the phone (1 sends one per commit)
COMMIT_DIGEST = 4
LAST_SETTINGS = {}

# the models that may do the tasks, by the name the MODEL command takes, with the name their commits carry
WORKER_MODELS = {"haiku": "Claude Haiku 5.5", "sonnet": "Claude Sonnet 5.5", "opus": "Claude Opus 5.5",
                 "fable": "Claude Fable 5.1"}
MODEL_RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 2}
# the most one task session may spend, by model. A session that reaches it ends, and the task counts as failed
WORKER_BUDGET_USD = {"haiku": 3.0, "sonnet": 8.0, "opus": 60.0, "fable": 40.0}
WORKER_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_WORKER = CFG.task_model

OPUS_MODEL = os.environ.get("OPUS_MODEL", "opus")
OPUS_EFFORT = "high"
# a phone question: read-only tools, time and spend
OPUS_TOOLS = "Read,Grep,Glob"
OPUS_TIMEOUT = 1800
OPUS_BUDGET_USD = 10.0
OPUS_MAX_BYTES = 3_900_000   # the phone must take the file, and the limit is 4 MB, so stay well under it
# OPUS WRITE from the phone, and the triage
OPUS_WRITE_TURNS = 80
OPUS_WRITE_TIMEOUT = 3600
OPUS_WRITE_BUDGET_USD = 20.0
# a task whose Who line says Opus
OPUS_TASK_TURNS = 200
OPUS_TASK_TIMEOUT = 7200
OPUS_TASK_BUDGET_USD = 60.0
# a task whose Who line says Fable: an audit, which reads the code and writes one report
FABLE_MODEL = os.environ.get("FABLE_MODEL", "fable")
FABLE_EFFORT = "high"
FABLE_TASK_BUDGET_USD = 50.0
# the models a Who line may name: (model, effort without an Effort line, the most one session may spend)
SENIOR_MODELS = {"opus": (OPUS_MODEL, OPUS_EFFORT, OPUS_TASK_BUDGET_USD),
                 "fable": (FABLE_MODEL, FABLE_EFFORT, FABLE_TASK_BUDGET_USD)}

USAGE_RESUME_MARGIN = 240  # why: the driver restarts 4 minutes after usage reopens, so the first call is not refused again
APPROVAL_POLL_SECONDS = 15
# a session that fails within this many seconds having taken no turn never started: an account, network or install
# fault, not the task's. The driver pauses and tries again after NOT_STARTED_RETRY_SECONDS instead of blocking the task
NOT_STARTED_SECONDS = 30
NOT_STARTED_RETRY_SECONDS = 1800
MAX_TASK_ERRORS = 3
# the share of a day's finished tasks that were requeued, past which the phone is told (once a day, from REQUEUE_ALERT_MIN
# tasks on), since requeues cost money and land nothing: fewer slots or a smaller window is then the fix
REQUEUE_ALERT_SHARE = 0.2
REQUEUE_ALERT_MIN = 5
# blocks in a row that start an Opus triage, and the blocks in a row after a triage that pause the driver
TRIAGE_AFTER_BLOCKS = 2
PAUSE_AFTER_TRIAGE_BLOCKS = 2
# the share of its time limit the test run may take (from timing_regex) before the phone is told, once a day
SUITE_WARN_SHARE = 0.6
# earlier attempts at a task whose last words the next attempt's prompt carries
MAX_ATTEMPTS_KEPT = 3


def read_settings(text):
    """The driver-settings block of the loop file as a dict over SETTING_DEFAULTS, with old names mapped to new ones.
    Unknown keys are logged and ignored."""
    values = dict(SETTING_DEFAULTS)
    match = re.search(r"```driver-settings\n(.*?)```", text, re.S)
    if not match:
        return values
    for line in match.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            key, value = (part.strip() for part in line.split(":", 1))
            key = SETTING_ALIASES.get(key, key)
            if key in values:
                values[key] = value
            else:
                log(f"{LOOP_MD.name}: unknown driver setting {key!r} is ignored")
    return values


def apply_settings(values):
    """Sets the driver's values from a settings dict. Raises ValueError when one cannot be read."""
    global SCOPE, POLL_SECONDS, INBOX_WINDOW, WORKER_TURNS, WORKER_TIMEOUT, ESCALATE_MODEL, DAILY_BUDGET_USD
    global APPROVAL_HOURS, APPROVALS_ON, MONITOR_EVERY, PARALLEL_SLOTS, PARALLEL_WINDOW, SHARED_FILES, MAX_REQUEUES
    global COMMIT_DIGEST
    escalate = values["escalate_model"].lower()
    if values["approvals"].lower() not in ("on", "off"):
        raise ValueError(f"approvals {values['approvals']!r} is not on or off")
    if escalate not in WORKER_MODELS and escalate not in ("", "none"):
        raise ValueError(f"escalate_model {escalate!r} is not one of {', '.join(WORKER_MODELS)} or none")
    scope = [item.strip().replace("\\", "/").rstrip("/") for item in values["scope"].split(",") if item.strip()]
    numbers = (int(values["poll_seconds"]), int(values["worker_turns"]), int(values["worker_timeout"]),
               float(values["daily_budget_usd"]), float(values["approval_hours"]), int(values["monitor_every"]))
    parallel = (int(values["parallel_slots"]), int(values["parallel_window"]), int(values["max_requeues"]))
    if parallel[0] < 1 or parallel[1] < 1 or parallel[2] < 0:
        raise ValueError(f"parallel_slots and parallel_window must be 1 or more, max_requeues 0 or more: {parallel}")
    SCOPE = scope
    PARALLEL_SLOTS, PARALLEL_WINDOW, MAX_REQUEUES = parallel
    COMMIT_DIGEST = max(1, int(values["commit_digest"]))
    SHARED_FILES = [item.strip() for item in values["shared_files"].split(",") if item.strip()]
    POLL_SECONDS, WORKER_TURNS, WORKER_TIMEOUT, DAILY_BUDGET_USD, APPROVAL_HOURS, MONITOR_EVERY = numbers
    INBOX_WINDOW = values["inbox_window"]
    ESCALATE_MODEL = "" if escalate == "none" else escalate
    APPROVALS_ON = values["approvals"].lower() == "on"


def refresh():
    """Reads the driver-settings block in the loop file and sets the driver's values from it. Called at each pass of
    the loop, so an edit there takes effect without a restart. A value it cannot read puts every setting back to its
    default, and each change is logged."""
    try:
        values = read_settings(LOOP_MD.read_text(encoding="utf-8"))
    except OSError as error:
        log(f"could not read {LOOP_MD.name}: {error}; using the defaults")
        values = dict(SETTING_DEFAULTS)
    try:
        apply_settings(values)
    except ValueError as error:
        log(f"{LOOP_MD.name}: a driver setting is not valid ({error}); using the defaults")
        values = dict(SETTING_DEFAULTS)
        apply_settings(values)
    if values != LAST_SETTINGS:
        if LAST_SETTINGS:
            changed = {k: v for k, v in values.items() if LAST_SETTINGS.get(k) != v}
            log(f"driver settings changed from {LOOP_MD.name}: {changed}")
            emit("settings", changed=changed)
        else:
            log(f"driver settings from {LOOP_MD.name}: {values}")
        LAST_SETTINGS.clear()
        LAST_SETTINGS.update(values)

# -------------------------------------------------------------------------------------------------------------------
# The structured record: <logs>/events.jsonl, one JSON object per line, for audits that look for what makes the loop
# slow, stuck or wasteful. driver.log says what happened in words; this file says it as data, with a duration on every
# phase, so report.py (or a model) can add up where the hours and dollars went. Every event has: time (local, to the
# second), t (epoch seconds, for arithmetic), event (its kind), and, when a worker thread emits it, task, slot and run
# (the task's run number: 1 for its first start, 2 after a requeue or a restart). The kinds and their fields:
#   driver_start   pid, branch, commit (of the main tree), settings, worker
#   driver_stop    reason
#   settings       changed (the driver settings that changed in the loop file)
#   pause          title, body          resume   reason
#   schedule_wait  free_slots, next, reason (why no task starts in a free slot), running
#   task_start     title, base, files, solo
#   session        model, effort, kind, seconds, turns, cost_usd, is_error, subtype, terminal_reason, tokens_in,
#                  tokens_out, cache_read, cache_write, tools ({name: calls}), tool_errors, denials ([reason]),
#                  bash_timeouts, test_runs, log (the stream's file)
#   escalate       from, to, reason, queued (true when it waits for the other slots to be idle)
#   catch_up       seconds, landed (commits that landed while the task ran), result
#   gate_wait      seconds (how long the gate waited for another slot's gate or landing)
#   gate           seconds, test_seconds, totals, reason ('' when passed), log (the test output's file),
#                  suite_seconds, suite_limit (from the config's timing_regex, when it matches), receipt (true when
#                  the session's own full run was trusted)
#   baseline       head, totals, timed_out, suite_seconds (HEAD's own suite, after a gate timed out)
#   land           seconds, lock_wait, rebased, retest_seconds, owner_wait, result, commit
#   push           seconds, ok, detail
#   task_end       outcome (landed, blocked, requeued, escalating, paused, stopped, crashed), reason, seconds
#   requeue        reason, wait_for, count          block   reason, outcome
#   approval       what, seconds, answer           opus_write   seconds, result, instruction
#   monitor_run    seconds, head, clean (the clean checkout's verdict), tasks, commands ({name: exit}), worse
# Adding a field is safe: readers take what they know. Renaming or dropping one breaks report.py and old audits.
# -------------------------------------------------------------------------------------------------------------------

LOCK = threading.Lock()


def log_path():
    """<logs>/events.jsonl, found when written, so a test that moves the logs moves this file too."""
    return LOGS / "events.jsonl"


def emit(event, **fields):
    """Appends one event. The thread's task, slot and run are added when it has them. Never raises: a record that
    cannot be written must not stop the loop, so the failure goes to driver.log instead."""
    context = ctx()
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
        log(f"could not write an event ({event}): {error}")


class Timer:
    """Seconds since it was made, rounded for the record."""

    def __init__(self):
        self.start = time.time()

    def seconds(self):
        return round(time.time() - self.start, 1)


def stamp():
    """A file-name-safe time, so each attempt's logs get their own file instead of overwriting the last one's."""
    return time.strftime("%Y%m%d-%H%M%S")
