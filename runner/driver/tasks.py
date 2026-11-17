"""The task queue and its record: reading the open tasks, marking the task, progress and status files (in a slot, so
the marks land with the work), blocking and requeueing a task, the run of blocked tasks that starts a triage, and the
review packet a review task starts from."""
import datetime
import collections
import subprocess
import json
import re
import textwrap

import core as C
import monitor as MO
import prompts
import report as RP
import slots as SL

PROGRESS = C.PROJECT / C.PROGRESS_REL
STATUS = C.PROJECT / C.STATUS_REL if C.STATUS_REL else None
OPEN_HEADING = re.compile(C.CFG.open_task_pattern, re.M)
ANY_HEADING = re.compile(C.CFG.task_pattern, re.M)
# a row of the progress file's log: | when | task | result | tests | notes |
PROGRESS_ROW = re.compile(r"^\| (20\S+) \| (\S+) \| ([^|]*)\|", re.M)
# how many of the newest done tasks the status file names before pointing at the log
STATUS_RECENT = 5
# how a session's last message names a number it picked (picked_text), and the progress file heading it is logged under
PICKED_LABEL = "**Picked.**"
PICKED_LINE = "Picked:"
OPEN_QUESTIONS = "## Open questions"
BLOCKED = "## Blocked"
# How long a task takes, from the driver's logs: a worker task is about 6 minutes of session and 2 of gate, an Opus or
# Fable task about 30, and a task that lands after other work landed during its gate runs the tests again.
# simulate.py simulates the scheduler with these, and progress_text adds them up for one slot.
WORKER_MINUTES = 6.0
GATE_MINUTES = 2.0
RETEST_MINUTES = 2.0
SENIOR_MINUTES = 30.0

# how far the queue has come, as counts() reads it
Counts = collections.namedtuple(
    "Counts", "done left blocked waiting total milestone milestone_done milestone_total"
)


def title_of(text):
    """A heading's title, without the middle dot some task files put between the id and the title."""
    return text.strip().removeprefix("·").strip()


def headings(text):
    """(mark, id, title) of every task heading in the text, in file order."""
    return [(mark, task_id, title_of(title)) for mark, task_id, title in ANY_HEADING.findall(text)]


def senior_model(spec):
    """The model a task's Who line names, opus or fable, else ''. Such a task is a review, an audit or a task revision:
    that model does it once, alone."""
    match = re.search(r"^\*\*Who\.\*\* (Opus|Fable)\b", spec, re.M)
    return match.group(1).lower() if match else ""


def is_senior_task(spec):
    return bool(senior_model(spec))


def senior_effort(spec):
    """The effort a senior task asks for in an **Effort.** line (low, medium, high, xhigh or max), else its model's
    default."""
    match = re.search(r"^\*\*Effort\.\*\* (\w+)", spec, re.M)
    if match and match.group(1).lower() in C.WORKER_EFFORTS:
        return match.group(1).lower()
    return C.SENIOR_MODELS[senior_model(spec) or "opus"][1]


def tasks_file(root=None):
    return C.TASKS if root is None else root / C.TASKS_REL


def progress_file(root=None):
    return PROGRESS if root is None else root / C.PROGRESS_REL


def status_file(root=None):
    """The status file in the main tree or root, or None when the config names none."""
    if not C.STATUS_REL:
        return None
    return STATUS if root is None else root / C.STATUS_REL


def read(path):
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def open_tasks(text=None):
    """(id, title) of every open task, in file order, from the committed task file unless text is given."""
    text = C.tasks_text() if text is None else text
    return [(match.group(1), title_of(match.group(2))) for match in OPEN_HEADING.finditer(text)]


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


def blocked_reasons(root=None):
    """{task id: why it was blocked} from the Blocked list in the progress file."""
    lines = read(progress_file(root)).split("\n")
    if BLOCKED not in lines:
        return {}
    start = lines.index(BLOCKED)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    found = {}
    for line in lines[start:end]:
        if line.startswith("- ") and ": " in line:
            task_id, reason = line[2:].split(": ", 1)
            found[task_id] = reason.strip()
    return found


def open_specs(root=None):
    """(task id, spec) for each open task in the task file, in file order. The spec is the whole run under its
    heading, so a caller can read its Who line."""
    lines = read(tasks_file(root)).split("\n")
    out = []
    for kind, task_id, body in runs(lines):
        if kind == "task" and body and body[0].startswith("### [ ] "):
            out.append((task_id, "\n".join(body)))
    return out


