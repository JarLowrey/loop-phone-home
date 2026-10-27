"""The repository as a task sees it: the gate's checks, run in the task's slot. A slot is a worktree of its own, reset to
the main branch's last commit before the task, so every change in it is the task's and the owner's edits in the main
tree never are. Its snapshot is {"head": the commit the task started from, "dirty": paths changed before it (none in a
fresh slot)}; each check compares against that commit, so a commit made during the task cannot hide what it changed."""
import pathlib
import re
import time

import loop_common as C

DONE_WHEN_ITEM = re.compile(r"\b(test|file|output) `([^`]+)`")
LOOP_PATH = re.compile(r"`(" + re.escape(C.LOOP_DIR) + r"[^`]+)`")
FILES_TOKEN = re.compile(r"`([^`\s]+)`")
# a file name: something, a dot, and a short extension
FILE_NAME = re.compile(r"\.[a-z][a-z0-9]{0,7}$")
TEST_FUNC = C.TEST_FUNC
ANY_FUNC = C.ANY_FUNC
CLASS_NAME = C.CLASS_NAME
# a word just before a Files path that says the task must not change it
NEGATED = re.compile(r"\b(?:no|not|never)\b[^`.;]{0,30}$", re.I)
TASK_ID = C.TASK_ID
ASSERT_CALL = C.ASSERT_CALL
# the loop files only an Opus task may change: it marks, adds and rewrites tasks
OPUS_LOOP_FILES = (C.TASKS_REL, C.PROGRESS_REL)
# the driver sets this to its own log
log = print


def snapshot(root=None):
    """The state a task starts from: HEAD's commit and every path already changed."""
    return {"head": C.head(root), "dirty": set(C.changed_paths(root))}


def head_moved(snap, root=None):
    """True when a commit was made since the snapshot. The task's work may then be in that commit, out of the checks'
    reach, so the driver does not judge or commit it."""
    return bool(snap) and C.head(root) != snap["head"]


def task_changes(snap, root=None):
    """Every path changed since the snapshot, the loop folder included."""
    return sorted(set(C.changed_paths(root)) - (snap or {}).get("dirty", set()))


def in_scope(path, scope):
    return path.startswith(tuple(item.rstrip("/") + "/" for item in scope))


def scope_changes(scope, root=None):
    return sorted(path for path in C.changed_paths(root) if in_scope(path, scope))


def outside_scope(snap, scope, root=None):
    """Paths the task changed outside the scope and the loop folder. A task must not touch these."""
    return [path for path in task_changes(snap, root)
            if not in_scope(path, scope) and not path.startswith(C.LOOP_DIR)]


def paragraph(spec, label):
    """The paragraph of a task that starts with label (such as **Done when.**), to the next blank line."""
    lines = spec.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(label):
            found = []
            for part in lines[index:]:
                if not part.strip():
                    break
                found.append(part)
            return " ".join(found)
    return ""


def done_when(spec):
    """The task's **Done when.** checks, as (kind, value) pairs: test `name` (a test function that must exist), file
    `path` (a file, from the project root, that the task must write), output `text` (text the test run must print).
    Empty when the task has no Done when line. The line may wrap: the paragraph runs to the next blank line."""
    return DONE_WHEN_ITEM.findall(paragraph(spec, "**Done when.**"))


def task_files(spec):
    """The paths a task's Files line names, from the project root: what its slot leases when it starts. A name without
    an extension is a folder and ends in /. files_strip comes off the front of a path, and files_prefix goes in front of
    one whose first folder is not at the project root. A path the line says not to change ("Change no file under
    `src/`") is left out."""
    found = []
    line = paragraph(spec, "**Files.**")
    for match in FILES_TOKEN.finditer(line):
        if NEGATED.search(line[max(0, match.start() - 40):match.start()]):
            continue
        token = match.group(1).strip().rstrip(",.;:")
        for strip in C.CFG.files_strip:
            token = token.removeprefix(strip)
        last = token.rsplit("/", 1)[-1]
        if "/" not in token and not FILE_NAME.search(last):
            continue
        token = with_prefix(token)
        if "." not in last:
            token = token.rstrip("/") + "/"
        if token not in found:
            found.append(token)
    return found


