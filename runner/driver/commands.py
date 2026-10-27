"""The owner's commands, from the phone and from the machine: who does the tasks, approvals, status, pause and resume,
and the ways to stop the driver."""
import os
import secrets
import time

import budget as B
import phone as P
import prompts
import runtime as R
import sessions as X
import settings as S
import tasks as T
import worker_task as W

C = W.C
COMMANDS = ("STATUS", "MAN", "STOP", "PAUSE", "RESUME", "RESTART", "WIND DOWN", "SHUTDOWN NOW")


def task_model(state):
    """(model, effort) that do the tasks. effort is None for the model's own default."""
    return state.get("task_model") or C.task_model, state.get("task_effort")


def model_text(state):
    model, effort = task_model(state)
    return f"Tasks are done by {model}, effort {effort or 'default'}."


def needs_approval(model, effort):
    """True for an automated call that must wait for the phone's Approve (with approvals on): Fable at any effort, and
    Opus at medium effort or more. The model's default effort counts as more than medium."""
    return model == "fable" or (model == "opus" and effort != "low")


def set_model(state, words):
    """MODEL <model> [effort] or EFFORT <level>, as upper-case words. Returns the reply for the phone."""
    if words[0] == "EFFORT":
        if len(words) < 2:
            return f"Say EFFORT and a level: {', '.join(S.EFFORTS)} or default. " + model_text(state)
        words = ["MODEL", task_model(state)[0].upper(), *words[1:]]
    if len(words) == 1:
        return model_text(state)
    model = words[1].lower()
    effort = words[2].lower() if len(words) > 2 else (state.get("task_effort") or "default")
    if model not in S.MODEL_NAMES or effort not in S.EFFORTS + ("default",) or len(words) > 3:
        return (f"Not changed. Models: {', '.join(S.MODEL_NAMES)}. Efforts: {', '.join(S.EFFORTS)}, default. "
                + model_text(state))
    state["task_model"] = model
    state["task_effort"] = None if effort == "default" else effort
    R.write_state(state)
    R.log(f"task model set: {model}, effort {effort}")
    asks = S.get("approvals") and needs_approval(model, state["task_effort"])
    return "From the next task on. " + model_text(state) + (" Each task asks for your approval first." if asks else "")


def answer_approval(state, words, from_machine):
    """APPROVE <code> or DENY <code>. From the machine, with no code, it answers every waiting request."""
    answer = "approve" if words[0] == "APPROVE" else "deny"
    waiting = state.get("approvals") or {}
    codes = [words[1].lower()] if len(words) > 1 else (list(waiting) if from_machine else [])
    hits = [code for code in codes if code in waiting and not waiting[code].get("answer")]
    for code in hits:
        waiting[code]["answer"] = answer
    R.write_state(state)
    if not hits:
        return f"No approval request is waiting for {' '.join(words[1:]) or 'an answer'}."
    return f"{answer.capitalize()}d: " + "; ".join(waiting[code]["what"][:120] for code in hits)


def approved(state, what):
    """Asks the phone to approve an automated Opus or Fable call, and waits for the answer. True when it is approved. No
    answer within approval_hours counts as a denial, and so does a shutdown or WIND DOWN while waiting. With approvals
    off in the driver-settings block, nothing is asked and the call goes ahead."""
    if not S.get("approvals"):
        R.log(f"approvals off, so not asked: {what}")
        return True
    code = secrets.token_hex(3)
    hours = S.get("approval_hours")
    with R.STATE_LOCK:
        state.setdefault("approvals", {})[code] = {"what": what, "asked": R.now(), "answer": None}
        R.write_state(state)
    R.log(f"approval {code} asked: {what}")
    P.post("Approve this call?", f"{what}. Approve or deny (code {code}). No answer in {hours:g} h is a denial.",
           "high", f"{P.button('Approve', 'APPROVE ' + code)}; {P.button('Deny', 'DENY ' + code)}")
    deadline = time.time() + hours * 3600
    answer = None
    while time.time() < deadline and not R.RUNTIME.get("stopping"):
        handle_commands(state)
        answer = state["approvals"][code].get("answer")
        if answer or state.get("wind_down"):
            break
        time.sleep(S.APPROVAL_POLL_SECONDS)
    with R.STATE_LOCK:
        state["approvals"].pop(code, None)
        R.write_state(state)
    R.log(f"approval {code}: {answer or 'no answer, so denied'}")
    return answer == "approve"