def unparsed_headings(text):
    """Every line that starts `### [` and matches ANY_HEADING nowhere, as the line itself stripped: counts() and
    status_text() skip these silently."""
    return [line.strip() for line in text.split("\n") if line.startswith("### [") and not ANY_HEADING.match(line)]


def counts(root=None):
    """How far the queue has come: the tasks done, left, blocked and waiting, their total, and the same for the
    milestone the next open task belongs to (the part of its id before the first -). The phone's STATUS and --check both
    read it, so one count answers both."""
    found = headings(read(tasks_file(root)))
    rows = PROGRESS_ROW.findall(read(progress_file(root)))
    done = [task_id for _, task_id, result in rows if "done" in result]
    left = [task_id for mark, task_id, _ in found if mark == " "]
    open_now = left + [task_id for mark, task_id, _ in found if mark in "!?~"]
    blocked = sum(1 for mark, _, _ in found if mark == "!")
    waiting = sum(1 for mark, _, _ in found if mark in "?~")
    here = left[0].split("-")[0] if left else ""
    mine = [task_id for task_id in done if task_id.split("-")[0] == here]
    return Counts(
        done=len(done),
        left=len(left),
        blocked=blocked,
        waiting=waiting,
        total=len(done) + len(open_now),
        milestone=here,
        milestone_done=len(mine),
        milestone_total=len(mine) + sum(1 for task_id in open_now if task_id.split("-")[0] == here),
    )


def remaining_minutes(root=None):
    """Minutes the open queue takes with one task running at a time: each task's own time added up. It is an upper
    bound, because parallel_slots can only shorten it, and it is the figure simulate.py prints for one slot. That tool
    accounts for the slots and for the files two tasks share; this is the number a status line can carry without
    simulating the scheduler."""
    total = 0.0
    for _, spec in open_specs(root):
        total += SENIOR_MINUTES if is_senior_task(spec) else WORKER_MINUTES + GATE_MINUTES
    return total


def progress_text(root=None):
    """One line of how far the queue has come, for the phone's STATUS and --check: the tasks done of the total, what
    is left, the milestone's own share, and how long the rest takes one task at a time."""
    found = counts(root)
    text = f"Done {found.done}/{found.total}, {found.left} left"
    if found.milestone and found.milestone_total != found.total:
        text += f" ({found.milestone}: {found.milestone_done}/{found.milestone_total})"
    if found.blocked:
        text += f", {found.blocked} blocked"
    if found.waiting:
        text += f", {found.waiting} waiting on you"
    if not found.left:
        return text + ". The queue is empty."
    return text + f", about {remaining_minutes(root) / 60.0:.1f} h one at a time."


def status_text(root=None):
    """The status file: what is left, what is blocked and what is done, in that order, from the queue and the log. Only
    the lasting state, so it can be committed with the task that changed it; check_driver.py shows what is running."""
    tasks = read(tasks_file(root))
    found = headings(tasks)
    left = [(task_id, title) for mark, task_id, title in found if mark == " "]
    blocked = [(task_id, title) for mark, task_id, title in found if mark == "!"]
    waiting = [(task_id, title) for mark, task_id, title in found if mark in "?~"]
    reasons = blocked_reasons(root)
    rows = PROGRESS_ROW.findall(read(progress_file(root)))
    done = [(when, task_id) for when, task_id, result in rows if "done" in result]
    out = [
        "# Status",
        "",
        "What is left, what is blocked and what is done. The driver rewrites this file whenever a task lands, so do not",
        "edit it; `python <runner>/driver --status` rewrites it now. For what is running this minute, and today's",
        "spending, run `python <runner>/driver --check`.",
        "",
        f"## Left to do ({len(left)})",
        "",
    ]
    if left:
        for index, (task_id, title) in enumerate(left):
            out.append(f"{index + 1}. **{task_id}** · {title}" + ("  ← next" if index == 0 else ""))
    else:
        out.append("Nothing. The driver stops when the queue is empty and says so on the phone.")
    out += ["", f"## Blocked ({len(blocked)})", ""]
    if blocked:
        for task_id, title in blocked:
            out.append(f"- **{task_id}** · {title}")
            out.append(f"  - {reasons.get(task_id, f'no reason recorded in {C.PROGRESS_REL}')}")
    else:
        out.append("Nothing.")
    odd = unparsed_headings(tasks)
    if odd:
        out += ["", f"## Unparsed headings ({len(odd)})", ""]
        out += [f"- {line}" for line in odd]
        out += ["", "A heading needs `### [mark] ID Title` (task_pattern in the config); these are no task to the "
                "driver."]
    if waiting:
        out += ["", f"## Waiting on you ({len(waiting)})", ""]
        out += [f"- **{task_id}** · {title}" for task_id, title in waiting]
    out += ["", "## Monitor", ""] + MO.status_lines(root)
    out += ["", f"## Done ({len(done)})", ""]
    for when, task_id in done[-STATUS_RECENT:][::-1]:
        out.append(f"- {when} · {task_id}")
    out += [
        "",
        f"The whole record, with the tests each task left passing, is the log in `{C.PROGRESS_REL}`.",
    ]
    if C.ARCHIVE_REL:
        out[-1] += f" The specs of landed tasks that `--tidy` moved are in `{C.ARCHIVE_REL}`."
    return "\n".join(out) + "\n"


