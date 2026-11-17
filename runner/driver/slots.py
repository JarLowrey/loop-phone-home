"""Slots: the git worktrees the tasks run in, apart from the owner's main tree. Before a task, its slot is reset to the
main branch's last commit, so the owner's uncommitted edits are never the task's, and nothing the task does touches the
owner's tree. A task's work reaches the main branch only through integrate(): one slot at a time, rebased onto the
newest commit, checked and tested again when other work landed meanwhile, then fast-forwarded. Git never overwrites an
uncommitted change of the owner's: when one is in the way, the driver waits and says which file.

The second half of this file is the gate: what the repository looks like to a task, and every check its work must pass
before it lands. It reads a task's spec for the files, tests and **Done when.** line it promised, and compares the
slot against the commit the task started from."""
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time

import leases as L
import core as C


def default_slots_dir():
    """<LOCALAPPDATA, or ~/.cache>/loop-slots/<project folder name>: outside the project, and outside a synced folder."""
    base = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / ".cache")
    return pathlib.Path(base) / "loop-slots" / C.PROJECT.name


SLOTS_DIR = pathlib.Path(os.environ.get("LOOP_SLOTS_DIR") or C.CFG.slots_dir or default_slots_dir()).expanduser()
LEASES = C.LOGS / "leases.json"
# held by a worker from its catch-up through the gate's test run to the fast-forward, so nothing lands between a gate
# and its landing
INTEGRATE_LOCK = threading.RLock()
# the loop files the driver itself writes when a task lands or is blocked: the marks, the log and the status
BOOKKEEPING = [path for path in (C.TASKS_REL, C.PROGRESS_REL, C.STATUS_REL) if path]
# a name a file declares at its top level (declaration_regex): two of one name in one file is a duplicate a merge made
TOP_NAME = C.DECLARATION


class Slot:
    """One worktree, and the task running in it."""

    def __init__(self, name):
        self.name = str(name)
        self.root = SLOTS_DIR / self.name
        self.branch = f"loop-slot-{self.name}"
        self.task = None
        self.spec = ""
        # the main branch's commit the slot was reset to, which the task's changes are measured against
        self.base = None
        self.started = None
        self.thread = None

    def snap(self):
        """The task's snapshot, for the gate's checks: the commit it started from; nothing was changed before it."""
        return {"head": self.base, "dirty": set()}

    def record(self):
        """What driver.json keeps of a busy slot, so STATUS and --shutdown from another process can see it."""
        return {"slot": self.name, "task": self.task, "base": self.base, "root": str(self.root), "started": self.started}


def branch():
    """The branch the driver lands work on: the one the main tree was on when the driver started."""
    return C.RUNTIME.get("branch") or C.main_branch()


def owner_file():
    return SLOTS_DIR / "driver.owner"


def other_owner():
    """The pid of another driver that holds the slots folder, 0 when none does: the pid in driver.owner is of a live
    Python process. A driver and the supervisor that started it are one owner."""
    pid = C.read_pid(owner_file())
    return pid if pid and pid not in (os.getpid(), os.getppid()) and C.pid_is_python(pid) else 0


def claim_slots():
    """Records this process as the driver that holds the slots folder, so a second driver, from any tree, finds it."""
    SLOTS_DIR.mkdir(parents=True, exist_ok=True)
    owner_file().write_text(str(os.getpid()), encoding="utf-8", newline="\n")


def release_slots():
    if C.read_pid(owner_file()) == os.getpid():
        owner_file().unlink(missing_ok=True)


def main_tip():
    return C.git("rev-parse", f"refs/heads/{branch()}").stdout.strip()


def other_roots(slot):
    """Every tree a slot's session must not touch: the main tree and the other slots."""
    found = [C.PROJECT]
    if SLOTS_DIR.is_dir():
        found += [path for path in SLOTS_DIR.iterdir() if path.is_dir() and path != slot.root]
    return found


def exclude_slot_files():
    """Lists each of slot_files in the repository's info/exclude, which every worktree reads, so a slot's own files
    are never a change, whatever the committed .gitignore says."""
    if not C.CFG.slot_files:
        return
    common = pathlib.Path(C.git("rev-parse", "--git-common-dir").stdout.strip() or ".git")
    exclude = (common if common.is_absolute() else C.PROJECT / common) / "info" / "exclude"
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    missing = [path for path in C.CFG.slot_files if path not in text.split("\n")]
    if missing:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(text.rstrip("\n") + ("\n" if text else "") + "\n".join(missing) + "\n", encoding="utf-8")


