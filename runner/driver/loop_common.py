"""The project the driver works on, read from the config file, and the helpers every module shares: paths, git, the test
run, the task file, the Claude command and the usage log.

The config is worker.json in the current folder, or the file WORKER_CONFIG names: the same file the worker runner reads
(runner/worker.example.json shows every key). CONFIG_DEFAULTS below lists the keys the driver reads, with what each is
for. Nothing else in the driver names a project path, a language or a test tool."""
import datetime
import fnmatch
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

CONFIG_DEFAULTS = {
    # where the project is, and the loop's files (relative to project_root)
    "project_root": ".",                    # the git repository root, relative to the config file
    "tasks_file": "loop/TASKS.md",          # the task queue
    "progress_file": "loop/PROGRESS.md",    # the log: a table row per task, and a Blocked section
    "loop_file": "loop/LOOP.md",            # holds the driver-settings block
    "logs_dir": "loop/logs",
    "inbox_seen_file": "loop/inbox-seen.txt",
    "rules_file": "CLAUDE.md",              # the project rules, given to every task session
    "design_file": "",                      # optional: the design document whose sections tasks name ("Design: ...")
    "project_brief": "a software project",  # one line about the project, for the task session's prompt
    "open_task_pattern": r"^### \[ \] (\S+) (.*)$",   # an open task's heading: group 1 its id, group 2 its title
    "task_id_regex": r"\b([A-Z][A-Z0-9]*-[A-Z0-9]+[a-z]?)\b",   # a task id in text (M3-07, T-12, I-02R): After lines
    "review_id_regex": r"-(?:R|V\d+)$",   # the ids of review tasks, which get the review packet
    # what a task may change, and how to test it
    "scope": [],                            # required: folders, from project_root, a task may change
    "test_command": [],                     # required: run from the root of the tree under test
    "test_timeout": 1800,                   # seconds the driver's own test run may take before it counts as failed
    "totals_marker": "passed, ",            # text that marks the totals line in the test output
    "pass_marker": " 0 failed",             # text in the totals line when everything passed
    "fail_markers": [],                     # lines that start with one of these fail the run, e.g. "smoke: FAIL"
    "test_one_hint": "",                    # optional: how a session runs one test file, for its prompt
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
    "files_prefix": "",                     # put in front of a Files-line path that names no top folder, e.g. "game/"
    "files_strip": [],                      # taken off the front of a Files-line path, e.g. "res://"
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
    "monitor_command": [],                  # optional: run every monitor_every tasks after the clean check
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


PROJECT = resolve(CFG.project_root, CFG.config_path.parent)
LOGS = PROJECT / CFG.logs_dir
TASKS = PROJECT / CFG.tasks_file
TASKS_REL = pathlib.PurePosixPath(CFG.tasks_file).as_posix()
PROGRESS_REL = pathlib.PurePosixPath(CFG.progress_file).as_posix()
LOOP_DIR = pathlib.PurePosixPath(TASKS_REL).parent.as_posix() + "/"
LOOP_FILE = PROJECT / CFG.loop_file
SCOPE = [item.replace("\\", "/").rstrip("/") for item in CFG.scope]
USAGE_LOG = LOGS / CFG.usage_log
TEST_COMMAND = CFG.test_command
TEST_COMMAND_TEXT = TEST_COMMAND if isinstance(TEST_COMMAND, str) else " ".join(TEST_COMMAND)
# a word in a Bash command that means a session ran the tests: the test command's last path or word
TEST_MARKER = re.split(r"[\\/\s]", TEST_COMMAND_TEXT.strip())[-1] if TEST_COMMAND_TEXT.strip() else "test"
TEST_FUNC = re.compile(CFG.test_function_regex, re.M)
ANY_FUNC = re.compile(CFG.any_function_regex, re.M)
ASSERT_CALL = re.compile(CFG.assert_regex)
DECLARATION = re.compile(CFG.declaration_regex, re.M)
CLASS_NAME = re.compile(CFG.class_name_regex, re.M)
TASK_ID = re.compile(CFG.task_id_regex)
REVIEW_ID = re.compile(CFG.review_id_regex)


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


def pid_alive(pid):
    """True while a process with this id runs. On Windows os.kill(pid, 0) would end it, so tasklist is asked."""
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, errors="replace").stdout
    return f'"{pid}"' in out


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


def test_output(timeout=None, root=None, args=()):
    """The test command's printed output, run from the root of the tree under test (a slot's worktree, or the main
    tree). A run longer than timeout (test_timeout by default) is ended with everything it started, and its output then
    has no totals line, so it counts as failed."""
    command = TEST_COMMAND if isinstance(TEST_COMMAND, str) else [*TEST_COMMAND, *args]
    if isinstance(command, str) and args:
        command = f"{command} {' '.join(args)}"
    limit = timeout or CFG.test_timeout
    process = subprocess.Popen(command, shell=isinstance(command, str), cwd=root or PROJECT, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, errors="replace",
                               start_new_session=os.name != "nt")
    try:
        out, _ = process.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        # why: a test command may start other programs, which a plain kill of the first would leave running
        kill_tree(process.pid)
        process.communicate()
        return f"test run timed out after {limit} s"
    return out.strip()


def totals_line(output):
    totals = [line for line in output.splitlines() if CFG.totals_marker in line]
    return totals[-1] if totals else ""


def test_failure(output):
    """Why a test run's output is not a pass, or '': no totals line, a totals line without pass_marker, or a line that
    starts with one of fail_markers."""
    totals = totals_line(output)
    if not totals or CFG.pass_marker not in totals:
        return totals or output[:200] or "no totals printed"
    marked = next((line.strip() for line in output.splitlines() if line.startswith(tuple(CFG.fail_markers or ()))
                   and CFG.fail_markers), "")
    return marked[:200]


def is_test_file(path):
    """True for a path, from the project root, that the test_files globs match."""
    return any(fnmatch.fnmatch(path, pattern) for pattern in CFG.test_files)


def is_source(path):
    """True for a source file (source_extensions) or a keyed file (keyed_files): the files the name checks read."""
    return str(path).endswith(tuple(CFG.source_extensions) + tuple(CFG.keyed_files))


def code_changed(paths):
    """True when any path is inside the scope: then the tests must run again."""
    return any(path.startswith(tuple(item + "/" for item in SCOPE)) for path in paths)


def tasks_text():
    """The task file as the main branch's last commit has it. The driver reads only committed tasks, so an edit the
    owner is still making is never half-read; it counts once it is committed."""
    shown = git("show", f"HEAD:{TASKS_REL}")
    return shown.stdout if shown.returncode == 0 else TASKS.read_text(encoding="utf-8")


def find_spec(task_id, text=None):
    """The task's section of the task file: from its heading to the next heading or a --- line."""
    section = []
    inside = False
    heading = re.compile(r"^### \[.\] " + re.escape(task_id) + r"\b")
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
    """The sections of the design file a task names in its "Design:" references ("Interior combat", "Ship systems"):
    each heading that is or contains the name, to the next heading at its level or above. A name that is no heading is
    looked for as a bold label, from it to the next heading. Each section once, at most DESIGN_LIMIT characters."""
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


def helper_env():
    """The normal Claude login, without any settings that point Claude Code at another server."""
    env = dict(os.environ)
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
