"""The owner's commands, from the phone and from the PC: who does the tasks, approvals, status, pause and resume, and the
ways to stop the driver."""
import os
import pathlib
import secrets
import threading
import time

import loop_common as C
import opus as O
import phone as P
import prompts
import runtime as R
import sessions as X
import settings as S
import slots as SL
import workspace as W

COMMANDS = ("STATUS", "MAN", "STOP", "PAUSE", "RESUME", "RESTART", "WIND DOWN", "SHUTDOWN NOW")


def worker(state):
    """(model, effort) that do the tasks. effort is None for the model's own default."""
    return state.get("worker_model") or S.DEFAULT_WORKER, state.get("worker_effort")


def worker_text(state):
    model, effort = worker(state)
    return f"Tasks are done by {model}, effort {effort or 'default'}."


def set_worker(state, words):
    """MODEL <model> [effort] or EFFORT <level>, as upper-case words. Returns the reply for the phone."""
    if words[0] == "EFFORT":
        if len(words) < 2:
            return f"Say EFFORT and a level: {', '.join(S.WORKER_EFFORTS)} or default. " + worker_text(state)
        words = ["MODEL", worker(state)[0].upper(), *words[1:]]
    if len(words) == 1:
        return worker_text(state)
    model = words[1].lower()
    effort = words[2].lower() if len(words) > 2 else (state.get("worker_effort") or "default")
    if model not in S.WORKER_MODELS or effort not in S.WORKER_EFFORTS + ("default",) or len(words) > 3:
        return (f"Not changed. Models: {', '.join(S.WORKER_MODELS)}. Efforts: {', '.join(S.WORKER_EFFORTS)}, "
                "default. " + worker_text(state))
    state["worker_model"] = model
    state["worker_effort"] = None if effort == "default" else effort
    R.write_driver_state(state)
    R.log(f"worker set: {model}, effort {effort}")
    note = (" Each task asks for your approval first." if S.APPROVALS_ON and needs_approval(model, state["worker_effort"])
            else "")
    return "From the next task on. " + worker_text(state) + note


def needs_approval(model, effort):
    """True for an automated call that must wait for the phone's Approve: Fable at any effort, and Opus at medium
    effort or more. The model's default effort counts as more than medium."""
    return model == "fable" or (model == "opus" and effort != "low")


def answer_approval(state, words, trusted_pc):
    """APPROVE <code> or DENY <code>. From the PC, with no code, it answers every waiting request. Returns the reply."""
    answer = "approve" if words[0] == "APPROVE" else "deny"
    waiting = state.get("approvals") or {}
    codes = [words[1].lower()] if len(words) > 1 else (list(waiting) if trusted_pc else [])
    hits = [code for code in codes if code in waiting and not waiting[code].get("answer")]
    for code in hits:
        waiting[code]["answer"] = answer
    R.write_driver_state(state)
    if not hits:
        return f"No approval request is waiting for {' '.join(words[1:]) or 'an answer'}."
    return f"{answer.capitalize()}d: " + "; ".join(waiting[code]["what"][:120] for code in hits)


def approved(state, what):
    """Asks the phone to approve an automated Opus or Fable call, and waits for the answer. True when it is approved. No
    answer within approval_hours counts as a denial, and so does a shutdown or WIND DOWN while waiting. With approvals
    off in the loop file, nothing is asked and the call goes ahead."""
    if not S.APPROVALS_ON:
        R.log(f"approvals off, so not asked: {what}")
        return True
    code = secrets.token_hex(3)
    with R.STATE_LOCK:
        state.setdefault("approvals", {})[code] = {"what": what, "asked": R.now(), "answer": None}
        R.write_driver_state(state)
    R.log(f"approval {code} asked: {what}")
    P.post("Approve this call?", f"{what}. Approve or deny (code {code}). No answer in {S.APPROVAL_HOURS:g} h is a "
           "denial.", "high", f"{P.button('Approve', 'APPROVE ' + code)}; {P.button('Deny', 'DENY ' + code)}")
    asked = time.time()
    deadline = asked + S.APPROVAL_HOURS * 3600
    answer = None
    while time.time() < deadline and not R.RUNTIME.get("stopping"):
        handle_commands(state)
        answer = state["approvals"][code].get("answer")
        if answer or state.get("exit_after"):
            break
        time.sleep(S.APPROVAL_POLL_SECONDS)
    with R.STATE_LOCK:
        state["approvals"].pop(code, None)
        R.write_driver_state(state)
    R.log(f"approval {code}: {answer or 'no answer, so denied'}")
    if R.EMIT:
        R.EMIT("approval", what=what[:200], seconds=round(time.time() - asked, 1), answer=answer or "none")
    return answer == "approve"


def status_text(state):
    loop_state = "paused" if state.get("paused") else "running"
    slots = state.get("slots") or []
    task = "; ".join(f"slot {item['slot']}: {item['task']} since {item.get('started', '?')}" for item in slots) or "none"
    waiting = len(state.get("approvals") or {})
    text = (f"Loop {loop_state}, {S.PARALLEL_SLOTS} slot(s). Running: {task}. {worker_text(state)} Spent today: "
            f"${C.spend_on():.2f} of ${S.DAILY_BUDGET_USD:g}.")
    return text + (f" Waiting for {waiting} approval(s)." if waiting else "")