def ensure(slot):
    """Makes the slot's worktree if it does not exist yet, on its own branch."""
    exclude_slot_files()
    if (slot.root / ".git").exists():
        return
    SLOTS_DIR.mkdir(parents=True, exist_ok=True)
    C.git("worktree", "prune")
    if slot.root.exists():
        shutil.rmtree(slot.root, ignore_errors=True)
    made = C.git("worktree", "add", "-f", "-B", slot.branch, str(slot.root), main_tip())
    if made.returncode != 0:
        raise RuntimeError(f"could not make slot {slot.name} at {slot.root}: {made.stderr.strip()[:300]}")
    C.log(f"slot {slot.name}: made its worktree at {slot.root}")


def reset(slot, commit):
    """Puts the slot on its branch at commit with nothing changed. A rebase left half done is abandoned first."""
    if (pathlib.Path(C.git("rev-parse", "--git-dir", root=slot.root).stdout.strip() or ".") / "rebase-merge").exists():
        C.git("rebase", "--abort", root=slot.root)
    C.git("reset", "-q", "--hard", root=slot.root)
    C.git("checkout", "-q", "-B", slot.branch, commit, root=slot.root)
    C.git("reset", "-q", "--hard", commit, root=slot.root)
    C.git("clean", "-fdq", root=slot.root)


def prepare(slot, fresh=False):
    """Readies the slot for a task: its worktree at the main branch's last commit, with nothing changed. What an
    earlier run left there is saved to <logs>/revert-* first. Each of slot_files is written into it ({slot} becomes its
    name), so a tool that keys its data on the project's path keeps one slot's apart from another's. fresh also
    deletes cache_dirs, so the next test run starts from nothing, as a new clone would. Returns the commit."""
    ensure(slot)
    leftovers = sorted(C.changed_paths(slot.root))
    if leftovers:
        C.log(f"slot {slot.name}: saving and undoing what an earlier run left: {', '.join(leftovers[:5])}")
        undo_changes(leftovers, C.LOGS, slot.root, f"slot {slot.name}: left over before {C.ctx().get('task', 'a task')}"
                       f" (a crash, a shutdown or a failed attempt)")
    tip = main_tip()
    reset(slot, tip)
    if fresh:
        for folder in C.CFG.cache_dirs:
            shutil.rmtree(slot.root / folder, ignore_errors=True)
    for path, text in C.CFG.slot_files.items():
        target = slot.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.replace("{slot}", slot.name), encoding="utf-8", newline="\n")
    left = C.changed_paths(slot.root)
    if left or C.head(slot.root) != tip:
        raise RuntimeError(f"slot {slot.name} is not clean after its reset: {', '.join(sorted(left)[:3])}")
    slot.base = tip
    return tip


def revert(slot, why=""):
    """Saves, then undoes, everything the task changed in its slot, and drops any commit it made there. why goes in the
    copies' REASON.txt."""
    paths = sorted(C.changed_paths(slot.root)) if (slot.root / ".git").exists() else []
    if paths:
        context = C.ctx()
        undo_changes(paths, C.LOGS, slot.root, f"slot {slot.name}, task {context.get('task', slot.task)} run "
                       f"{context.get('run', '?')}: {why or 'undone'}")
    if slot.base:
        reset(slot, slot.base)


def hold(slot, name):
    """Keeps the slot's last commit on a branch held/<name>, for the owner to merge by hand. Returns the branch."""
    held = f"held/{name}"
    C.git("branch", "-f", held, "HEAD", root=slot.root)
    return held


def commit_work(slot, message, paths):
    """Commits these paths in the slot (an empty commit when none changed), on the slot's branch only."""
    changed = C.changed_paths(slot.root)
    paths = [path for path in dict.fromkeys(paths) if (slot.root / path).exists() or path in changed]
    if paths:
        C.git("add", "-A", "--", *paths, root=slot.root)
    done = C.git("commit", "-q", "--allow-empty", "-F", "-", stdin=message, root=slot.root)
    if done.returncode != 0:
        raise RuntimeError(f"git commit in slot {slot.name} failed: {done.stderr.strip()[:300]}")