def status_text(state):
    """One line for the phone: whether the loop runs, what the current task is doing, who does the tasks, today's spend."""
    paused = "paused" if state.get("paused") else "running"
    if not state.get("busy"):
        text = f"Loop {paused}. No task is running."
    else:
        text = f"Loop {paused}. Task {state.get('task')} ({state.get('mode')}), started {state.get('task_started')}."
        if not state.get("solo"):
            run = W.read_status()
            if run.get("task") == state.get("task"):
                text += f" Attempt {run.get('attempt')}: {run.get('phase_detail') or run.get('state', '')}."
    waiting = len(state.get("approvals") or {})
    text += f" {model_text(state)} Spent today: ${B.spend_on():.2f} of ${S.get('daily_budget_usd'):g}."
    return text + (f" Waiting for {waiting} approval(s)." if waiting else "")


def machine_requests():
    """The commands --model, --approve and --deny left in the request file, as (id, text) like the phone's, with an
    empty id. The file is emptied."""
    path = R.PATHS["request"]
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    path.unlink(missing_ok=True)
    return [("", line) for line in lines if line.strip()]


def handle_commands(state):
    """Reads the phone's inbox and the machine's request file, and does each command in them. Each message is read
    once. A message that is not a command is ignored; a short one is logged, in case it was a typo, and a long one (such
    as the driver's own replies) is not."""
    with R.STATE_LOCK:
        seen = W.read_seen()
        for message_id, text in machine_requests() + W.poll_inbox():
            if message_id:
                if message_id in seen:
                    continue
                W.mark_seen(message_id)
                seen.add(message_id)
            words = text.upper().split()
            command = " ".join(words)
            if words and words[0] in ("MODEL", "EFFORT"):
                R.log(f"command {command}")
                P.post("Loop task model", set_model(state, words))
                continue
            if words and words[0] in ("APPROVE", "DENY"):
                R.log(f"command {command}")
                P.post("Approval", answer_approval(state, words, not message_id))
                continue
            if command not in COMMANDS:
                if len(text) <= 40:
                    R.log(f"inbox message is not a command, ignored: {text!r}")
                continue
            R.log(f"command {command}")
            if command == "STATUS":
                P.post("Loop status", status_text(state))
            elif command == "MAN":
                P.post("Commands", prompts.MAN_TEXT)
            elif command == "WIND DOWN":
                state["wind_down"] = True
                R.write_state(state)
                P.post("Loop will exit", "The driver ends once the current task is committed or undone.")
            elif command == "SHUTDOWN NOW":
                P.post("Loop shutting down", "Killing the running session and ending the driver now.", "high")
                shut_down(state, "SHUTDOWN NOW from the phone")
                raise SystemExit(0)
            elif command in ("STOP", "PAUSE"):
                state["paused"] = True
                R.write_state(state)
                P.post("Loop paused", "No new task starts until you send RESUME.")
            else:
                state.update({"paused": False, "resume_at": None})
                R.write_state(state)
                P.post("Loop resumed", "Carrying on with the next open task.")


X.HANDLE_COMMANDS = handle_commands


def shut_down(state, reason):
    """Kills the running session, undoes the unfinished task, and marks the driver paused and ended by its owner. The
    stopping flag is set first, so the thread that was waiting on the session does not also undo or mark the task."""
    R.RUNTIME["stopping"] = True
    W.kill_tree(R.CHILD["pid"])
    R.CHILD["pid"] = None
    if state.get("busy"):
        T.revert_unfinished()
    state.update({"busy": False, "paused": True, "ended_by": "owner"})
    R.write_state(state)
    R.log(f"shut down: {reason}")


def running_driver():
    """The pid of another driver or supervisor that is still running from this folder, or 0."""
    for key in ("supervisor", "pid"):
        other = R.read_pid(R.PATHS[key])
        if other and other != os.getpid() and W.pid_alive(other):
            return other
    return 0


def cli_shutdown():
    """--shutdown: stops the supervisor, the driver and its session, from the machine they run on. The supervisor goes
    first, so it does not start again the driver it sees end."""
    state = R.read_state()
    for pid in (R.read_pid(R.PATHS["supervisor"]), R.read_pid(R.PATHS["pid"]), state.get("child_pid")):
        if pid and W.pid_alive(pid):
            W.kill_tree(pid)
    if state.get("busy"):
        T.revert_unfinished(state)
    state.update({"busy": False, "paused": True, "child_pid": None, "approvals": {}, "ended_by": "owner"})
    R.write_state(state)
    R.PATHS["supervisor"].unlink(missing_ok=True)
    R.PATHS["pid"].unlink(missing_ok=True)
    R.log("shut down from the machine")
    print("driver shut down; nothing is running from it. Start it again with python <runner>/driver")


def queue_request(text):
    """--model, --approve, --deny: leaves the command for the driver, which reads it on its next poll or start."""
    with open(R.PATHS["request"], "a", encoding="utf-8", newline="\n") as requests:
        requests.write(text + "\n")
    print(f"queued {text!r}: the driver applies it on its next poll, or when it next starts. "
          "The reply goes to the phone and driver.log.")
