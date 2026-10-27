"""Slots: the git worktrees the tasks run in, apart from the owner's main tree. Before a task, its slot is reset to the
main branch's last commit, so the owner's uncommitted edits are never the task's, and nothing the task does touches the
owner's tree. A task's work reaches the main branch only through integrate(): one slot at a time, rebased onto the
newest commit, checked and tested again when other work landed meanwhile, then fast-forwarded. Git never overwrites an
uncommitted change of the owner's: when one is in the way, the driver waits and says which file."""
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time

import events as E
import leases as L
import loop_common as C
import phone as P
import runtime as R
import settings as S
import workspace as W

def default_slots_dir():
    """<LOCALAPPDATA, or ~/.cache>/loop-slots/<project folder name>: outside the project, and outside a synced folder."""
    base = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / ".cache")
    return pathlib.Path(base) / "loop-slots" / C.PROJECT.name


SLOTS_DIR = pathlib.Path(os.environ.get("LOOP_SLOTS_DIR") or C.CFG.slots_dir or default_slots_dir()).expanduser()
LEASES = C.LOGS / "leases.json"
INTEGRATE_LOCK = threading.Lock()
BOOKKEEPING = [C.TASKS_REL, C.PROGRESS_REL]
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
    return R.RUNTIME.get("branch") or C.main_branch()


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
    R.log(f"slot {slot.name}: made its worktree at {slot.root}")


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
        R.log(f"slot {slot.name}: saving and undoing what an earlier run left: {', '.join(leftovers[:5])}")
        W.undo_changes(leftovers, C.LOGS, slot.root, f"slot {slot.name}: left over before {R.ctx().get('task', 'a task')}"
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
        context = R.ctx()
        W.undo_changes(paths, C.LOGS, slot.root, f"slot {slot.name}, task {context.get('task', slot.task)} run "
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


def additive_merge(text):
    """A conflicted file, resolved where each conflict only adds lines on both sides (its base part is empty): both
    additions are kept, the landed side's first. None when any conflict changes or removes a line. This merges two
    tasks that each add a signal at the end of the same list, or a string at the end of the same group."""
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
        for added in parts["ours"] + parts["theirs"]:
            out.append(added if added.endswith("\n") else added + "\n")
        index += 1
    return "".join(out)


def rebase(slot, onto):
    """Moves the slot's commits from its base onto onto, merging added lines (additive_merge). Returns '' when done,
    else why not, with the rebase undone."""
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
                    merged = additive_merge(handle.read())
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
        if path.endswith(tuple(C.CFG.keyed_files)):
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
        elif line.startswith("+") and current and C.is_source(current) and not current.endswith(
                tuple(C.CFG.keyed_files)):
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
    R.log(f"slot {slot.name}: work landed since the task started, so the tests run again on the rebased tree")
    failure = C.test_failure(C.test_output(root=slot.root))
    return f"the tests on the rebased tree: {failure}" if failure else ""


def catch_up(slot):
    """Moves the task's finished, uncommitted work onto the main branch's newest commit before the gate tests it, so the
    gate tests the tree that will land and the landing seldom needs a second test run. Doubled names and helpers
    written twice are found here, before the tests are spent. Returns '' (the work is uncommitted on the newest commit)
    or why it cannot land (the work is as it was)."""
    timer = E.Timer()
    reason = move_onto_tip(slot)
    if reason or slot.base != R.ctx().get("caught_from", slot.base):
        E.emit("catch_up", seconds=timer.seconds(), landed=R.ctx().get("caught_landed", 0), result=reason or "moved")
    return reason


def move_onto_tip(slot):
    """catch_up's work: '' when the work is on the newest commit, else why it cannot land."""
    tip = main_tip()
    old = slot.base
    R.ctx().update(caught_from=old, caught_landed=0)
    if tip == old:
        return ""
    R.ctx()["caught_landed"] = int(C.git("rev-list", "--count", f"{old}..{tip}").stdout.strip() or 0)
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
    R.log(f"slot {slot.name}: moved the work onto {tip[:8]} ({landed} commit(s) landed meanwhile) before the gate")
    return ""


def main_tree_problem():
    """Why the main tree cannot take a fast-forward now ('' when it can): it is on another branch."""
    current = C.main_branch()
    return "" if current == branch() else f"the main tree is on {current}, not {branch()}"


def integrate(slot, message, bookkeeping=None, test=True):
    """Lands the slot's work on the main branch, one slot at a time. The slot is rebased onto the newest commit; when
    other work landed since the task started, recheck() runs (with test). Then bookkeeping(root) marks the task and
    progress files on the rebased tree, so those lines never conflict, and the main tree fast-forwards. When the
    owner's uncommitted edits are in git's way, it waits, tells the phone once, and tries again. Returns '' when landed,
    else why not; the slot then holds the work, rebased or not."""
    timer = E.Timer()
    record = {"lock_wait": 0.0, "rebased": False, "retest_seconds": 0.0, "owner_wait": 0.0}
    with INTEGRATE_LOCK:
        record["lock_wait"] = timer.seconds()
        result = land_locked(slot, message, bookkeeping, test, record)
    E.emit("land", seconds=timer.seconds(), result=result or "landed", commit=C.head()[:10] if not result else "",
           **record)
    return result


def land_locked(slot, message, bookkeeping, test, record):
    """integrate()'s work, under the landing lock; record gets its timings."""
    told = False
    while True:
        if R.RUNTIME.get("stopping"):
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
            retest = E.Timer()
            reason = recheck(slot, old_base, tip) if test else ""
            record["retest_seconds"] += retest.seconds()
            if reason:
                return reason
        slot.base = tip
        work = C.head(slot.root)
        if bookkeeping:
            bookkeeping(slot.root)
            C.git("add", "--", *BOOKKEEPING, root=slot.root)
            staged = C.git("diff", "--cached", "--quiet", root=slot.root).returncode != 0
            if staged and work != tip:
                C.git("commit", "-q", "--amend", "--no-edit", root=slot.root)
            elif staged or work == tip:
                C.git("commit", "-q", "--allow-empty", "-F", "-", stdin=message, root=slot.root)
        problem = main_tree_problem()
        done = None if problem else C.git("merge", "--ff-only", "-q", slot.branch)
        if done is not None and done.returncode == 0:
            R.log(f"landed on {branch()}: {message.splitlines()[0]}")
            sync()
            return ""
        reset(slot, work)
        if main_tip() != tip:
            continue
        detail = problem or (done.stderr or done.stdout).strip().replace("\n", " ")[:300]
        R.log(f"slot {slot.name}: cannot land yet: {detail}")
        if not told:
            told = True
            P.post("Driver waiting for the main tree", f"{(slot.task or message.splitlines()[0])} is ready to land, "
                   f"but git will not overwrite your uncommitted changes there, or the tree is on another branch: "
                   f"{detail}. Commit your edits (the driver reads only committed work) or stash them; it tries "
                   f"again every {S.POLL_SECONDS} s.", "high")
        waited = E.Timer()
        for _ in range(S.POLL_SECONDS):
            if R.RUNTIME.get("stopping"):
                break
            time.sleep(1)
        record["owner_wait"] += waited.seconds()


def sync():
    """Pushes the main branch after work lands, so it reaches the remote. A plain push only: a rejected push is
    reported, never forced, and the next landing tries again. Only with sync true in the config."""
    if not C.CFG.sync:
        return
    timer = E.Timer()
    try:
        done = subprocess.run(["git", "push", "origin", branch()], cwd=C.PROJECT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
    except subprocess.TimeoutExpired:
        E.emit("push", seconds=timer.seconds(), ok=False, detail="timed out")
        R.log("sync timed out")
        P.post("Sync failed", "git push took longer than 3 minutes. The commit is local only.", "high")
        return
    E.emit("push", seconds=timer.seconds(), ok=done.returncode == 0,
           detail="" if done.returncode == 0 else (done.stderr or done.stdout).strip()[-300:])
    if done.returncode == 0:
        R.log("synced to origin")
        return
    detail = (done.stderr or done.stdout).strip()[-300:]
    R.log(f"sync failed: {detail}")
    if "fetch first" in detail or "non-fast-forward" in detail:
        detail = (f"origin/{branch()} has commits this PC does not. When none of your edits is open, run git pull "
                  f"--rebase in the main tree; the driver's next push then carries both. {detail[-120:]}")
    P.post("Sync failed", f"The commit is local only: {detail[-300:]}", "high")


def lease(slot, task, spec):
    """Gives the slot its task's Files line, and records the shared files."""
    L.start(LEASES, slot.name, task, W.task_files(spec), S.SHARED_FILES)


def release(slot):
    L.release(LEASES, slot.name)