def write_status(root=None):
    """Rewrites the status file (nothing when the config names none). Called wherever the queue changes, so one short
    file always answers where things are."""
    path = status_file(root)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(status_text(root), encoding="utf-8", newline="\n")


def runs(lines):
    """The task file as (kind, title, body) runs: ("header", None, lines) once, then ("section", title, prose) and
    ("task", id, body) in file order. Nothing is dropped, so joining every body returns the file."""
    first = next((i for i, line in enumerate(lines) if ANY_HEADING.match(line) or line.startswith("## ")), len(lines))
    out, index = [("header", None, lines[:first])], first
    while index < len(lines):
        found = ANY_HEADING.match(lines[index])
        if lines[index].startswith("## "):
            out.append(("section", lines[index][3:], [lines[index]]))
            index += 1
        elif found:
            body, index = [lines[index]], index + 1
            while index < len(lines) and not ANY_HEADING.match(lines[index]) and not lines[index].startswith("## "):
                body.append(lines[index])
                index += 1
            out.append(("task", found.group(2), body))
        else:
            out[-1] = (out[-1][0], out[-1][1], out[-1][2] + [lines[index]])
            index += 1
    return out


def _section_has_work(parts, index):
    """True when the section run at index keeps a reason to exist: a task before the next section that is not done, or
    no tasks at all, which makes it the file's own method text rather than a milestone."""
    tasks = []
    for kind, _, body in parts[index + 1:]:
        if kind == "section":
            break
        if kind == "task":
            tasks.append(body)
    return not tasks or any(not body[0].startswith("### [x] ") for body in tasks)


def lint_tasks(root=None):
    """Every text-only fault the gate would hit over the open tasks, as "<task id>: <fault>" lines, before a session is
    paid for. "files:" lines show how each Files line reads; "warn:" lines are not faults."""
    found = []
    base = root or C.PROJECT
    for task_id, spec in open_specs(root):
        fault = SL.precheck(spec)
        if fault:
            found.append(f"{task_id}: {fault}")
        named = SL.task_files(spec, root)
        found.append(f"{task_id}: files: {', '.join(named)}")
        for kind, value in SL.done_when(spec):
            if kind == "file" and value not in named and not SL.in_scope(value, C.SCOPE):
                found.append(f"{task_id}: Done when: file {value} is not on the Files line")
            elif kind == "test" and SL.has_test(base, value):
                found.append(f"{task_id}: warn: Done when: test {value} already exists, so it cannot show this task "
                             "was done")
    for line in unparsed_headings(read(tasks_file(root))):
        found.append(f"-: unparsed heading: {line}")
    return found


def cli_lint_tasks():
    """Prints lint_tasks and returns 1 when any line is a fault, else 0."""
    lines = lint_tasks()
    for line in lines:
        C.log(line)
    faults = [line for line in lines if ": files: " not in line and ": warn: " not in line]
    if faults:
        return 1
    C.log(f"lint-tasks: {len(open_specs())} open tasks, no faults")
    return 0