def additive_merge(text, dedupe=False):
    """A conflicted file, resolved where each conflict only adds lines on both sides (its base part is empty): both
    additions are kept, the landed side's first. None when any conflict changes or removes a line. This merges two
    tasks that each add a function at the end of the same file, or a row at the end of the same table. With dedupe, a
    line the landed side also added is kept once: two tasks appending the same row to a table."""
    out = []
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("<<<<<<< "):
            out.append(line)
            index += 1
            continue
        parts = {"ours": [], "base": [], "theirs": []}
        part = "ours"
        index += 1
        while index < len(lines) and not lines[index].startswith(">>>>>>> "):
            current = lines[index]
            if current.startswith("||||||| "):
                part = "base"
            elif current.rstrip("\r\n") == "=======":
                part = "theirs"
            else:
                parts[part].append(current)
            index += 1
        if index >= len(lines) or parts["base"]:
            return None
        landed = {added.rstrip("\r\n") for added in parts["ours"]} if dedupe else set()
        theirs = [added for added in parts["theirs"] if added.rstrip("\r\n") not in landed]
        for added in parts["ours"] + theirs:
            out.append(added if added.endswith("\n") else added + "\n")
        index += 1
    return "".join(out)


def rebase(slot, onto):
    """Moves the slot's commits from its base onto onto, merging added lines (additive_merge; a keyed file keeps a row
    both sides added once). Returns '' when done, else why not, with the rebase undone."""
    def git(*args):
        return C.git("-c", "merge.conflictStyle=diff3", "-c", "core.editor=true", *args, root=slot.root)
    done = git("rebase", "-q", "--onto", onto, slot.base)
    for _ in range(20):
        if done.returncode == 0:
            return ""
        conflicted = git("diff", "--name-only", "--diff-filter=U").stdout.split()
        if not conflicted:
            git("rebase", "--abort")
            return f"the rebase failed: {(done.stderr or done.stdout).strip()[:200]}"
        for path in conflicted:
            file = slot.root / path
            merged = None
            if file.exists():
                with open(file, encoding="utf-8", errors="replace", newline="") as handle:
                    merged = additive_merge(handle.read(), dedupe=C.is_keyed(path))
            if merged is None:
                git("rebase", "--abort")
                return f"a conflict with work that landed meanwhile, in {', '.join(conflicted[:3])}"
            with open(file, "w", encoding="utf-8", newline="") as handle:
                handle.write(merged)
            git("add", "--", path)
        done = git("rebase", "--continue")
    git("rebase", "--abort")
    return "the rebase did not finish"


def duplicates(root, paths):
    """Names a merge may have doubled, in these files: a top-level name (declaration_regex) declared twice in one source
    file in the scope, or a key twice in one of keyed_files. Each as 'path: name twice'."""
    found = []
    for path in paths:
        file = pathlib.Path(root) / path
        if not file.is_file() or not C.code_changed([path]) or not C.is_source(path):
            continue
        text = file.read_text(encoding="utf-8", errors="replace")
        if C.is_keyed(path):
            names = [line.split(",", 1)[0] for line in text.splitlines()[1:] if line.strip()]
        else:
            names = TOP_NAME.findall(text)
        found += [f"{path}: {name} twice" for name in sorted({n for n in names if names.count(n) > 1})]
    return found


def added_names(root, start, end):
    """{name: file} of the top-level names (declaration_regex) the commits from start to end add in the scope."""
    names = {}
    current = ""
    for line in C.git("diff", "-U0", f"{start}..{end}", "--", *C.SCOPE, root=root).stdout.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("+") and current and C.is_source(current) and not C.is_keyed(current):
            match = TOP_NAME.match(line[1:])
            if match:
                names.setdefault(match.group(1), current)
    return names


def declared_before(root, base, name):
    """True when some file in the scope already declared name at the commit base."""
    found = C.git("grep", "-h", "-w", name, base, "--", *C.SCOPE, root=root).stdout
    return any(TOP_NAME.match(line) and TOP_NAME.match(line).group(1) == name for line in found.splitlines())


def clashes(root, old_base, tip):
    """New names the slot's work and the work that landed meanwhile both added in different files: the same helper
    written twice. Names the base already had (step, start, ...) and test functions do not count."""
    mine = added_names(root, tip, "HEAD")
    landed = added_names(C.PROJECT, old_base, tip)
    found = []
    for name, path in mine.items():
        if name.startswith(("_", "test_")) or name not in landed or landed[name] == path:
            continue
        if declared_before(root, old_base, name):
            continue
        found.append(f"{name} is also new in {landed[name]}, from work that landed meanwhile; use that one")
    return found


