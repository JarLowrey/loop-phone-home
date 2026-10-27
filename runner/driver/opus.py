"""The phone's Opus features: OPUS: asks a read-only question, answered as a text file; OPUS WRITE: has Opus change
files between tasks, then the driver tests and commits or undoes the change. The triage runs as an OPUS WRITE."""
import json
import subprocess
import threading
import time

import budget as B
import events as E
import loop_common as C
import phone as P
import prompts
import runtime as R
import sessions as X
import settings as S
import slots as SL

OPUS_CHILDREN = set()
OPUS_KILLED = set()   # the calls a shutdown stopped: they report nothing, because the stop was asked for
OPUS_THREADS = []


def ask_opus(question):
    """Starts an Opus question in its own thread, so the driver keeps answering the phone meanwhile."""
    if not question:
        P.post("Opus", "Send OPUS: followed by your question.")
        return
    thread = threading.Thread(target=run_opus, args=(question,), daemon=True)
    OPUS_THREADS.append(thread)
    thread.start()


def run_opus(question):
    """Asks Opus at high effort, read-only, and sends the answer to the phone as a text file."""
    name = f"opus-{time.strftime('%Y%m%d-%H%M%S')}"
    command = [C.CLAUDE, "-p", "--model", S.OPUS_MODEL, "--effort", S.OPUS_EFFORT, "--tools", S.OPUS_TOOLS,
               "--allowedTools", *S.OPUS_TOOLS.split(","), "--disable-slash-commands", "--strict-mcp-config",
               "--output-format", "json", "--max-budget-usd", str(S.OPUS_BUDGET_USD)]
    R.log(f"opus starts ({S.OPUS_MODEL}, {S.OPUS_EFFORT}): {question[:120]!r}")
    started = time.time()
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               cwd=C.PROJECT, env=C.helper_env(), text=True, encoding="utf-8", errors="replace")
    OPUS_CHILDREN.add(process.pid)
    try:
        out, err = process.communicate(prompts.OPUS_PROMPT.format(question=question), timeout=S.OPUS_TIMEOUT)
    except subprocess.TimeoutExpired:
        X.kill_tree(process.pid)
        process.communicate()
        return send_opus_error(question, f"Opus took longer than {S.OPUS_TIMEOUT} seconds")
    finally:
        OPUS_CHILDREN.discard(process.pid)
    if process.pid in OPUS_KILLED:
        R.log("opus stopped by a shutdown; no answer")
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
    body = f"Question:\n{question}\n\nAnswer from {S.OPUS_MODEL}, effort {S.OPUS_EFFORT}, after {seconds} s:\n" \
           f"{str(result['result']).strip()}\n"
    send_opus_answer(name, body)


def send_opus_error(question, reason):
    """Tells the phone an Opus question failed, with the reason."""
    R.log(f"opus failed: {reason}")
    P.post("Opus failed", f"{reason}. Question: {question[:200]}", "high")


def send_opus_answer(name, body, title="Opus answer"):
    """Saves the whole answer in the logs folder, and sends it to the phone as a text file under OPUS_MAX_BYTES."""
    C.LOGS.mkdir(parents=True, exist_ok=True)
    (C.LOGS / f"{name}.txt").write_text(body, encoding="utf-8", newline="\n")
    data = body.encode("utf-8")
    if len(data) > S.OPUS_MAX_BYTES:
        note = f"\n\n[cut short to fit the 4 MB limit; the whole answer is {C.CFG.logs_dir}/{name}.txt]".encode("utf-8")
        data = data[:S.OPUS_MAX_BYTES - len(note)].decode("utf-8", errors="ignore").encode("utf-8") + note
        R.log(f"opus answer cut to {len(data)} bytes for the phone; the whole answer is {name}.txt")
    if not P.send_file(title, f"{name}.txt", data):
        P.post("Opus answer not sent", f"The file could not be sent; it is {C.CFG.logs_dir}/{name}.txt.", "high")


def wait_for_opus():
    """Before the driver ends, waits for the Opus answers still being written, so none is lost."""
    for thread in OPUS_THREADS:
        if thread.is_alive():
            R.log("waiting for an Opus answer before the driver ends")
            thread.join(S.OPUS_TIMEOUT + 120)