def cli_tidy():
    """--tidy: moves every done spec out of the task file into the archive file, so the queue holds only what is left.
    The specs are history once a task has landed; the progress file is the record of what it did."""
    driver = C.read_pid(C.DRIVER_PID)
    if driver and C.pid_alive(driver):
        print("the driver is running: stop it with python <runner>/driver --shutdown, then tidy")
        return 1
    if not C.ARCHIVE_REL:
        print("no archive_file in the config, so there is nowhere to move the done specs")
        return 1
    parts = runs(read(tasks_file()).split("\n"))
    done = [body for kind, name, body in parts if kind == "task" and body[0].startswith("### [x] ")]
    if not done:
        print(f"nothing to tidy: {C.TASKS_REL} holds no done specs")
        return 0
    keep = []
    for index, (kind, name, body) in enumerate(parts):
        if kind == "task" and body[0].startswith("### [x] "):
            continue
        # why: a section whose every task has just gone would be left as a bare heading, so it goes with them. A
        # section that never held a task is the file's own method text and stays.
        if kind == "section" and not _section_has_work(parts, index):
            continue
        keep += body
    while keep and keep[-1] == "":
        keep.pop()
    archive = C.PROJECT / C.ARCHIVE_REL
    if not archive.exists():
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(f"# Task specs of tasks that have landed\n\nMoved here by `python <runner>/driver --tidy`, "
                           f"so `{C.TASKS_REL}` holds only what is left to do. What each task did, and the tests it "
                           f"left passing, is the log in `{C.PROGRESS_REL}`. Nothing reads this file.\n",
                           encoding="utf-8", newline="\n")
    with open(archive, "a", encoding="utf-8", newline="\n") as out:
        out.write("\n" + "\n".join(line for body in done for line in body).rstrip("\n") + "\n")
    tasks_file().write_text("\n".join(keep) + "\n", encoding="utf-8", newline="\n")
    write_status()
    also = f", and {C.STATUS_REL}" if C.STATUS_REL else ""
    print(f"moved {len(done)} done spec(s) to {C.ARCHIVE_REL}; {C.TASKS_REL} is now {len(keep) + 1} lines. Commit "
          f"both{also}.")
    return 0


def heading_line(mark, task_id):
    """A regex for the heading of task_id with this mark (a character, such as ' ', 'x' or '!')."""
    return re.compile(rf"^### \[{re.escape(mark)}\] {re.escape(task_id)}(?:\s|$)")


def set_heading(task_id, mark, root=None):
    path = tasks_file(root)
    lines = path.read_text(encoding="utf-8").split("\n")
    opened = heading_line(" ", task_id)
    for index, line in enumerate(lines):
        if opened.match(line):
            lines[index] = line.replace("### [ ]", f"### [{mark}]", 1)
            break
    else:
        raise RuntimeError(f"no open heading for {task_id}")
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def section_lines(lines, heading):
    """The lines with the section heading present: the file's own, or the file with the heading added at its end."""
    if heading in lines:
        return lines
    while lines and not lines[-1].strip():
        lines.pop()
    return lines + ["", heading, ""]


def add_progress_row(row, root=None):
    """Adds a row after the log table's last row; a log without rows gets it after its header line, and a file
    without a table gets one."""
    path = progress_file(root)
    lines = read(path).split("\n")
    rows = [i for i, line in enumerate(lines) if line.startswith("| 20")]
    if rows:
        lines.insert(rows[-1] + 1, row)
    else:
        header = next((i for i, line in enumerate(lines) if line.startswith("|---") or line.startswith("| ---")), None)
        if header is None:
            lines = lines + ["", "| Date | Task | Result | Tests | Notes |", "|---|---|---|---|---|"]
            header = len(lines) - 1
        lines.insert(header + 1, row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def clear_blocked(task_id, root=None):
    """Removes a task's lines from the Blocked list when it is committed, so the list shows only what is still blocked."""
    path = progress_file(root)
    lines = read(path).split("\n")
    if BLOCKED not in lines:
        return
    start = lines.index(BLOCKED)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(("## ", "(task id"))), len(lines))
    kept = [line for line in lines[start:end] if not line.startswith(f"- {task_id}:")]
    if len(kept) != end - start:
        path.write_text("\n".join(lines[:start] + kept + lines[end:]), encoding="utf-8", newline="\n")
        C.log(f"{task_id} left the Blocked list")