def recheck(slot, old_base, tip):
    """After a rebase onto newer work: no doubled names, no helper written twice, and the tests pass on exactly the tree
    that will land. Returns '' or why the work cannot land."""
    paths = C.git("diff", "--name-only", f"{tip}..HEAD", root=slot.root).stdout.split()
    doubled = duplicates(slot.root, paths)
    if doubled:
        return f"the merge with work that landed meanwhile doubled a name: {'; '.join(doubled[:3])}"
    clash = clashes(slot.root, old_base, tip)
    if clash:
        return f"the same helper was written twice: {'; '.join(clash[:3])}"
    if not C.code_changed(paths):
        return ""
    C.log(f"slot {slot.name}: work landed since the task started, so the tests run again on the rebased tree")
    failure = C.test_failure(C.test_output(root=slot.root))
    return f"the tests on the rebased tree: {failure}" if failure else ""


def catch_up(slot):
    """Moves the task's finished, uncommitted work onto the main branch's newest commit before the gate tests it, so the
    gate tests the tree that will land and the landing seldom needs a second test run. Doubled names and helpers
    written twice are found here, before the tests are spent. Returns '' (the work is uncommitted on the newest commit)
    or why it cannot land (the work is as it was)."""
    timer = C.Timer()
    reason = move_onto_tip(slot)
    if reason or slot.base != C.ctx().get("caught_from", slot.base):
        C.emit("catch_up", seconds=timer.seconds(), landed=C.ctx().get("caught_landed", 0), result=reason or "moved")
    return reason


def move_onto_tip(slot):
    """catch_up's work: '' when the work is on the newest commit, else why it cannot land."""
    tip = main_tip()
    old = slot.base
    C.ctx().update(caught_from=old, caught_landed=0)
    if tip == old:
        return ""
    C.ctx()["caught_landed"] = int(C.git("rev-list", "--count", f"{old}..{tip}").stdout.strip() or 0)
    if not C.changed_paths(slot.root):
        reset(slot, tip)
        slot.base = tip
        return ""
    C.git("add", "-A", root=slot.root)
    C.git("commit", "-q", "-m", "work in progress", root=slot.root)
    reason = rebase(slot, tip)
    rebased = not reason
    if rebased:
        paths = C.git("diff", "--name-only", f"{tip}..HEAD", root=slot.root).stdout.split()
        doubled = duplicates(slot.root, paths)
        clash = clashes(slot.root, old, tip)
        if doubled:
            reason = f"the merge with work that landed meanwhile doubled a name: {'; '.join(doubled[:3])}"
        elif clash:
            reason = f"the same helper was written twice: {'; '.join(clash[:3])}"
    C.git("reset", "-q", "--mixed", tip if rebased else old, root=slot.root)
    if rebased:
        slot.base = tip
    if reason:
        return reason
    landed = C.git("rev-list", "--count", f"{old}..{tip}").stdout.strip()
    C.log(f"slot {slot.name}: moved the work onto {tip[:8]} ({landed} commit(s) landed meanwhile) before the gate")
    return ""


def main_tree_problem():
    """Why the main tree cannot take a fast-forward now ('' when it can): it is on another branch."""
    current = C.main_branch()
    return "" if current == branch() else f"the main tree is on {current}, not {branch()}"


def integrate(slot, message, bookkeeping=None, test=True):
    """Lands the slot's work on the main branch, one slot at a time. The slot is rebased onto the newest commit; when
    other work landed since the task started, recheck() runs (with test). Then bookkeeping(root) marks the task,
    progress and status files on the rebased tree, so those lines never conflict, and the main tree fast-forwards. When
    the owner's uncommitted edits are in git's way, it waits, tells the phone once, and tries again. Returns '' when
    landed, else why not; the slot then holds the work, rebased or not."""
    timer = C.Timer()
    record = {"lock_wait": 0.0, "rebased": False, "retest_seconds": 0.0, "owner_wait": 0.0}
    with INTEGRATE_LOCK:
        record["lock_wait"] = timer.seconds()
        result = land_locked(slot, message, bookkeeping, test, record)
    C.emit("land", seconds=timer.seconds(), result=result or "landed", commit=C.head()[:10] if not result else "",
           **record)
    return result


