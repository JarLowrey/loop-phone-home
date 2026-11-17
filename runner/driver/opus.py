"""The phone's Opus features: OPUS: asks a read-only question, answered as a text file; OPUS WRITE: has Opus change
files between tasks, then the driver tests and commits or undoes the change. The triage runs as an OPUS WRITE."""
import json
import subprocess
import threading
import time

import budget as B
import core as C
import prompts
import sessions as X
import slots as SL
import tasks as T

OPUS_CHILDREN = set()
OPUS_KILLED = set()   # the calls a shutdown stopped: they report nothing, because the stop was asked for
OPUS_THREADS = []
# the start of every triage's instruction, which tells a triage from another OPUS WRITE
TRIAGE_START = prompts.TRIAGE_PROMPT.split(":", 1)[0] + ":"


def ask_opus(question):
    """Starts an Opus question in its own thread, so the driver keeps answering the phone meanwhile."""
    if not question:
        C.post("Opus", "Send OPUS: followed by your question.")
        return
    thread = threading.Thread(target=run_opus, args=(question,), daemon=True)
    OPUS_THREADS.append(thread)
    thread.start()


def run_opus(question):
    """Asks Opus at high effort, read-only, and sends the answer to the phone as a text file."""
    name = f"opus-{time.strftime('%Y%m%d-%H%M%S')}"
    command = [C.CLAUDE, "-p", "--model", C.OPUS_MODEL, "--effort", C.OPUS_EFFORT, "--tools", C.OPUS_TOOLS,
               "--allowedTools", *C.OPUS_TOOLS.split(","), "--disable-slash-commands", "--strict-mcp-config",
               "--output-format", "json", "--max-budget-usd", str(C.OPUS_BUDGET_USD)]
    C.log(f"opus starts ({C.OPUS_MODEL}, {C.OPUS_EFFORT}): {question[:120]!r}")
    started = time.time()
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               cwd=C.PROJECT, env=C.helper_env(), text=True, encoding="utf-8", errors="replace")
    OPUS_CHILDREN.add(process.pid)
    try:
        out, err = process.communicate(prompts.OPUS_PROMPT.format(question=question), timeout=C.OPUS_TIMEOUT)
    except subprocess.TimeoutExpired:
        C.kill_tree(process.pid)
        process.communicate()
        return send_opus_error(question, f"Opus took longer than {C.OPUS_TIMEOUT} seconds")
    finally:
        OPUS_CHILDREN.discard(process.pid)
    if process.pid in OPUS_KILLED:
        C.log("opus stopped by a shutdown; no answer")
        return
    seconds = round(time.time() - started)
    try:
        result = json.loads(out)
    except ValueError:
        return send_opus_error(question, f"Claude gave no JSON: {(out or err)[:200]}")
    C.log_usage({"who": "opus", "kind": "phone-question", "is_error": bool(result.get("is_error")),
                 "turns": result.get("num_turns"), "seconds": seconds, "cost_usd": result.get("total_cost_usd"),
                 "usage": result.get("usage")})
    if result.get("is_error") or not result.get("result"):
        return send_opus_error(question, f"Claude reported an error: {str(result.get('result'))[:200]}")
    body = f"Question:\n{question}\n\nAnswer from {C.OPUS_MODEL}, effort {C.OPUS_EFFORT}, after {seconds} s:\n" \
           f"{str(result['result']).strip()}\n"
    send_opus_answer(name, body)


def send_opus_error(question, reason):
    """Tells the phone an Opus question failed, with the reason."""
    C.log(f"opus failed: {reason}")
    C.post("Opus failed", f"{reason}. Question: {question[:200]}", "high")


def send_opus_answer(name, body, title="Opus answer"):
    """Saves the whole answer in the logs folder, and sends it to the phone as a text file under OPUS_MAX_BYTES."""
    C.LOGS.mkdir(parents=True, exist_ok=True)
    (C.LOGS / f"{name}.txt").write_text(body, encoding="utf-8", newline="\n")
    data = body.encode("utf-8")
    if len(data) > C.OPUS_MAX_BYTES:
        note = f"\n\n[cut short to fit the 4 MB limit; the whole answer is {C.LOGS_REL}{name}.txt]".encode("utf-8")
        data = data[:C.OPUS_MAX_BYTES - len(note)].decode("utf-8", errors="ignore").encode("utf-8") + note
        C.log(f"opus answer cut to {len(data)} bytes for the phone; the whole answer is {name}.txt")
    if not C.send_file(title, f"{name}.txt", data):
        C.post("Opus answer not sent", f"The file could not be sent; it is {C.LOGS_REL}{name}.txt.", "high")


def wait_for_opus():
    """Before the driver ends, waits for the Opus answers still being written, so none is lost."""
    for thread in OPUS_THREADS:
        if thread.is_alive():
            C.log("waiting for an Opus answer before the driver ends")
            thread.join(C.OPUS_TIMEOUT + 120)