def queue_opus_write(state, instruction):
    """Queues an OPUS WRITE instruction. It runs between tasks, never alongside one, so its edits and a task's never mix.
    The queue is kept in driver.json, so a restart does not lose it."""
    if not instruction:
        P.post("Opus", "Send OPUS WRITE: followed by what Opus should change.")
        return
    state.setdefault("opus_writes", []).append(instruction)
    R.write_driver_state(state)
    waiting = " once the running tasks have finished" if state.get("busy") else ""
    P.post("Opus write queued", f"Opus starts on it{waiting}: {instruction[:200]}")


def run_opus_write(state, instruction, slot):
    """OPUS WRITE: Opus, at high effort and with write access, does the instruction in a slot, while no task runs. The
    driver then checks the result as it checks a task: if anything in the scope changed, the tests must pass, or
    every change is undone. What passes lands on the main branch and is pushed, and Opus's summary goes to the phone as
    a text file. It was asked for from the phone, so it needs no approval."""
    name = f"opus-write-{time.strftime('%Y%m%d-%H%M%S')}"
    timer = E.Timer()
    SL.prepare(slot)
    slot.task = name
    R.ctx().update(task=name, slot=slot.name, run=1, outcome="", reason="")
    state["opus_write"] = instruction
    R.write_driver_state(state)
    R.log(f"opus write starts in slot {slot.name}: {instruction[:200]!r}")
    rules_path = slot.root / C.CFG.rules_file
    prompt = prompts.OPUS_WRITE_PROMPT.format(rules=rules_path.read_text(encoding="utf-8") if rules_path.is_file() else "",
                                              instruction=instruction)
    try:
        text, error = X.run_session(prompt, name, S.OPUS_MODEL, S.OPUS_EFFORT, S.OPUS_WRITE_TURNS,
                                    S.OPUS_WRITE_TIMEOUT, B.session_budget(S.OPUS_WRITE_BUDGET_USD), "opus-write", slot)
        if R.RUNTIME.get("stopping"):
            return
        land_opus_write(slot, name, instruction, text, error)
    finally:
        E.emit("opus_write", seconds=timer.seconds(), result=R.ctx().get("reason") or "done",
               instruction=instruction[:200])
        R.ctx().clear()
        SL.release(slot)
        slot.task = None
        state.pop("opus_write", None)
        R.write_driver_state(state)


def land_opus_write(slot, name, instruction, text, error):
    """Checks an OPUS WRITE's changes in its slot, then lands them or undoes them, and tells the phone which."""
    changed = sorted(C.changed_paths(slot.root))
    if error and (B.cut_by_daily_budget(error) or B.never_started("an OPUS WRITE", error)):
        SL.revert(slot)
        P.post("Opus write not done", f"It stopped before it could work; send it again later: {instruction[:200]}")
        return
    summary = text or error
    body = f"Instruction:\n{instruction}\n\nOpus ({S.OPUS_MODEL}, effort {S.OPUS_EFFORT}):\n{summary}\n"
    if not changed:
        send_opus_answer(name, body + "\nNo files changed.\n", "Opus write: no changes")
        return
    if C.code_changed(changed):
        output = X.run_tests(slot.root)
        totals = C.test_failure(output)
        if totals:
            SL.revert(slot)
            send_opus_answer(name, body + f"\nThe driver's test run: {totals or 'no totals printed'}. Every change "
                             "was undone; the copies are in the logs folder, revert-*.\n", "Opus write undone: tests fail")
            return
    message = (f"Opus (phone): {instruction.splitlines()[0][:60]}\n\n{instruction}\n\n{summary[:1500]}\n\n"
               f"Co-Authored-By: {S.WORKER_MODELS['opus']} <noreply@anthropic.com>\n")
    SL.commit_work(slot, message, changed)
    failed = SL.integrate(slot, message)
    if failed:
        held = SL.hold(slot, name)
        SL.revert(slot)
        send_opus_answer(name, body + f"\nIt could not land ({failed}); it is kept on branch {held}.\n",
                         "Opus write not landed")
        return
    send_opus_answer(name, body + f"\nCommitted: {', '.join(changed)}\n", "Opus write committed")