def land_locked(slot, message, bookkeeping, test, record):
    """integrate()'s work, under the landing lock; record gets its timings."""
    told = False
    while True:
        if C.RUNTIME.get("stopping"):
            return "the driver is stopping"
        tip = main_tip()
        if C.head(slot.root) == slot.base:
            reset(slot, tip)
        elif tip != slot.base:
            old_base = slot.base
            reason = rebase(slot, tip)
            if reason:
                return reason
            slot.base = tip
            record["rebased"] = True
            retest = C.Timer()
            reason = recheck(slot, old_base, tip) if test else ""
            record["retest_seconds"] += retest.seconds()
            if reason:
                return reason
        slot.base = tip
        work = C.head(slot.root)
        if bookkeeping:
            bookkeeping(slot.root)
            present = [path for path in BOOKKEEPING if (slot.root / path).exists()]
            if present:
                C.git("add", "--", *present, root=slot.root)
            staged = C.git("diff", "--cached", "--quiet", root=slot.root).returncode != 0
            if staged and work != tip:
                C.git("commit", "-q", "--amend", "--no-edit", root=slot.root)
            elif staged or work == tip:
                C.git("commit", "-q", "--allow-empty", "-F", "-", stdin=message, root=slot.root)
        problem = main_tree_problem()
        done = None if problem else C.git("merge", "--ff-only", "-q", slot.branch)
        if done is not None and done.returncode == 0:
            C.log(f"landed on {branch()}: {message.splitlines()[0]}")
            sync()
            return ""
        reset(slot, work)
        if main_tip() != tip:
            continue
        detail = problem or (done.stderr or done.stdout).strip().replace("\n", " ")[:300]
        C.log(f"slot {slot.name}: cannot land yet: {detail}")
        if not told:
            told = True
            C.post("Driver waiting for the main tree", f"{(slot.task or message.splitlines()[0])} is ready to land, "
                   f"but git will not overwrite your uncommitted changes there, or the tree is on another branch: "
                   f"{detail}. Commit your edits (the driver reads only committed work) or stash them; it tries "
                   f"again every {C.POLL_SECONDS} s.", "high")
        waited = C.Timer()
        for _ in range(C.POLL_SECONDS):
            if C.RUNTIME.get("stopping"):
                break
            time.sleep(1)
        record["owner_wait"] += waited.seconds()


