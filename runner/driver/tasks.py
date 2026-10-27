"""The task queue and its record: reading and marking the task file, the progress file, commits and pushes, undoing a
task's work, and the run of blocked tasks that starts a triage."""
import datetime
import os
import re
import subprocess
import sys

import phone as P
import runtime as R
import settings as S
import workspace as WS
import worker_task as W

C = W.C


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
    if not R.PATHS["progress"].exists():
        return
    lines = R.PATHS["progress"].read_text(encoding="utf-8").split("\n")
    if row:
        dated = [index for index, line in enumerate(lines) if line.startswith("|")]
        lines.insert((dated[-1] if dated else len(lines) - 1) + 1, row)
    if blocked:
        if "## Blocked" not in lines:
            lines += ["", "## Blocked", ""]
        lines.insert(lines.index("## Blocked") + 2, blocked)
    R.PATHS["progress"].write_text("\n".join(lines), encoding="utf-8", newline="\n")


def sync():
    """Pushes the branch after a commit, as a plain push: a rejected push is reported, never forced."""
    if not C.sync:
        return
    try:
        done = subprocess.run(["git", "push", "origin", "HEAD"], cwd=C.project, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
    except subprocess.TimeoutExpired:
        R.log("sync timed out")
        P.post("Sync failed", "git push took longer than 3 minutes. The commit is local only.", "high")
        return
    if done.returncode == 0:
        R.log("synced to origin")
        return
    detail = (done.stderr or done.stdout).strip()
    R.log(f"sync failed: {detail[-300:]}")
    P.post("Sync failed", f"The commit is local only: {detail[-200:]}", "high")


def commit(message, paths):
    """Commits the paths that exist or were deleted, with the message, and then syncs if sync is on."""
    changed = W.changed_paths()
    WS.git("add", "-A", "--", *[path for path in dict.fromkeys(paths)
                                if (C.project / path).exists() or path in changed])
    done = WS.git("commit", "-q", "-F", "-", stdin=message)
    if done.returncode != 0:
        raise RuntimeError(f"git commit failed: {(done.stderr or done.stdout).strip()[-300:]}")
    R.log(f"committed: {message.splitlines()[0]}")
    sync()


def driver_env():
    """The environment for a worker run or a revert: the same config, and the flag that tells the worker the driver
    answers the phone's inbox."""
    env = dict(os.environ)
    env["WORKER_CONFIG"] = str(C.config_path)
    env["WORKER_UNDER_DRIVER"] = "1"
    return env


def revert_unfinished(state=None):
    """Undoes an unfinished task: every change since its snapshot, the loop folder included, saved first. The owner's
    edits from before the task are left alone. With no snapshot, the worker's --revert undoes the scope."""
    snap = R.current_snap(state or R.RUNTIME.get("state"))
    if snap:
        WS.undo_changes(WS.task_changes(snap))
        return
    done = subprocess.run([sys.executable, R.PATHS["worker"], "--revert"], cwd=C.project, env=driver_env(),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    R.log(f"revert: {(done.stdout or done.stderr).strip()[-300:]}")


def clear_leftovers(task_id):
    """A task starts from a clean scope: changes an earlier run left there are saved and undone. True means go on."""
    leftovers = WS.scope_changes()
    if not leftovers:
        return True
    R.log(f"leftover changes in the scope before {task_id}: {', '.join(leftovers[:5])}")
    WS.undo_changes(leftovers)
    if WS.scope_changes():
        R.log(f"still dirty before {task_id} after the undo: {', '.join(WS.scope_changes()[:5])}")
        return False
    return True


def block(task_id, reason, outcome):
    """Undoes an unfinished task, marks it blocked and adds it to the run of blocks. The phone is not told: the driver
    moves on by itself, and a run of blocks starts a triage."""
    if R.RUNTIME.get("stopping"):
        return
    revert_unfinished()
    set_heading(task_id, "!")
    add_progress(blocked=f"- {task_id}: {reason}")
    commit(f"{task_id}: blocked\n\n{reason}\n", [C.tasks_file, C.progress_file])
    R.log(f"blocked {task_id} ({outcome}): {reason}")
    state = R.RUNTIME.get("state")
    if state is not None:
        state.setdefault("block_streak", []).append({"task": task_id, "reason": reason[:400]})
        R.write_state(state)


def finish_with_commit(task_id, title, totals, body, trailer="", extra_paths=()):
    """Marks a task done, records it in the progress file, commits it, tells the phone, and ends a run of blocks."""
    set_heading(task_id, "x")
    add_progress(row=f"| {datetime.date.today().isoformat()} | {task_id} | done | {totals} | {title} |")
    commit(f"{task_id}: {title}\n\n{body}{trailer}\n",
           list(C.scope) + list(extra_paths) + [C.tasks_file, C.progress_file])
    P.post("Committed", f"{task_id} {title}: {totals}")
    task_committed(R.RUNTIME["state"])


def task_committed(state):
    """A commit ends a run of blocks, and with it the triage that run started."""
    state["block_streak"] = []
    state["triaged"] = False
    R.write_state(state)


def after_blocks(state):
    """Two blocks in a row queue an Opus triage of what they share. If the blocks go on after the triage, the driver
    pauses, since more attempts would cost money and fail the same way."""
    streak = state.get("block_streak") or []
    if state.get("triaged"):
        if len(streak) >= S.TRIAGE_AFTER_BLOCKS + S.PAUSE_AFTER_TRIAGE_BLOCKS:
            ids = ", ".join(item["task"] for item in streak)
            P.pause(state, "Driver paused: tasks keep failing", f"{len(streak)} tasks in a row were blocked ({ids}), "
                    "the last ones after an Opus triage. Look at the progress file, then send RESUME.", "urgent")
            task_committed(state)
        return
    if len(streak) >= S.TRIAGE_AFTER_BLOCKS:
        state["triaged"] = True
        state["triage"] = "\n".join(f"- {item['task']}: {item['reason']}" for item in streak)
        R.write_state(state)
        R.log(f"{len(streak)} blocks in a row: an Opus triage is queued")
        P.post("Triage queued", f"{len(streak)} tasks in a row were blocked, so Opus looks for what they share before "
               "the next task.")