def with_prefix(token):
    """A Files-line path from the project root: files_prefix in front when its first folder is not at the root."""
    first = token.split("/", 1)[0]
    if not C.CFG.files_prefix or token.startswith(C.LOOP_DIR) or (C.PROJECT / first).exists():
        return token
    return C.CFG.files_prefix + token


def named_functions(spec, path, root=None):
    """The existing functions of the source file at path whose bodies a task changes. The scheduler lets two tasks
    share a shared file only while these do not meet; an empty set means the task only adds to the file (signals,
    fields, new functions, or one call inserted in a list of calls).
    When the Files line names functions in brackets after the file (`src/Sim/tactical_sim.gd` (`end_fight`, one call
    in `_run_interiors`)), those are the answer, less the ones it only inserts a call in. Otherwise every backticked
    `Class.name`, `_private` or `name(...)` of the file anywhere in the spec counts."""
    file = (root or C.PROJECT) / path
    if path.endswith("/") or not file.is_file():
        return set()
    text = file.read_text(encoding="utf-8", errors="replace")
    declared = set(ANY_FUNC.findall(text))
    files_line = paragraph(spec, "**Files.**")
    relative = path.removeprefix(C.CFG.files_prefix) if C.CFG.files_prefix else path
    prefixes = "|".join(re.escape(item) for item in [C.CFG.files_prefix, *C.CFG.files_strip] if item)
    bracket = re.search(rf"`(?:{prefixes})?{re.escape(relative)}`\s*\(([^)]*)\)" if prefixes else
                        rf"`{re.escape(relative)}`\s*\(([^)]*)\)", files_line)
    if bracket:
        listed = re.sub(r"\b(?:one|a)\s+call\s+in\s+`\w+`", "", bracket.group(1))
        return {name for name in re.findall(r"`(\w+)`", listed) if name in declared}
    match = CLASS_NAME.search(text)
    owner = (match.group(1) if match else "") or pathlib.PurePosixPath(path).stem
    found = set()
    for token in re.findall(r"`([^`]+)`", spec):
        for name in re.findall(r"\b(\w+)\b", token):
            if name in declared and (name.startswith("_") or (owner and f"{owner}.{name}" in token)
                                     or re.search(rf"(?<![.\w]){name}\(", token)):
                found.add(name)
    return found


def mentioned_tasks(spec):
    """The ids of other tasks a spec names anywhere ("M3-11 builds on this", "M2-10's crew killed ending")."""
    own = re.match(r"^### \[.\] (\S+)", spec)
    return set(TASK_ID.findall(spec)) - ({own.group(1)} if own else set())


def done_files(spec):
    """The files a task's Done when line names. The driver commits them with the scope."""
    return [value for kind, value in done_when(spec) if kind == "file"]


def allowed_loop_files(spec, opus):
    """The loop-folder paths a task may change: the ones its Files and Done when lines name, and for an Opus task also
    the task and progress files. A named path ending in / allows the whole folder."""
    named = set(LOOP_PATH.findall(paragraph(spec, "**Files.**")))
    named.update(path for path in done_files(spec) if path.startswith(C.LOOP_DIR))
    if opus:
        named.update(OPUS_LOOP_FILES)
    return named


def loop_changes(snap, allowed, root=None):
    """The loop-folder paths the task changed that it was not allowed to, such as the driver itself."""
    folders = tuple(path for path in allowed if path.endswith("/"))
    return [path for path in task_changes(snap, root)
            if path.startswith(C.LOOP_DIR) and path not in allowed and not path.startswith(folders)]


def test_files(root=None):
    """Every file under root (the main tree by default) that the test_files globs match."""
    root = root or C.PROJECT
    found = set()
    for pattern in C.CFG.test_files:
        found.update(path for path in root.glob(pattern) if path.is_file())
    return sorted(found)