def pc_requests():
    """The commands --model, --approve and --deny left in REQUEST_FILE, as (id, text) like the phone's, with an empty
    id. The file is emptied."""
    if not R.REQUEST_FILE.exists():
        return []
    lines = R.REQUEST_FILE.read_text(encoding="utf-8").splitlines()
    R.REQUEST_FILE.unlink(missing_ok=True)
    return [("", line) for line in lines if line.strip()]


def poll_messages():
    return P.poll_messages(S.INBOX_WINDOW)


def handle_commands(state):
    """Answers the phone's commands, and the ones the PC left in REQUEST_FILE. Every other message is only logged."""
    with R.STATE_LOCK:
        for message_id, text in pc_requests() + poll_messages():
            if message_id:
                P.mark_seen(message_id)
            text = text.strip()
            words = text.upper().split()
            if not words:
                continue
            command = " ".join(words)
            if command.startswith("OPUS WRITE:"):
                O.queue_opus_write(state, text[len("OPUS WRITE:"):].strip())
                continue
            if command.startswith("OPUS:"):
                O.ask_opus(text[len("OPUS:"):].strip())
                continue
            if words[0] in ("MODEL", "EFFORT"):
                R.log(f"command {command}")
                P.post("Loop worker", set_worker(state, words))
                continue
            if words[0] in ("APPROVE", "DENY"):
                R.log(f"command {command}")
                P.post("Approval", answer_approval(state, words, not message_id))
                continue
            if command not in COMMANDS:
                R.log(f"inbox message ignored: {text[:80]!r}")
                continue
            R.log(f"command {command}")
            if command == "STATUS":
                P.post("Loop status", status_text(state))
            elif command == "MAN":
                P.post("Commands", prompts.MAN_TEXT)
            elif command == "SHUTDOWN NOW":
                P.post("Loop shutting down", "Stopping the running sessions and the driver now.")
                shut_down(state, "SHUTDOWN NOW from the phone")
                if threading.current_thread() is not threading.main_thread():
                    # why: a slot's thread waiting on an approval read it; SystemExit there would end only that thread
                    R.DRIVER_PID.unlink(missing_ok=True)
                    os._exit(0)
                raise SystemExit(0)
            elif command == "WIND DOWN":
                state["exit_after"] = True
                R.write_driver_state(state)
                P.post("Loop will exit", "The driver ends once the running tasks are landed or undone.")
            elif command in ("STOP", "PAUSE"):
                state["paused"] = True
                R.write_driver_state(state)
                P.post("Loop paused", "No new task starts until you send RESUME.")
            else:
                state["paused"] = False
                state["resume_at"] = None
                R.write_driver_state(state)
                P.post("Loop resumed", "Carrying on with the next open task.")
                if R.EMIT:
                    R.EMIT("resume", reason=f"{command} from the owner")


X.HANDLE_COMMANDS = handle_commands


def shut_down(state, reason):
    """Kills every running session and its children, reverts the unfinished tasks in their slots, and leaves the driver
    paused. The stopping flag is set first, so the threads waiting on the killed sessions do not also block the tasks."""
    R.RUNTIME["stopping"] = True
    for pid in list(R.CHILDREN.values()):
        X.kill_tree(pid)
    R.CHILDREN.clear()
    for pid in list(O.OPUS_CHILDREN):
        O.OPUS_KILLED.add(pid)
        X.kill_tree(pid)
    scheduler = R.RUNTIME.get("scheduler")
    for slot in scheduler.busy() if scheduler else []:
        SL.revert(slot)
    state["slots"] = []
    state["task"] = None
    state["busy"] = False
    state["paused"] = True
    state["ended_by"] = "owner"
    R.write_driver_state(state)
    R.log(f"shut down: {reason}")
    if R.EMIT:
        R.EMIT("driver_stop", reason=reason)


def cli_shutdown():
    """python <runner>/driver --shutdown: stops the supervisor and the driver from the PC. The supervisor goes first, so it
    does not restart the driver it sees end."""
    S.refresh()
    state = R.read_driver_state()
    X.kill_tree(R.read_pid(R.SUPERVISOR_PID))
    X.kill_tree(R.read_pid(R.DRIVER_PID))
    for pid in state.get("child_pids") or [state.get("child_pid")]:
        X.kill_tree(pid)
    for item in state.get("slots") or []:
        root = pathlib.Path(item["root"])
        if (root / ".git").exists():
            W.undo_changes(sorted(C.changed_paths(root)), C.LOGS, root)
    state["ended_by"] = "owner"
    state["busy"] = False
    state["paused"] = True
    state["slots"] = []
    state["task"] = None
    state["child_pids"] = []
    state["approvals"] = {}
    R.write_driver_state(state)
    R.SUPERVISOR_PID.unlink(missing_ok=True)
    R.DRIVER_PID.unlink(missing_ok=True)
    R.log("shut down from the PC")
    print("driver shut down; nothing is running from it. Start it again with python <runner>/driver")


def queue_request(text):
    """--model, --approve, --deny: leaves the command for the driver, which reads it on its next poll or start."""
    R.REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(R.REQUEST_FILE, "a", encoding="utf-8", newline="\n") as requests:
        requests.write(text + "\n")
    print(f"queued {text!r}: the driver applies it on its next poll, or when it next starts. "
          "The reply goes to the phone and driver.log in the logs folder.")