def sync():
    """Pushes the main branch after work lands, so it reaches the remote. A plain push only: a rejected push is
    reported, never forced, and the next landing tries again. Only with sync true in the config."""
    if not C.CFG.sync:
        return
    timer = C.Timer()
    try:
        done = subprocess.run(["git", "push", "origin", branch()], cwd=C.PROJECT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
    except subprocess.TimeoutExpired:
        C.emit("push", seconds=timer.seconds(), ok=False, detail="timed out")
        C.log("sync timed out")
        C.post("Sync failed", "git push took longer than 3 minutes. The commit is local only.", "high")
        return
    C.emit("push", seconds=timer.seconds(), ok=done.returncode == 0,
           detail="" if done.returncode == 0 else (done.stderr or done.stdout).strip()[-300:])
    if done.returncode == 0:
        C.log("synced to origin")
        return
    detail = (done.stderr or done.stdout).strip()[-300:]
    C.log(f"sync failed: {detail}")
    if "fetch first" in detail or "non-fast-forward" in detail:
        detail = (f"origin/{branch()} has commits this machine does not. When none of your edits is open, run git "
                  f"pull --rebase in the main tree; the driver's next push then carries both. {detail[-120:]}")
    C.post("Sync failed", f"The commit is local only: {detail[-300:]}", "high")


def lease(slot, task, spec):
    """Gives the slot its task's Files line, and records the shared files."""
    L.start(LEASES, slot.name, task, task_files(spec), C.SHARED_FILES)


def release(slot):
    L.release(LEASES, slot.name)


# ---------------------------------------------------------------------------
# The repository as a task sees it: the gate's checks, run in the task's slot.
# A task's snapshot is Slot.snap(): {"head": the commit it started from, "dirty": the paths changed before it,
# none in a fresh slot}. Each check compares against that commit, so a commit made during the task cannot hide
# what it changed.
# ---------------------------------------------------------------------------

DONE_WHEN_ITEM = re.compile(r"\b(test|file|output) `([^`]+)`")
LOOP_PATH = re.compile(r"`(" + re.escape(C.LOOP_DIR) + r"[^`]+)`")
FILES_TOKEN = re.compile(r"`([^`\s]+)`")
# a file name: something, a dot, and a short extension; is_file_name says whether such a name is a file at all
FILE_NAME = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")
TEST_FUNC = C.TEST_FUNC
ANY_FUNC = C.ANY_FUNC
CLASS_NAME = C.CLASS_NAME
# a word just before a Files path that says the task must not change it
NEGATED = re.compile(r"\b(?:no|not|never)\b[^`.;]{0,30}$", re.I)
TASK_ID = C.TASK_ID
ASSERT_CALL = C.ASSERT_CALL
# the loop files only an Opus or Fable task may change: it marks, adds and rewrites tasks
OPUS_LOOP_FILES = (C.TASKS_REL, C.PROGRESS_REL)


def task_changes(snap, root=None):
    """Every path changed since the snapshot, the loop folder included."""
    return sorted(set(C.changed_paths(root)) - (snap or {}).get("dirty", set()))


def in_scope(path, scope):
    return path.startswith(tuple(item.rstrip("/") + "/" for item in scope))


def outside_scope(snap, scope, root=None, named=()):
    """Paths the task changed outside the scope and the loop folder, except the ones its own Files line names (named,
    from task_files): a task that must change a build file says so there. A task must not touch these."""
    folders = tuple(path for path in named if path.endswith("/"))
    return [path for path in task_changes(snap, root)
            if not in_scope(path, scope) and not path.startswith(C.LOOP_DIR)
            and path not in named and not (folders and path.startswith(folders))]


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


def task_files(spec, root=None):
    """The paths a task's Files line names, from the project root: what its slot leases when it starts. A name without
    an extension is a folder and ends in /. files_strip comes off the front of a path. A path whose first folder exists
    at the project root keeps its own; any other gets files_prefix in front. A bare name that exists at the project
    root, or a dotfile (`.gitattributes`), is a project-root file; any other bare name with an extension gets
    files_prefix. A path the line says not to change ("Change no file under `src/`") is left out."""
    root = root or C.PROJECT
    found = []
    line = paragraph(spec, "**Files.**")
    for match in FILES_TOKEN.finditer(line):
        if NEGATED.search(line[max(0, match.start() - 40):match.start()]):
            continue
        token = match.group(1).strip().rstrip(",.;:")
        for strip in C.CFG.files_strip:
            token = token.removeprefix(strip)
        last = token.rsplit("/", 1)[-1]
        if "/" in token:
            if not (root / token.split("/", 1)[0]).exists():
                token = C.CFG.files_prefix + token
        elif token.startswith("."):
            pass
        elif not is_file_name(token, root):
            continue
        elif not (root / token).exists():
            token = C.CFG.files_prefix + token
        if "." not in last:
            token = token.rstrip("/") + "/"
        if token not in found:
            found.append(token)
    return found


def is_file_name(name, root=None):
    """True when a bare Files-line name (no folder) is a file, not a member such as `World.step` or `Config.LIMIT`: it
    exists at the project root or under files_prefix, or its extension is a source, keyed or file_extensions one."""
    if not FILE_NAME.search(name):
        return False
    root = root or C.PROJECT
    if (root / name).exists() or (C.CFG.files_prefix and (root / (C.CFG.files_prefix + name)).exists()):
        return True
    known = tuple(extension.lower() for extension in
                  [*C.CFG.source_extensions, *C.CFG.keyed_files, *C.CFG.file_extensions])
    return name.lower().endswith(known)


def mixed_paths(spec):
    """A fault when the Files paragraph writes some paths with a files_strip prefix (res://) and some with files_prefix
    (game/), else '': the two read differently, so a task should write them all one way."""
    if not C.CFG.files_prefix or not C.CFG.files_strip:
        return ""
    tokens = [m.group(1) for m in FILES_TOKEN.finditer(paragraph(spec, "**Files.**"))]
    stripped = [strip for strip in C.CFG.files_strip if any(t.startswith(strip) for t in tokens)]
    if stripped and any(t.startswith(C.CFG.files_prefix) for t in tokens):
        return f"the Files line mixes {stripped[0]} and {C.CFG.files_prefix} paths; write them all one way"
    return ""


def named_functions(spec, path, root=None):
    """The existing functions of the source file at path whose bodies a task changes. The scheduler lets two tasks
    share a shared file only while these do not meet; an empty set means the task only adds to the file (fields, new
    functions, or one call inserted in a list of calls).
    When the Files line names functions in brackets after the file (`src/core/world.py` (`finish`, one call in
    `_run_rooms`)), those are the answer, less the ones it only inserts a call in. Otherwise every backticked
    `Class.name`, `_private` or `name(...)` of the file anywhere in the spec counts."""
    file = (root or C.PROJECT) / path
    if path.endswith("/") or not file.is_file() or not C.is_source(path) or C.is_keyed(path):
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
    """The ids of other tasks a spec names anywhere ("T-11 builds on this", "T-10's ending")."""
    own = re.match(r"^### \[.\] (\S+)", spec)
    return set(TASK_ID.findall(spec)) - ({own.group(1)} if own else set())


def done_files(spec):
    """The files a task's Done when line names. The driver commits them with the scope."""
    return [value for kind, value in done_when(spec) if kind == "file"]


def landing_paths(spec):
    """What a worker task's landing commits: the scope, the loop-folder and Done when files it names, and every other
    file its Files line names. The gate lets a task change a named file outside the scope, such as a test runner, so
    the landing must keep it too: without it, a task lands its tests and loses the runner change the tests check."""
    return list(dict.fromkeys(C.SCOPE + sorted(allowed_loop_files(spec, False)) + done_files(spec) + task_files(spec)))


def allowed_loop_files(spec, opus):
    """The loop-folder paths a task may change: the ones its Files and Done when lines name, and for an Opus or Fable
    task also the task and progress files. A named path ending in / allows the whole folder."""
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


def removed(before, now, spec):
    """The test names in before and not in now, sorted, leaving out each name the task's spec names: a task may retire
    or rename a test it names, as weakened lets it loosen one."""
    return sorted(name for name in before - now if name not in spec)


def removed_tests(snap, root=None, spec=""):
    """Test functions the snapshot's commit has in a changed test file, that no test file has now, and that the task
    does not name (see removed). A test that moved to another file is not removed."""
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
    return removed(before, now_names, spec)


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
    without the line is refused: passing tests alone does not show that a task was done. A test is a test function in
    a file the test_files globs match."""
    root = root or C.PROJECT
    checks = done_when(spec)
    if not checks:
        return "the task has no **Done when.** line, so the driver cannot tell that it was done"
    for kind, value in checks:
        if kind == "test":
            if not has_test(root, value):
                return f"Done when: no test function {value} in the test files"
        elif kind == "file":
            if not (root / value).exists() or not C.git("status", "--porcelain", "--", value, root=root).stdout.strip():
                return f"Done when: {value} was not written"
        elif value not in output:
            return f"Done when: the test run did not print {value!r}"
    return ""


def precheck(spec):
    """The first fault a task's text shows before any session starts, or ''. A Done when the gate cannot meet: none at
    all, output that only another command prints (blind_outputs; the gate runs only the test command), or a loop-folder
    file the Files line does not name."""
    checks = done_when(spec)
    if not checks:
        return "the task has no **Done when.** line, so the driver cannot tell that it was done"
    named = task_files(spec)
    for kind, value in checks:
        if kind == "output":
            for text, why in (C.CFG.blind_outputs or {}).items():
                if value.startswith(text):
                    return f"Done when: output {value!r} is not printed by the gate's test run: {why}"
        elif kind == "file" and value.startswith(C.LOOP_DIR) and value not in named:
            return f"Done when: {value} is in the loop folder and the Files line does not name it"
    return mixed_paths(spec)


def has_test(root, name):
    """True when a test function called name is in a file the test_files globs match."""
    return any(name in TEST_FUNC.findall(path.read_text(encoding="utf-8", errors="replace"))
               for path in test_files(root))


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
    C.log(f"undid {len(paths)} changed files in {root}; copies in {shown}")


def remove_empty_dirs(paths, root=None):
    """Removes the folders that deleting new files left empty, up to the project root."""
    root = root or C.PROJECT
    for path in paths:
        folder = (root / path).parent
        while folder != root and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent
