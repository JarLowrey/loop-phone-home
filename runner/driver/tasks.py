"""The task queue and its record: reading the open tasks, marking the task and progress files (in a slot, so the
marks land with the work), blocking and requeueing a task, and the run of blocked tasks that starts a triage."""
import datetime
import re

import events as E
import loop_common as C
import phone as P
import prompts
import runtime as R
import settings as S
import slots as SL

PROGRESS = C.PROJECT / C.PROGRESS_REL
OPEN_HEADING = re.compile(C.CFG.open_task_pattern, re.M)


def tasks_file(root=None):
    return C.TASKS if root is None else root / C.TASKS_REL


def progress_file(root=None):
    return PROGRESS if root is None else root / C.PROGRESS_REL


def open_tasks(text=None):
    """(id, title) of every open task, in file order, from the committed task file unless text is given."""
    text = C.tasks_text() if text is None else text
    return [(match.group(1), match.group(2).strip().removeprefix("· ").strip())
            for match in OPEN_HEADING.finditer(text)]


def next_task(text=None):
    """(id, title) of the first open task, or None."""
    found = open_tasks(text)
    return found[0] if found else None


def queue_summary():
    """The 'Loop finished' text: what is done and what is blocked, so the reason the queue is empty is on the phone."""
    text = C.tasks_text()
    done = len(re.findall(r"^### \[x\] ", text, re.M))
    blocked = re.findall(r"^### \[!\] (\S+)", text, re.M)
    summary = f"No open tasks are left in {C.TASKS_REL}: {done} done, {len(blocked)} blocked"
    if blocked:
        summary += f" ({', '.join(blocked)})"
    return summary + ". Send RESUME after you add some."


def set_heading(task_id, mark, root=None):
    path = tasks_file(root)
    lines = path.read_text(encoding="utf-8").split("\n")
    for index, line in enumerate(lines):
        if re.match(rf"### \[ \] {re.escape(task_id)}(\s|$)", line):
            lines[index] = line.replace("### [ ]", f"### [{mark}]", 1)
            break
    else:
        raise RuntimeError(f"no open heading for {task_id}")
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def add_progress_row(row, root=None):
    path = progress_file(root)
    lines = path.read_text(encoding="utf-8").split("\n")
    last = max(i for i, line in enumerate(lines) if line.startswith("| 20"))
    lines.insert(last + 1, row)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def clear_blocked(task_id, root=None):
    """Removes a task's lines from the Blocked list when it is committed, so the list shows only what is still blocked."""
    path = progress_file(root)
    lines = path.read_text(encoding="utf-8").split("\n")
    if "## Blocked" not in lines:
        return
    start = lines.index("## Blocked")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(("## ", "(task id"))), len(lines))
    kept = [line for line in lines[start:end] if not line.startswith(f"- {task_id}:")]
    if len(kept) != end - start:
        path.write_text("\n".join(lines[:start] + kept + lines[end:]), encoding="utf-8", newline="\n")
        R.log(f"{task_id} left the Blocked list")


def add_blocked(task_id, reason, root=None):
    path = progress_file(root)
    lines = path.read_text(encoding="utf-8").split("\n")
    heading = lines.index("## Blocked")
    lines.insert(heading + 2, f"- {task_id}: {reason}")
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def done_marks(task_id, row):
    """The bookkeeping of a landed task, made on the rebased tree: its heading [x], off the Blocked list, its row."""
    def mark(root):
        set_heading(task_id, "x", root)
        clear_blocked(task_id, root)
        add_progress_row(row, root)
    return mark


def block(slot, task_id, title, reason, outcome):
    """Undoes the task's work in its slot, marks the task blocked, lands the mark and adds it to the run of blocks. Does
    nothing during a shutdown, which reverts the task itself."""
    if R.RUNTIME.get("stopping"):
        return
    SL.revert(slot, f"blocked: {reason[:300]}")

    def mark(root):
        set_heading(task_id, "!", root)
        add_blocked(task_id, reason, root)
    failed = SL.integrate(slot, f"{task_id}: blocked\n\n{reason}\n", mark, test=False)
    if failed:
        raise RuntimeError(f"could not record the block of {task_id}: {failed}")
    R.log(f"blocked {task_id} ({outcome}): {reason}")
    R.ctx().update(outcome="blocked", reason=reason[:400])
    note_for_digest(R.RUNTIME.get("state"), f"{task_id} blocked: {reason[:120]}")
    E.emit("block", reason=reason[:400], outcome=outcome)
    state = R.RUNTIME.get("state")
    if state is not None:
        with R.STATE_LOCK:
            state.setdefault("block_streak", []).append({"task": task_id, "reason": reason[:400]})
            count_day(state, "blocked")
            R.write_driver_state(state)