def add_blocked(task_id, reason, root=None):
    path = progress_file(root)
    lines = section_lines(read(path).split("\n"), BLOCKED)
    heading = lines.index(BLOCKED)
    lines.insert(min(heading + 2, len(lines)), f"- {task_id}: {reason}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def reopen(task_id, root=None):
    """Turns a blocked task's heading back to open and takes it off the Blocked list. True when it was blocked."""
    path = tasks_file(root)
    lines = path.read_text(encoding="utf-8").split("\n")
    blocked = heading_line("!", task_id)
    for index, line in enumerate(lines):
        if blocked.match(line):
            lines[index] = line.replace("### [!]", "### [ ]", 1)
            break
    else:
        return False
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    clear_blocked(task_id, root)
    return True


def reopen_marks(task_ids, reopened):
    """The bookkeeping of a landed triage, made on the rebased tree: every task of the run of blocks it looked at that
    is still blocked opens again, so it is tried on the fixed code. reopened gets the ids it opened."""
    def mark(root):
        reopened.clear()
        reopened.extend(task_id for task_id in task_ids if reopen(task_id, root))
        write_status(root)
    return mark


def forget_attempts(state, task_ids):
    """Clears what the driver remembers of earlier runs of these tasks, so each starts again as a first run."""
    with C.STATE_LOCK:
        for key in ("requeues", "waits", "attempts", "misplaced"):
            for task_id in task_ids:
                (state.get(key) or {}).pop(task_id, None)
        C.write_driver_state(state)


def picked_text(words):
    """The numbers a session says it picked, from its last message: the paragraph that starts **Picked.** (to the next
    blank line, the label removed), or else every line that starts Picked: joined with '; '. '' when it has neither."""
    lines = words.splitlines()
    for start, line in enumerate(lines):
        if line.lstrip().startswith(PICKED_LABEL):
            paragraph = [line.lstrip()[len(PICKED_LABEL):]]
            for more in lines[start + 1:]:
                if not more.strip():
                    break
                paragraph.append(more)
            return " ".join(part.strip() for part in paragraph if part.strip())
    labelled = [line.lstrip()[len(PICKED_LINE):].strip() for line in lines if line.lstrip().startswith(PICKED_LINE)]
    return "; ".join(part for part in labelled if part)


def add_open_question(task_id, text, root=None):
    """Adds `- <task> (<date>): <text>` as the last bullet of the progress file's Open questions: before its ### Resolved
    line, else at the end of the section. A file without the section gets one at its end."""
    path = progress_file(root)
    lines = section_lines(read(path).split("\n"), OPEN_QUESTIONS)
    start = lines.index(OPEN_QUESTIONS)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(("### Resolved", "## "))), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    bullet = textwrap.fill(f"- {task_id} ({datetime.date.today().isoformat()}): {text}", width=120,
                           subsequent_indent="  ")
    lines[end:end] = bullet.split("\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def done_marks(task_id, row, picked=""):
    """The bookkeeping of a landed task, made on the rebased tree: its heading [x], off the Blocked list, its row, and
    the numbers it picked under Open questions."""
    def mark(root):
        set_heading(task_id, "x", root)
        clear_blocked(task_id, root)
        add_progress_row(row, root)
        if picked:
            add_open_question(task_id, picked, root)
        write_status(root)
    return mark


def block(slot, task_id, title, reason, outcome):
    """Undoes the task's work in its slot, marks the task blocked, lands the mark and adds it to the run of blocks. Does
    nothing during a shutdown, which reverts the task itself."""
    if C.RUNTIME.get("stopping"):
        return
    SL.revert(slot, f"blocked: {reason[:300]}")

    def mark(root):
        set_heading(task_id, "!", root)
        add_blocked(task_id, reason, root)
        write_status(root)
    failed = SL.integrate(slot, f"{task_id}: blocked\n\n{reason}\n", mark, test=False)
    if failed:
        raise RuntimeError(f"could not record the block of {task_id}: {failed}")
    C.log(f"blocked {task_id} ({outcome}): {reason}")
    C.ctx().update(outcome="blocked", reason=reason[:400])
    note_for_digest(C.RUNTIME.get("state"), f"{task_id} blocked: {reason[:120]}")
    C.emit("block", reason=reason[:400], outcome=outcome)
    state = C.RUNTIME.get("state")
    if state is not None:
        with C.STATE_LOCK:
            state.setdefault("block_streak", []).append({"task": task_id, "reason": reason[:400]})
            count_day(state, "blocked")
            C.write_driver_state(state)


def requeue(slot, task_id, reason, state, wait_for=()):
    """The task's work could not land (a conflict, a doubled name, a failed re-test after the rebase), or it needed a
    file another running task held. It is undone and the task stays open, so it runs again on the newer tree, after the
    tasks in wait_for have landed; it is not the model's fault, so nothing escalates. After max_requeues of these it
    runs alone (scheduler.solo), where nothing can land meanwhile."""
    SL.revert(slot, f"requeued: {str(reason)[:300]}")
    with C.STATE_LOCK:
        if wait_for:
            state.setdefault("waits", {})[task_id] = sorted(set(wait_for))
        counts = state.setdefault("requeues", {})
        counts[task_id] = counts.get(task_id, 0) + 1
        count_day(state, "requeued")
        C.write_driver_state(state)
    C.log(f"requeued {task_id} ({counts[task_id]} time(s)): {reason}")
    C.ctx().update(outcome="requeued", reason=str(reason)[:400])
    note_for_digest(state, f"{task_id} requeued: {str(reason)[:120]}")
    C.emit("requeue", reason=str(reason)[:400], wait_for=sorted(set(wait_for)), count=counts[task_id])
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
    if day.get("alerted") or finished < C.REQUEUE_ALERT_MIN or day.get("requeued", 0) <= C.REQUEUE_ALERT_SHARE * finished:
        return
    day["alerted"] = True
    C.write_driver_state(state)
    C.post("Many tasks requeued", f"{day['requeued']} of today's {finished} finished tasks could not land and ran "
           f"again. Set parallel_slots lower, or parallel_window smaller, in {C.LOOP_REL}.", "high")


def announce(state, line):
    """Adds a landed task to the phone's digest, and sends the digest once commit_digest tasks have landed since the
    last one."""
    with C.STATE_LOCK:
        digest = state.setdefault("digest", {"landed": [], "other": [], "since": C.now()})
        digest["landed"].append(line)
        C.write_driver_state(state)
        if len(digest["landed"]) >= C.COMMIT_DIGEST:
            send_digest(state)


def note_for_digest(state, line):
    """A block or a requeue, told in the next digest rather than on its own."""
    if state is None:
        return
    with C.STATE_LOCK:
        state.setdefault("digest", {"landed": [], "other": [], "since": C.now()})["other"].append(line)


def send_digest(state):
    """Sends what landed (and what was blocked or requeued) since the last digest, with the day's spending, and starts
    a new one. Nothing is sent when nothing landed or failed."""
    with C.STATE_LOCK:
        digest = state.get("digest") or {}
        landed, other = digest.get("landed") or [], digest.get("other") or []
        if not landed and not other:
            return
        state["digest"] = {"landed": [], "other": [], "since": C.now()}
        C.write_driver_state(state)
    title = f"{len(landed)} task(s) landed" if landed else "No task landed"
    body = "\n".join(landed + other) + f"\nSince {digest.get('since', '?')}. Spent today: ${C.spend_on():.2f}."
    C.post(title, body)


def task_committed(state, task_id=None):
    """A commit ends a run of blocks, and with it the triage that run started. A committed task also counts toward the
    next monitor run (monitor.py)."""
    with C.STATE_LOCK:
        state["block_streak"] = []
        state["triaged"] = False
        if task_id:
            state.setdefault("monitor_pending", []).append(task_id)
            (state.get("requeues") or {}).pop(task_id, None)
            (state.get("waits") or {}).pop(task_id, None)
            (state.get("attempts") or {}).pop(task_id, None)
            (state.get("misplaced") or {}).pop(task_id, None)
            count_day(state, "landed")
        C.write_driver_state(state)


def after_blocks(state):
    """Two blocks in a row queue an Opus triage of what they share. If the blocks go on after the triage, the driver
    pauses, since more attempts would cost money and fail the same way."""
    streak = state.get("block_streak") or []
    if state.get("triaged"):
        if len(streak) >= C.TRIAGE_AFTER_BLOCKS + C.PAUSE_AFTER_TRIAGE_BLOCKS:
            ids = ", ".join(item["task"] for item in streak)
            C.pause(state, "Driver paused: tasks keep failing", f"{len(streak)} tasks in a row were blocked ({ids}), "
                    f"the last ones after an Opus triage. Look at {C.PROGRESS_REL}, then send RESUME.", "urgent")
            task_committed(state)
        return
    if len(streak) >= C.TRIAGE_AFTER_BLOCKS:
        blocked = "\n".join(f"- {item['task']}: {item['reason']}" for item in streak)
        state["triaged"] = True
        state["triage_tasks"] = [item["task"] for item in streak]
        state.setdefault("opus_writes", []).append(prompts.TRIAGE_PROMPT.format(blocked=blocked))
        C.write_driver_state(state)
        C.log(f"{len(streak)} blocks in a row: an Opus triage is queued")
        C.post("Triage queued", f"{len(streak)} tasks in a row were blocked, so Opus looks for what they share before "
               "the next task.")


# ---------------------------------------------------------------------------
# The review packet: what the driver already knows about a stretch of work, handed to an Opus review or a Fable audit
# so it does not have to gather it.
# ---------------------------------------------------------------------------

REVIEW_ID = C.REVIEW_ID
RANGE_AFTER = re.compile(r"starts at the commit after `([^`]+)`")
# how far back the packet looks for the last review's commit
REVIEW_SEARCH = 300


def is_review(task_id):
    return bool(REVIEW_ID.search(task_id))


def subject_commit(task_id):
    """The newest commit whose subject starts with '<task_id>:', or ''."""
    found = C.git("log", "-1", "--format=%H", f"--grep=^{re.escape(task_id)}:", "-E")
    return found.stdout.strip()


def range_start(spec):
    """The commit the review starts after: the one its Build names, else the last review's commit, else 20 back."""
    named = RANGE_AFTER.search(spec)
    if named and subject_commit(named.group(1)):
        return subject_commit(named.group(1)), f"the commit of {named.group(1)}, as the task says"
    for line in C.git("log", f"-{REVIEW_SEARCH}", "--format=%H %s").stdout.splitlines():
        commit, _, subject = line.partition(" ")
        if is_review(subject.split(":", 1)[0]) and ":" in subject:
            return commit, f"the last review's commit ({subject[:60]})"
    return C.git("rev-parse", "HEAD~20").stdout.strip(), "20 commits back (no earlier review found)"


def changed_names(diff):
    """The top-level names (declaration_regex) a diff adds, removes or changes the line of."""
    return {match.group(1) for line in diff.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---"))
            for match in [SL.TOP_NAME.match(line[1:])] if match}


def task_costs(ids):
    """{task: [dollars, sessions, failed sessions, Counter of session kinds]} from usage.jsonl."""
    table = collections.defaultdict(lambda: [0.0, 0, 0, collections.Counter()])
    if not C.USAGE_LOG.exists():
        return table
    for line in C.USAGE_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("task") in ids:
            entry = table[row["task"]]
            entry[0] += row.get("cost_usd") or 0
            entry[1] += 1
            entry[2] += 1 if row.get("is_error") else 0
            entry[3][row.get("kind") or "unknown"] += 1
    return table


def events_lines():
    path = C.LOGS / "events.jsonl"
    return path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []


def task_failures(ids):
    """{task: [why each failed session ended]} from events.jsonl: a session's terminal_reason, else its subtype, else
    'no result'. The reason is read from the session events because usage.jsonl keeps none."""
    table = collections.defaultdict(list)
    for line in events_lines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "session" and event.get("task") in ids and event.get("is_error"):
            reason = event.get("terminal_reason") or event.get("subtype") or "no result"
            table[event["task"]].append(reason)
    return table


def task_events(ids):
    """{task: Counter of requeue, block, escalate, task_end outcomes} from events.jsonl."""
    table = collections.defaultdict(collections.Counter)
    for line in events_lines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("task") in ids and event.get("event") in ("requeue", "block", "escalate", "task_end"):
            table[event["task"]][event["event"] if event["event"] != "task_end" else event.get("outcome")] += 1
    return table


def latest_totals():
    """The newest gate's test totals in events.jsonl, or ''."""
    for line in reversed(events_lines()):
        if '"event": "gate"' in line and '"totals": "' in line:
            event = json.loads(line)
            if event.get("totals"):
                return f"{event['totals']} (gate of {event.get('task')} at {event['time']}, tests {event.get('test_seconds')} s)"
    return ""


def blocked_lines():
    text = read(progress_file())
    match = re.search(r"^## Blocked\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return [line for line in (match.group(1) if match else "").splitlines() if line.startswith("- ")]


def driver_lines():
    """The packet's opening lines: the commit the driver started from and, when the driver's code changed since, that
    the packet was built by older code, whose rules may be out of date."""
    import check_driver  # why: check_driver imports commands, which imports this module
    state = C.read_driver_state()
    start = state.get("start_commit")
    lines = [f"Driver: started from {start[:120]}" if start else "Driver: start commit unknown"]
    stale = check_driver.stale_driver_commits(state)
    if stale:
        named = "; ".join(stale[:3]) + ("; ..." if len(stale) > 3 else "")
        lines.append(f"This packet was built by code older than the driver's at HEAD ({len(stale)} commits since the "
                     f"driver's start: {named}). A rule it reads may be out of date: restart the driver.")
    return lines


def build(task_id, spec):
    """The packet's text, for the review's prompt."""
    base, why = range_start(spec)
    commits = C.git("log", "--reverse", "--format=%h %s", f"{base}..HEAD").stdout.strip().splitlines()
    ids = sorted({line.split(" ", 1)[1].split(":", 1)[0] for line in commits if ":" in line})
    stat = C.git("diff", "--stat=120", f"{base}..HEAD", "--", *C.SCOPE).stdout.strip().splitlines()
    changed = C.git("diff", "--name-only", f"{base}..HEAD", "--", *C.SCOPE).stdout.split()
    functions = collections.defaultdict(set)
    for path in [path for path in changed if C.is_source(path) and not C.is_keyed(path)]:
        names = changed_names(C.git("diff", "-U0", f"{base}..HEAD", "--", path).stdout)
        if names:
            functions[path] |= names
    doubled = SL.duplicates(C.PROJECT, changed)
    costs = task_costs(set(ids))
    events = task_events(set(ids))
    failures = task_failures(set(ids))
    lines = [*driver_lines(), "", f"# Review packet for {task_id}", "",
             f"Range: {base[:10]}..HEAD ({why}), {len(commits)} commits. Read a file's change with "
             f"`git diff {base[:10]} HEAD -- <path>`; open a whole file only where a finding needs it.", "",
             "## Commits", *[f"- {line}" for line in commits[-60:]], "",
             "## What changed in the scope (git diff --stat)", *stat[-80:], "",
             "## Top-level names each source change added or changed",
             *[f"- {path}: {', '.join(sorted(names))}" for path, names in sorted(functions.items())], "",
             "## Names declared twice in one changed file (a merge may have doubled them)",
             *([f"- {item}" for item in doubled] or ["- none"]), "",
             "## Each task in the range: spend and failures",
             "| task | cost | sessions | kinds | failed sessions | failed because | requeued | blocked | escalated |",
             "|---|---|---|---|---|---|---|---|---|"]
    for task in ids:
        cost, sessions, failed, kinds = costs.get(task, [0.0, 0, 0, collections.Counter()])
        counts = events.get(task, collections.Counter())
        kind_text = ", ".join(f"{name} {count}" for name, count in kinds.most_common()) or "-"
        why = ", ".join(failures.get(task, [])) or "-"
        lines.append(f"| {task} | ${cost:.2f} | {sessions} | {kind_text} | {failed} | {why} | {counts['requeue']} | "
                     f"{counts['block']} | {counts['escalate']} |")
    lines += ["", f"## Latest test run: {latest_totals() or 'no gate recorded in events.jsonl yet'}", "",
              f"## Blocked now ({C.PROGRESS_REL})", *(blocked_lines() or ["- none"])]
    first = commits[0].split(" ", 1)[0] if commits else base
    since = C.git("log", "-1", "--format=%cs", first).stdout.strip()
    lines += ["", "## Driver report", f"`report.py --since {since}`, over the whole loop's events, not only this "
              "range's tasks.", "", RP.report_text(since)]
    return "\n".join(lines) + "\n"


def safe_build(task_id, spec):
    """build(), or a note saying why there is no packet: a packet that fails must not stop the review."""
    try:
        return build(task_id, spec)
    except (OSError, ValueError, subprocess.SubprocessError, IndexError) as error:
        return f"(No review packet: {error}. Gather the range yourself, as the review guide says.)\n"