def queue_opus_write(state, instruction):
    """Queues an OPUS WRITE instruction. It runs between tasks, never alongside one, so its edits and a task's never mix.
    The queue is kept in driver.json, so a restart does not lose it."""
    if not instruction:
        C.post("Opus", "Send OPUS WRITE: followed by what Opus should change.")
        return
    state.setdefault("opus_writes", []).append(instruction)
    C.write_driver_state(state)
    waiting = " once the running tasks have finished" if state.get("busy") else ""
    C.post("Opus write queued", f"Opus starts on it{waiting}: {instruction[:200]}")


def run_opus_write(state, instruction, slot):
    """OPUS WRITE: Opus, at high effort and with write access, does the instruction in a slot, while no task runs. The
    driver then checks the result as it checks a task: if anything in the scope changed, the tests must pass, or
    every change is undone. What passes lands on the main branch and is pushed, and Opus's summary goes to the phone as
    a text file. It was asked for from the phone, so it needs no approval."""
    name = f"opus-write-{time.strftime('%Y%m%d-%H%M%S')}"
    timer = C.Timer()
    SL.prepare(slot)
    slot.task = name
    C.ctx().update(task=name, slot=slot.name, run=1, outcome="", reason="")
    state["opus_write"] = instruction
    C.write_driver_state(state)
    C.log(f"opus write starts in slot {slot.name}: {instruction[:200]!r}")
    prompt = prompts.OPUS_WRITE_PROMPT.format(rules=C.rules_text(slot.root), instruction=instruction)
    try:
        text, error = X.run_session(prompt, name, C.OPUS_MODEL, C.OPUS_EFFORT, C.OPUS_WRITE_TURNS,
                                    C.OPUS_WRITE_TIMEOUT, B.session_budget(C.OPUS_WRITE_BUDGET_USD), "opus-write", slot)
        if C.RUNTIME.get("stopping"):
            return
        land_opus_write(slot, name, instruction, text, error)
    finally:
        C.emit("opus_write", seconds=timer.seconds(), result=C.ctx().get("reason") or "done",
               instruction=instruction[:200])
        C.ctx().clear()
        SL.release(slot)
        slot.task = None
        state.pop("opus_write", None)
        C.write_driver_state(state)


def land_opus_write(slot, name, instruction, text, error):
    """Checks an OPUS WRITE's changes in its slot, then lands them or undoes them, and tells the phone which."""
    changed = sorted(C.changed_paths(slot.root))
    if error and (B.cut_by_daily_budget(error) or B.never_started("an OPUS WRITE", error)):
        SL.revert(slot)
        C.post("Opus write not done", f"It stopped before it could work; send it again later: {instruction[:200]}")
        return
    summary = text or error
    body = f"Instruction:\n{instruction}\n\nOpus ({C.OPUS_MODEL}, effort {C.OPUS_EFFORT}):\n{summary}\n"
    if not changed:
        send_opus_answer(name, body + "\nNo files changed.\n", "Opus write: no changes")
        return
    if C.code_changed(changed):
        failure = C.test_failure(X.run_tests(slot.root))
        if failure:
            SL.revert(slot)
            send_opus_answer(name, body + f"\nThe driver's test run: {failure}. Every change was undone; the copies "
                             f"are in {C.LOGS_REL}revert-*.\n", "Opus write undone: tests fail")
            return
    message = (f"Opus (phone): {instruction.splitlines()[0][:60]}\n\n{instruction}\n\n{summary[:1500]}\n\n"
               f"Co-Authored-By: {C.WORKER_MODELS['opus']} <noreply@anthropic.com>\n")
    SL.commit_work(slot, message, changed)
    state = C.RUNTIME.get("state") or {}
    triage = (instruction.startswith(TRIAGE_START) and state.get("triage_tasks")) or []
    reopened = []
    failed = SL.integrate(slot, message, T.reopen_marks(triage, reopened) if triage else None)
    if failed:
        held = SL.hold(slot, name)
        SL.revert(slot)
        send_opus_answer(name, body + f"\nIt could not land ({failed}); it is kept on branch {held}.\n",
                         "Opus write not landed")
        return
    if triage:
        state.pop("triage_tasks", None)
        T.forget_attempts(state, triage)
        if reopened:
            C.log(f"the triage landed: re-opened {', '.join(reopened)}")
            body += f"\nRe-opened, to be tried on the fixed code: {', '.join(reopened)}\n"
    if C.driver_changed(changed):
        body += "\nIt changed the driver's code, so the driver restarts on the new code before the next task.\n"
    send_opus_answer(name, body + f"\nCommitted: {', '.join(changed)}\n", "Opus write committed")