def requeue(slot, task_id, reason, state, wait_for=()):
    """The task's work could not land (a conflict, a doubled name, a failed re-test after the rebase), or it needed a
    file another running task held. It is undone and the task stays open, so it runs again on the newer tree, after the
    tasks in wait_for have landed; it is not the model's fault, so nothing escalates. After max_requeues of these it
    runs alone (scheduler.solo), where nothing can land meanwhile."""
    SL.revert(slot, f"requeued: {str(reason)[:300]}")
    with R.STATE_LOCK:
        if wait_for:
            state.setdefault("waits", {})[task_id] = sorted(set(wait_for))
        counts = state.setdefault("requeues", {})
        counts[task_id] = counts.get(task_id, 0) + 1
        count_day(state, "requeued")
        R.write_driver_state(state)
    R.log(f"requeued {task_id} ({counts[task_id]} time(s)): {reason}")
    R.ctx().update(outcome="requeued", reason=str(reason)[:400])
    note_for_digest(state, f"{task_id} requeued: {str(reason)[:120]}")
    E.emit("requeue", reason=str(reason)[:400], wait_for=sorted(set(wait_for)), count=counts[task_id])
    alert_requeues(state)


def count_day(state, outcome):
    """Counts a finished task's outcome (landed, blocked or requeued) for today, the basis of the requeue alert."""
    today = datetime.date.today().isoformat()
    day = state.get("day") or {}
    if day.get("date") != today:
        day = {"date": today, "landed": 0, "blocked": 0, "requeued": 0, "alerted": False}
    day[outcome] = day.get(outcome, 0) + 1
    state["day"] = day


def alert_requeues(state):
    """Tells the phone once a day when requeues pass REQUEUE_ALERT_SHARE of the day's finished tasks: each one is a
    session paid for with nothing landed."""
    day = state.get("day") or {}
    finished = day.get("landed", 0) + day.get("blocked", 0) + day.get("requeued", 0)
    if day.get("alerted") or finished < S.REQUEUE_ALERT_MIN or day.get("requeued", 0) <= S.REQUEUE_ALERT_SHARE * finished:
        return
    day["alerted"] = True
    R.write_driver_state(state)
    P.post("Many tasks requeued", f"{day['requeued']} of today's {finished} finished tasks could not land and ran "
           f"again. Set parallel_slots lower, or parallel_window smaller, in {C.CFG.loop_file}.", "high")


def announce(state, line):
    """Adds a landed task to the phone's digest, and sends the digest once commit_digest tasks have landed since the
    last one."""
    with R.STATE_LOCK:
        digest = state.setdefault("digest", {"landed": [], "other": [], "since": R.now()})
        digest["landed"].append(line)
        R.write_driver_state(state)
        if len(digest["landed"]) >= S.COMMIT_DIGEST:
            send_digest(state)


def note_for_digest(state, line):
    """A block or a requeue, told in the next digest rather than on its own."""
    if state is None:
        return
    with R.STATE_LOCK:
        state.setdefault("digest", {"landed": [], "other": [], "since": R.now()})["other"].append(line)


def send_digest(state):
    """Sends what landed (and what was blocked or requeued) since the last digest, with the day's spending, and starts
    a new one. Nothing is sent when nothing landed or failed."""
    with R.STATE_LOCK:
        digest = state.get("digest") or {}
        landed, other = digest.get("landed") or [], digest.get("other") or []
        if not landed and not other:
            return
        state["digest"] = {"landed": [], "other": [], "since": R.now()}
        R.write_driver_state(state)
    title = f"{len(landed)} task(s) landed" if landed else "No task landed"
    body = "\n".join(landed + other) + f"\nSince {digest.get('since', '?')}. Spent today: ${C.spend_on():.2f}."
    P.post(title, body)


def task_committed(state, task_id=None):
    """A commit ends a run of blocks, and with it the triage that run started. A committed task also counts toward the
    next monitor run (monitor.py)."""
    with R.STATE_LOCK:
        state["block_streak"] = []
        state["triaged"] = False
        if task_id:
            state.setdefault("monitor_pending", []).append(task_id)
            (state.get("requeues") or {}).pop(task_id, None)
            (state.get("waits") or {}).pop(task_id, None)
            count_day(state, "landed")
        R.write_driver_state(state)


def after_blocks(state):
    """Two blocks in a row queue an Opus triage of what they share. If the blocks go on after the triage, the driver
    pauses, since more attempts would cost money and fail the same way."""
    streak = state.get("block_streak") or []
    if state.get("triaged"):
        if len(streak) >= S.TRIAGE_AFTER_BLOCKS + S.PAUSE_AFTER_TRIAGE_BLOCKS:
            ids = ", ".join(item["task"] for item in streak)
            R.pause(state, "Driver paused: tasks keep failing", f"{len(streak)} tasks in a row were blocked ({ids}), "
                    f"the last ones after an Opus triage. Look at {C.PROGRESS_REL}, then send RESUME.", "urgent")
            task_committed(state)
        return
    if len(streak) >= S.TRIAGE_AFTER_BLOCKS:
        blocked = "\n".join(f"- {item['task']}: {item['reason']}" for item in streak)
        state["triaged"] = True
        state.setdefault("opus_writes", []).append(prompts.TRIAGE_PROMPT.format(blocked=blocked))
        R.write_driver_state(state)
        R.log(f"{len(streak)} blocks in a row: an Opus triage is queued")
        P.post("Triage queued", f"{len(streak)} tasks in a row were blocked, so Opus looks for what they share before "
               "the next task.")