def removed_tests(snap, root=None):
    """Test functions the snapshot's commit has in a changed test file and that no test file has now. A test that
    moved to another file is not removed."""
    before = set()
    for path in task_changes(snap, root):
        if C.is_test_file(path):
            shown = C.git("show", f"{snap['head']}:{path}", root=root)
            if shown.returncode == 0:
                before.update(TEST_FUNC.findall(shown.stdout))
    if not before:
        return []
    now_names = set()
    for path in test_files(root):
        now_names.update(TEST_FUNC.findall(path.read_text(encoding="utf-8", errors="replace")))
    return sorted(before - now_names)


def assert_counts(text):
    """{test function: how many asserts (assert_regex) it makes} for one test file's text."""
    counts = {}
    tests = set(TEST_FUNC.findall(text))
    starts = [(match.start(), match.group(1)) for match in ANY_FUNC.finditer(text)] + [(len(text), "")]
    for (start, name), (end, _) in zip(starts, starts[1:]):
        if name in tests:
            counts[name] = len(ASSERT_CALL.findall(text[start:end]))
    return counts


def weakened(before_text, now_text, spec, path):
    """The test functions in one file that make fewer asserts than before, where the task names neither the function nor
    the file. A task may rewrite the asserts it names; any other drop is a test loosened to pass."""
    before = assert_counts(before_text)
    now = assert_counts(now_text)
    stem = pathlib.PurePosixPath(path).stem
    if stem in spec:
        return []
    return [f"{stem}.{name} ({count} to {now[name]} asserts)" for name, count in before.items()
            if name in now and now[name] < count and name not in spec]


def weakened_tests(snap, spec, root=None):
    """The test functions the task loosened (see weakened), over every test file it changed."""
    found = []
    for path in task_changes(snap, root):
        if not C.is_test_file(path):
            continue
        shown = C.git("show", f"{snap['head']}:{path}", root=root)
        current = (root or C.PROJECT) / path
        if shown.returncode == 0 and current.exists():
            found += weakened(shown.stdout, current.read_text(encoding="utf-8", errors="replace"), spec, path)
    return found


def check_done_when(spec, output, root=None):
    """Checks a task's Done when line against the work and the test output. Returns the first unmet check, or ''. A task
    without the line is refused: passing tests alone does not show that a task was done."""
    root = root or C.PROJECT
    checks = done_when(spec)
    if not checks:
        return "the task has no **Done when.** line, so the driver cannot tell that it was done"
    for kind, value in checks:
        if kind == "test":
            if not any(value in TEST_FUNC.findall(path.read_text(encoding="utf-8", errors="replace"))
                       for path in test_files(root)):
                return f"Done when: no test function {value} in the test files"
        elif kind == "file":
            if not (root / value).exists() or not C.git("status", "--porcelain", "--", value, root=root).stdout.strip():
                return f"Done when: {value} was not written"
        elif value not in output:
            return f"Done when: the test run did not print {value!r}"
    return ""


def undo_changes(paths, logs, root=None, why=""):
    """Saves, then undoes, uncommitted changes in the main tree or root: tracked files go back to HEAD, new files are
    deleted. The copies go to <logs>/revert-<time>/, with REASON.txt saying where they came from and why."""
    if not paths:
        return
    root = root or C.PROJECT
    backup = logs / f"revert-{time.strftime('%Y%m%d-%H%M%S')}"
    if backup.exists():
        backup = logs / f"revert-{time.strftime('%Y%m%d-%H%M%S')}-{root.name}"
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "REASON.txt").write_text(f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}\nfrom: {root}\nwhy: "
                                       f"{why or 'not given'}\nfiles: {len(paths)}\n", encoding="utf-8",
                                       newline="\n")
    for path in paths:
        source = root / path
        if source.is_file():
            target = backup / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        if C.git("cat-file", "-e", f"HEAD:{path}", root=root).returncode == 0:
            C.git("checkout", "HEAD", "--", path, root=root)
        else:
            source.unlink(missing_ok=True)
    remove_empty_dirs(paths, root)
    shown = backup.relative_to(C.PROJECT) if backup.is_relative_to(C.PROJECT) else backup
    log(f"undid {len(paths)} changed files in {root}; copies in {shown}")


def remove_empty_dirs(paths, root=None):
    """Removes the folders that deleting new files left empty, up to the project root."""
    root = root or C.PROJECT
    for path in paths:
        folder = (root / path).parent
        while folder != root and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent
