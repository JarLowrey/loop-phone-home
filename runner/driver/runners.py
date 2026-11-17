"""Running one task in its slot: the session that does it, the gate its work must pass, and the landing, retry, requeue
or block that follows. RUNNERS lists the kinds of task; a new kind is one more row."""
import contextlib
import datetime
import hmac
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback

import budget as B
import commands as K
import leases as L
import core as C
import prompts
import sessions as X
import slots as SL
import task_hook
import tasks as T

# one HEAD check at a time: two slots whose gates time out together share the baseline slot
BASELINE_LOCK = threading.Lock()


class Requeue(str):
    """Why a task's work could not land: a conflict with work that landed meanwhile, or a file another running task
    held (wait_for names those tasks; the task waits until they land). The task goes back to the queue; the model is
    not at fault, so it does not escalate."""

    def __new__(cls, reason, wait_for=()):
        made = super().__new__(cls, reason)
        made.wait_for = list(wait_for)
        return made


def check_passed(slot, spec, opus=False):
    """What a commit needs, checked here in the task's slot and not taken from the session's word: nothing changed
    outside the scope, no loop-folder file the task does not name, no test removed or making fewer asserts unless the
    task names it, every companion file in place, the config's gate_checks pass, the test run passes now, and the task's
    Done when line holds. The tests run once, or not at all when the session's own full run left a receipt for this very
    tree; an Opus task that changed nothing in the scope skips them. Returns (reason to block or '', the totals line).
    The gate's time, its test run's time and output, and its verdict go to events.jsonl; the output is kept in
    <logs>/<task>.gate.<time>.log."""
    timer = C.Timer()
    found = {"test_seconds": 0.0, "output": ""}
    reason, totals = gate(slot, spec, opus, found)
    log = ""
    if found["output"]:
        log = f"{C.ctx().get('task', 'task')}.gate.{C.stamp()}.log"
        (C.LOGS / log).write_text(found["output"] + "\n", encoding="utf-8", newline="\n")
    load = C.suite_load(found["output"])
    C.emit("gate", seconds=timer.seconds(), test_seconds=found["test_seconds"], totals=totals, reason=reason, log=log,
           suite_seconds=load[0] if load else None, suite_limit=load[1] if load else None,
           receipt=found.get("receipt", False))
    C.ctx()["gate_output"] = found["output"]
    warn_suite_load(load)
    return reason, totals


def warn_suite_load(load):
    """Tells the phone once a day when the test run took more than SUITE_WARN_SHARE of its time limit (timing_regex),
    before a slow suite starts failing tasks that did nothing wrong."""
    state = C.RUNTIME.get("state")
    if not load or state is None or load[0] < C.SUITE_WARN_SHARE * load[1]:
        return
    today = datetime.date.today().isoformat()
    with C.STATE_LOCK:
        if state.get("suite_warned") == today:
            return
        state["suite_warned"] = today
        C.write_driver_state(state)
    C.log(f"test suite near its limit: {load[0]:g} s of {load[1]} s")
    C.post("Test suite near its limit", f"The test run took {load[0]:g} s of its {load[1]} s limit "
           f"({C.ctx().get('task', 'a gate')}). Past the limit every task fails its gate. Make the slow tests cheaper, "
           "or raise the limit. The slowest tests are in the gate's log.", "high")


def suite_fault(slot, task_id):
    """After a gate whose tests ran out of time: the same tests on HEAD, with no task's changes, in the baseline slot.
    If HEAD's own suite times out or fails, the fault is not the task's: its work is undone, it stays open, and the
    driver pauses with an alert; returns None. Otherwise returns a note for the task's failure reason."""
    with BASELINE_LOCK:
        baseline = SL.Slot("baseline")
        SL.prepare(baseline)
        C.log(f"{task_id}: the gate's tests ran out of time; running HEAD's own suite ({baseline.base[:10]})")
        output = X.run_tests(baseline.root)
    totals, load = C.totals_line(output), C.suite_load(output)
    C.emit("baseline", head=baseline.base[:10], totals=totals, timed_out=C.timed_out(output),
           suite_seconds=load[0] if load else None)
    if C.timed_out(output) or C.test_failure(output):
        SL.revert(slot, "HEAD's own test suite fails or runs out of time")
        C.pause(C.RUNTIME["state"], "Driver paused: the test suite fails on HEAD", f"{task_id}'s tests ran out of time, "
                f"and so do HEAD's ({baseline.base[:10]}) with no task's changes: "
                f"{(output.splitlines() or ['no output'])[0][:200]}. {task_id} stays open. Make the slow tests cheaper "
                "(the gate log names where it stopped), then send RESUME.", "urgent")
        return None
    took = f", {load[0]:g} s of {load[1]} s" if load else ""
    return (f" (HEAD's own suite passes: {totals}{took}, so this change made a test slow or hang; the timeout line "
            f"names where)")


def record_attempt(task_id, model, outcome):
    """Keeps what a session said at the end, with how its attempt ended, for the task's next attempt (ATTEMPTS_NOTE):
    the last MAX_ATTEMPTS_KEPT of them, until the task lands."""
    state = C.RUNTIME.get("state")
    if state is None:
        return
    words = (C.ctx().get("last_words") or "").strip() or "(nothing)"
    with C.STATE_LOCK:
        kept = state.setdefault("attempts", {}).setdefault(task_id, [])
        kept.append({"when": C.now(), "model": model, "outcome": str(outcome)[:600], "words": words[:3000]})
        del kept[:-C.MAX_ATTEMPTS_KEPT]
        C.write_driver_state(state)


def attempts_note(task_id):
    """The earlier attempts at the task, for its prompt, or ''."""
    kept = ((C.RUNTIME.get("state") or {}).get("attempts") or {}).get(task_id) or []
    if not kept:
        return ""
    lines = []
    for item in kept:
        words = "\n".join("  " + line for line in item["words"].splitlines())
        lines.append(f"- {item['model']}, {item['when']}: {item['outcome']}\n  What it said at the end:\n{words}")
    return prompts.ATTEMPTS_NOTE.format(attempts="\n".join(lines))


def done_when_note(spec):
    """The task's Done when items, one `test <name>`, `file <path>` or `output <text>` line each, for the end of its
    prompt, or '' when it has none: a Build that names its test after a long list is otherwise stopped short of it."""
    items = SL.done_when(spec)
    if not items:
        return ""
    return prompts.DONE_WHEN_NOTE.format(items="\n".join(f"{kind} {value}" for kind, value in items))


def gate(slot, spec, opus, found):
    """check_passed's checks, in order; found gets the test run's time and output."""
    snap, root = slot.snap(), slot.root
    extra = SL.outside_scope(snap, C.SCOPE, root, SL.task_files(spec, root))
    if extra:
        return f"changed outside the scope: {', '.join(extra[:3])}", ""
    stray = SL.loop_changes(snap, SL.allowed_loop_files(spec, opus), root)
    if stray:
        return f"changed {C.LOOP_DIR} files the task does not name: {', '.join(stray[:3])}", ""
    removed = SL.removed_tests(snap, root, spec)
    if removed:
        return f"test functions were removed: {', '.join(removed[:5])}", ""
    loosened = SL.weakened_tests(snap, spec, root)
    if loosened:
        return f"tests the task does not name make fewer asserts: {', '.join(loosened[:5])}", ""
    changes = SL.task_changes(snap, root)
    present = [path for path in changes if (root / path).exists()]
    bare = missing_companions(present, lambda path: (root / path).exists())
    if bare:
        return f"files without their companion: {', '.join(bare[:5])}", ""
    orphans = [path for path in orphan_companions(changes, lambda path: (root / path).exists())
               if (root / path).exists()]
    if orphans:
        return f"companion files without their file: {', '.join(orphans[:5])}", ""
    for check in C.CFG.gate_checks or []:
        failed = gate_check(root, check, present)
        if failed:
            return failed, ""
    output, totals = "", ""
    if not opus or C.code_changed(changes):
        tests = C.Timer()
        output = trusted_receipt(root, slot.name)
        if output:
            C.log("the session's own full run covers this tree (receipt); the gate skips its test run")
            found["receipt"] = True
        else:
            output = locked_test_run(root)
        found.update(test_seconds=tests.seconds(), output=output)
        totals = C.totals_line(output)
        failure = C.test_failure(output)
        if failure:
            return named_reason(f"the driver's own test run: {failure}", C.failing_tests(output)), totals
    return SL.check_done_when(spec, output, root), totals


def missing_companions(paths, exists):
    """The files among paths, in the scope, whose companion (companion_files: x.gd needs x.gd.uid) is neither among
    them nor on disk; exists is called with the companion's path."""
    given = set(paths)
    found = []
    for path in paths:
        if not C.code_changed([path]):
            continue
        for suffix, companion in (C.CFG.companion_files or {}).items():
            if path.endswith(suffix) and path + companion not in given and not exists(path + companion):
                found.append(path)
    return found


def orphan_companions(paths, exists):
    """The companion files among the task's changes (deleted ones included) whose file is gone: one whose file is
    neither on disk nor among paths, and the companion of a deleted file that is still on disk. A companion the task
    itself deleted is listed too, so the gate drops the ones no longer on disk."""
    given = set(paths)
    found = []
    for path in paths:
        if not C.code_changed([path]):
            continue
        for suffix, companion in (C.CFG.companion_files or {}).items():
            if path.endswith(suffix + companion):
                owner = path[:-len(companion)]
                if owner not in given and not exists(owner):
                    found.append(path)
            elif path.endswith(suffix) and not exists(path) and exists(path + companion):
                found.append(path + companion)
    return found


def check_command(check, paths):
    """A gate check's command, with a leading python as this interpreter, and the matching paths after it when the
    check takes them (pass_paths)."""
    command = check.get("command") or []
    if isinstance(command, str):
        quote = subprocess.list2cmdline if os.name == "nt" else (lambda words: shlex.quote(words[0]))
        return command + ("".join(f" {quote([path])}" for path in paths) if check.get("pass_paths") else "")
    command = list(command)
    if command and command[0] in ("python", "python3", "py"):
        command[0] = sys.executable
    return command + (list(paths) if check.get("pass_paths") else [])


def gate_check(root, check, present):
    """Runs one of the config's gate_checks in the task's tree when the task changed a file its paths match (every
    task when it names none), and returns why it failed, or ''. It fails on a non-zero exit, with the output's last
    lines and the names its fail_regex finds."""
    patterns = check.get("paths") or []
    hits = [path for path in present if C.matches(path, patterns)] if patterns else present
    if patterns and not hits:
        return ""
    name = check.get("name") or "gate check"
    command = check_command(check, hits)
    try:
        done = subprocess.run(command, cwd=root, shell=isinstance(command, str), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=check.get("timeout") or 600,
                              env={**os.environ, **X.test_env()})
    except subprocess.TimeoutExpired:
        return f"{name}: timed out after {check.get('timeout') or 600} s"
    except OSError as error:
        return f"{name}: could not run ({error})"
    if done.returncode == 0:
        return ""
    output = (done.stdout or "") + (done.stderr or "")
    lines = [line for line in output.strip().splitlines() if line.strip()]
    names = []
    if check.get("fail_regex"):
        names = list(dict.fromkeys(match.group(1) for match in re.finditer(check["fail_regex"], output, re.M)))
    return named_reason(f"{name}: {'; '.join(lines[-3:]) or f'exit {done.returncode}'}", names)


def trusted_receipt(root, slot_name):
    """The output of the session's own full run, when the slot's receipt (written by the task hook) is signed with the
    slot's key, its output passes, and it names the tree the slot holds now. '' otherwise, and the gate runs the
    suite."""
    path = C.LOGS / f"receipt-{slot_name}.json"
    key = (C.RUNTIME.get("receipt_keys") or {}).get(slot_name, "")
    if not key or not path.exists():
        return ""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        output = str(record.get("output", ""))
        expected = task_hook.receipt_signature(key, str(record.get("tree", "")), output)
        if not hmac.compare_digest(expected, str(record.get("signature", ""))):
            C.log("the slot's test receipt is not signed with its key; running the tests")
            return ""
        if C.test_failure(output) or record.get("tree") != task_hook.tree_hash(root, C.SCOPE):
            return ""
        return output
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        C.log(f"the slot's test receipt could not be checked ({error}); running the tests")
        return ""


GATE_LOCK_STALE = 4 * 3600
GATE_LOCK_POLL = 5.0
LOCK_WAIT_LOGGED = 5.0


@contextlib.contextmanager
def landing_lock():
    """Holds SL.INTEGRATE_LOCK for the block: a task's catch-up, gate and landing happen with nothing landing between
    them. Logs once, and emits gate_wait, when the lock made the slot wait more than LOCK_WAIT_LOGGED seconds."""
    timer = C.Timer()
    SL.INTEGRATE_LOCK.acquire()
    try:
        waited = timer.seconds()
        if waited > LOCK_WAIT_LOGGED:
            C.log("the gate waits for another slot's gate or landing")
            C.emit("gate_wait", seconds=waited)
        yield
    finally:
        SL.INTEGRATE_LOCK.release()


def locked_test_run(root):
    """The test run of one gate at a time, whichever slot it is in: it holds <logs>/gate-tests.lock for the run, and a
    run that timed out is run once more before it is judged. The file lock is kept although a gate holds
    SL.INTEGRATE_LOCK: it also keeps a gate apart from the monitor's test run, which does not take that lock."""
    lock = C.LOGS / "gate-tests.lock"
    waited = False
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > GATE_LOCK_STALE:
                    os.remove(lock)
                    continue
            except OSError:
                continue
            if not waited:
                C.log("a gate waits for another gate's test run")
                waited = True
            time.sleep(GATE_LOCK_POLL)
    try:
        output = X.run_tests(root)
        if C.timed_out(output):
            C.log("the gate's test run timed out; running it once more")
            output = X.run_tests(root)
        return output
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


REASON_LIMIT = 200
REASON_KEEP = 60
NAMED_TESTS = 5
# how many times a task may be requeued for a test written outside the files the gate reads (picked)
MISPLACED_REQUEUES = 1


def named_reason(reason, names):
    """The reason, ending with up to NAMED_TESTS of the failing tests' names, all within REASON_LIMIT characters. The
    names are what a retry needs, so the reason before them is cut to make room, down to REASON_KEEP characters; a
    name that does not fit whole is left out and counted."""
    shown = []
    for name in names[:NAMED_TESTS]:
        tail = f"; failing: {', '.join(shown + [name])} and {len(names)} more"
        if len(tail) > REASON_LIMIT - REASON_KEEP:
            break
        shown.append(name)
    if not shown:
        return reason[:REASON_LIMIT]
    more = f" and {len(names) - len(shown)} more" if len(names) > len(shown) else ""
    tail = f"; failing: {', '.join(shown)}{more}"
    return reason[:REASON_LIMIT - len(tail)] + tail


def commit_made_during(slot, task_id):
    """True when a commit was made in the slot while the task ran (by a session that got round the deny list). The
    task's work may be in it, out of the checks' reach, so the driver neither judges nor lands it: it pauses, and the
    task stays open. The next start of the slot saves and undoes what is there."""
    if C.head(slot.root) == slot.base:
        return False
    made = "; ".join(C.git("log", "-3", "--format=%h %s", f"{slot.base}..HEAD", root=slot.root).stdout.splitlines())
    rival = SL.other_owner()
    C.log(f"a commit was made in slot {slot.name} during {task_id} (from {slot.base[:8]} to {C.head(slot.root)[:8]}): "
          f"{made[:200]}; pausing")
    maker = (f"Another driver (pid {rival}) holds the slots folder and is the likelier maker: end it, then RESUME. "
             if rival else "")
    C.pause(C.RUNTIME["state"], "Driver paused: a commit was made during a task", f"{task_id}'s slot "
            f"({slot.root}) holds a commit that is not the driver's: {made[:200]}. {maker}The driver did not check or "
            "land it. Look at git reflog there, then send RESUME: the task starts again from a clean slot.", "urgent")
    return True


def land(slot, state, task_id, title, message, paths, row, picked=""):
    """Commits the checked work in the slot and lands it with the task's marks, and the numbers the session picked under
    Open questions. Returns '' or a Requeue."""
    SL.commit_work(slot, message, paths)
    failed = SL.integrate(slot, message, T.done_marks(task_id, row, picked))
    if failed:
        return Requeue(failed)
    C.ctx().update(outcome="landed", reason="")
    T.task_committed(state, task_id)
    return ""


def misplaced_requeue(task_id, slot, reason):
    """misplaced_test's Requeue the first time a task writes its test where the gate does not read, and '' after, so a
    model that keeps doing it goes on to the escalation and the block like any other failure, and not through a requeue
    that costs a paid session each time."""
    found = misplaced_test(slot, reason)
    if not found:
        return ""
    state = C.RUNTIME.get("state")
    with C.STATE_LOCK:
        counts = state.setdefault("misplaced", {})
        if counts.get(task_id, 0) >= MISPLACED_REQUEUES:
            return ""
        counts[task_id] = counts.get(task_id, 0) + 1
    return found


def misplaced_test(slot, reason):
    """A Requeue when the gate found no test function the task names, yet the attempt wrote one somewhere the gate does
    not read (its diff, and its new source files, declare the function); else ''. The model wrote the test, so a second
    attempt with the same model and the reason is cheaper than an escalation."""
    prefix = "Done when: no test function "
    if not reason.startswith(prefix):
        return ""
    name = reason[len(prefix):].split()[0]
    diff = C.git("diff", "HEAD", root=slot.root).stdout
    for path in sorted(C.changed_paths(slot.root)):
        file = slot.root / path
        if C.is_source(path) and f"+++ b/{path}" not in diff and file.is_file():
            diff += "\n" + file.read_text(encoding="utf-8", errors="replace")
    text = "\n".join(line[1:] if line.startswith("+") else line for line in diff.splitlines())
    if any(match.group(1) == name for match in C.ANY_FUNC.finditer(text)):
        return Requeue(f"{reason}; the function is in the attempt's diff but not in the test files the gate reads "
                       "(misplaced, or in a file test_files does not match)")
    return ""


def worker_attempt(slot, task_id, title, spec, model, effort, feedback):
    """One session of the model does the task. feedback is None, or (the model that failed, why, its test output).
    Returns None when nothing more is to be done (landed, stopped, or a limit paused the driver), a Requeue when the
    checked work could not land, or the reason it failed."""
    C.log(f"worker starts: {task_id}: {title} ({model}, effort {effort or 'default'}, slot {slot.name})")
    prompt = prompts.WORKER_PROMPT.format(rules=prompts.worker_rules(C.rules_text(slot.root)),
                                          task=C.task_context(spec))
    prompt += attempts_note(task_id)
    if feedback:
        prompt += prompts.FEEDBACK_NOTE.format(model=feedback[0], reason=feedback[1], tests=feedback[2])
    prompt += done_when_note(spec)
    C.log(f"worker prompt: {len(prompt)} characters, up to {C.WORKER_TURNS} turns, {C.WORKER_TIMEOUT} s, "
          f"${C.WORKER_BUDGET_USD[model]:g}")
    C.ctx()["gate_output"] = ""
    text, error = X.run_session(prompt, task_id, model, effort, C.WORKER_TURNS, C.WORKER_TIMEOUT,
                                B.session_budget(C.WORKER_BUDGET_USD[model]), "worker", slot)
    if C.RUNTIME.get("stopping"):
        return None
    (C.LOGS / f"{task_id}.{model}.{C.stamp()}.md").write_text((text or error) + "\n", encoding="utf-8", newline="\n")
    if commit_made_during(slot, task_id):
        return None
    if error:
        if B.hit_usage_limit(task_id, error) or B.cut_by_daily_budget(error) or B.never_started(task_id, error):
            SL.revert(slot)
            return None
        return f"{model} could not do it: {error}"
    with landing_lock():
        behind = SL.catch_up(slot)
        if behind:
            return Requeue(behind)
        reason, totals = check_passed(slot, spec)
        if reason and C.timed_out(C.ctx().get("gate_output")):
            note = suite_fault(slot, task_id)
            if note is None:
                return None
            reason += note
        if reason:
            C.log(f"worker check failed: {reason}")
            held = L.refusals(SL.LEASES, slot.name)
            if held:
                return Requeue(f"it needed {', '.join(f'{file} (held by {task})' for file, task in held[:3])}: "
                               f"{reason}", [task for _, task in held])
            return misplaced_requeue(task_id, slot, reason) or reason
        C.log(f"worker checks passed ({totals}): scope, loop files, tests kept, tests, done when; landing")
        name = C.WORKER_MODELS[model]
        message = (f"{task_id}: {title}\n\nWritten by {name} (effort {effort or 'default'}), checked by the driver."
                   f"\n\nIts own list:\n{text[:1500]}\n\nCo-Authored-By: {name} <noreply@anthropic.com>\n")
        row = (f"| {datetime.date.today().isoformat()} | {task_id} | done | {totals} | 1 attempt(s) · {title} · "
               f"{model} |")
        failed = land(slot, C.RUNTIME["state"], task_id, title, message, SL.landing_paths(spec), row,
                      T.picked_text(text))
    if failed:
        return failed
    T.announce(C.RUNTIME["state"], f"{task_id} {title}: {totals} ({model})")
    return None


def do_worker_task(slot, task_id, title, spec, state):
    """The worker model does the task. If it fails, its changes are undone and the escalation model tries once, with the
    failure as feedback. A second failure blocks the task. Work that passed but could not land is requeued. With
    approvals on, an Opus or Fable attempt waits for approval first. With more than one slot the escalation does not
    start here: it is queued (state "escalations"), and the scheduler runs it alone once every other slot has landed
    its task, so it starts on the newest tree, no other task holds a file it needs, and its wide fixes do not land
    under running tasks."""
    queued = (state.get("escalations") or {}).get(task_id)
    if queued:
        escalate(slot, task_id, title, spec, state, queued)
        return
    model, effort = K.worker(state)
    if K.needs_approval(model, effort) and not K.approved(
            state, f"{task_id} {title}: {model}, effort {effort or 'default'}, up to ${C.WORKER_BUDGET_USD[model]:g}"):
        C.pause(state, "Loop paused: not approved", f"{task_id} stays open. Send RESUME to ask again, or "
                "MODEL sonnet to use another model.")
        return
    result = worker_attempt(slot, task_id, title, spec, model, effort, None)
    if result is None:
        return
    record_attempt(task_id, model, f"requeued: {result}" if isinstance(result, Requeue) else f"failed: {result}")
    if isinstance(result, Requeue):
        T.requeue(slot, task_id, result, state, result.wait_for)
        return
    if not (C.ESCALATE_MODEL and C.MODEL_RANK[C.ESCALATE_MODEL] > C.MODEL_RANK[model]):
        T.block(slot, task_id, title, result, "worker_failed")
        return
    queued = {"from": model, "effort": effort, "reason": str(result), "tests": X.tests_summary()}
    if C.PARALLEL_SLOTS <= 1:
        SL.prepare(slot)
        escalate(slot, task_id, title, spec, state, queued)
        return
    SL.revert(slot, f"{model} failed; {C.ESCALATE_MODEL} runs it alone next: {str(result)[:200]}")
    with C.STATE_LOCK:
        state.setdefault("escalations", {})[task_id] = queued
        C.write_driver_state(state)
    C.log(f"{task_id}: {model} failed ({result}); {C.ESCALATE_MODEL} runs it alone once the other slots are idle")
    C.ctx().update(outcome="escalating", reason=str(result)[:400])
    C.emit("escalate", **{"from": model, "to": C.ESCALATE_MODEL, "reason": str(result)[:400], "queued": True})


def escalate(slot, task_id, title, spec, state, queued):
    """The escalation model's one attempt, with the worker's failure as feedback. A failure, or a denied approval,
    blocks the task."""
    who, level = C.ESCALATE_MODEL or queued["from"], queued.get("effort")
    with C.STATE_LOCK:
        (state.get("escalations") or {}).pop(task_id, None)
        C.write_driver_state(state)
    reason = queued["reason"]
    if K.needs_approval(who, level) and not K.approved(
            state, f"{task_id} {title}: {who}, effort {level or 'default'}, up to ${C.WORKER_BUDGET_USD[who]:g}"):
        T.block(slot, task_id, title, f"{reason} (the retry by {who} was not approved)", "worker_failed")
        return
    C.log(f"{task_id}: {queued['from']} failed ({reason}); {who} tries it once")
    C.emit("escalate", **{"from": queued["from"], "to": who, "reason": reason[:400]})
    feedback = (queued["from"], reason, queued.get("tests") or "(no test output)")
    result = worker_attempt(slot, task_id, title, spec, who, level, feedback)
    if result is None:
        return
    record_attempt(task_id, who, f"requeued: {result}" if isinstance(result, Requeue) else f"failed: {result}")
    if isinstance(result, Requeue):
        T.requeue(slot, task_id, result, state, result.wait_for)
        return
    T.block(slot, task_id, title, result, "worker_failed")


def scope_changes(slot):
    """The paths in the scope a task changed: an audit, which only reports, must leave this empty."""
    return [path for path in SL.task_changes(slot.snap(), slot.root) if C.code_changed([path])]


def do_senior_task(slot, task_id, title, spec, state):
    """Runs a task whose Who line names Opus or Fable in one session (once the phone approves it, with approvals on). Its
    work must pass the same gate as a worker's, with the task and progress files also allowed, and a Fable audit must
    change nothing in the scope; otherwise every change is undone and the task is blocked. Then everything it changed is
    landed. Work that passed but cannot land is kept on a held/ branch for the owner, since running the session again
    costs far more than a merge by hand."""
    who = T.senior_model(spec)
    model, _, budget = C.SENIOR_MODELS[who]
    name = who.capitalize()
    effort = T.senior_effort(spec)
    if not K.approved(state, f"{task_id} {title}: {name}, effort {effort}, up to ${budget:g}"):
        C.pause(state, "Loop paused: not approved", f"{task_id} stays open. Send RESUME to ask again.")
        return
    C.log(f"{who} task starts: {task_id}: {title} (slot {slot.name})")
    prompt = prompts.OPUS_TASK_PROMPT.format(rules=C.rules_text(slot.root), task=spec)
    prompt += attempts_note(task_id)
    if T.is_review(task_id):
        review = T.safe_build(task_id, spec)
        (C.LOGS / f"{task_id}.packet.{C.stamp()}.md").write_text(review, encoding="utf-8", newline="\n")
        prompt += prompts.PACKET_NOTE.format(packet=review)
    text, error = X.run_session(prompt, task_id, model, effort, C.OPUS_TASK_TURNS, C.OPUS_TASK_TIMEOUT,
                                B.session_budget(budget), who, slot)
    if C.RUNTIME.get("stopping"):
        return
    (C.LOGS / f"{task_id}.{who}.{C.stamp()}.md").write_text((text or error) + "\n", encoding="utf-8", newline="\n")
    if commit_made_during(slot, task_id):
        return
    if error and (B.hit_usage_limit(task_id, error) or B.cut_by_daily_budget(error) or B.never_started(task_id, error)):
        SL.revert(slot)
        return
    with landing_lock():
        touched = scope_changes(slot) if who == "fable" else []
        if error:
            reason = f"{name} could not do it: {error}"
        elif touched:
            reason = f"an audit changes nothing in the scope, but it changed {', '.join(touched[:3])}"
        else:
            reason = check_passed(slot, spec, opus=True)[0]
        if reason and not error and C.timed_out(C.ctx().get("gate_output")):
            note = suite_fault(slot, task_id)
            if note is None:
                return
            reason += note
        if reason:
            record_attempt(task_id, who, f"blocked: {reason}")
            T.block(slot, task_id, title, reason + f". Every change was undone; copies are in {C.LOGS_REL}revert-*.",
                    f"{who}_failed")
            return
        message = (f"{task_id}: {title}\n\n{(text or '')[:1500]}\n\nCo-Authored-By: {C.WORKER_MODELS[who]} "
                   "<noreply@anthropic.com>\n")
        row = f"| {datetime.date.today().isoformat()} | {task_id} | done | - | 1 attempt(s) · {title} · {name} |"
        paths = sorted(set(SL.task_changes(slot.snap(), slot.root)) | set(SL.BOOKKEEPING))
        failed = land(slot, state, task_id, title, message, paths, row, T.picked_text(text or ""))
    if failed:
        held = SL.hold(slot, task_id)
        T.block(slot, task_id, title, f"its checked work could not land ({failed}). It is kept on branch {held}: "
                "merge it by hand, then mark the task [x]", f"{who}_not_landed")
        return
    T.announce(state, f"{task_id} {title} ({name})")


# the kinds of task, tried in order: (does this spec belong to the kind, the function that runs it). A new kind of task
# is one more row here
RUNNERS = [(T.is_senior_task, do_senior_task), (lambda spec: True, do_worker_task)]


def do_task(slot, task_id, title, spec, state):
    """Starts a task in a clean slot at the main branch's last commit, then hands it to the first runner whose kind it
    is."""
    SL.prepare(slot)
    runner = next(run for kind, run in RUNNERS if kind(spec))
    runner(slot, task_id, title, spec, state)


def task_crashed(slot, task_id, title, error, errors_in_a_row, state):
    """A task raised an error the driver did not expect. Its changes are undone and it is blocked with the error, so the
    loop goes on. If blocking fails too, or MAX_TASK_ERRORS tasks in a row hit an error, the driver pauses."""
    C.log(f"driver error during {task_id}: {error}\n{traceback.format_exc()[-1500:]}")
    if C.RUNTIME.get("stopping"):
        return
    try:
        T.block(slot, task_id, title, f"the driver hit an error during the task: {str(error)[:300]}", "driver_error")
    except Exception as second:
        C.log(f"could not block {task_id} after the error: {second}")
        C.pause(state, "Driver paused: error", f"{task_id}: {str(error)[:200]}. Blocking it failed too ({second}). "
                f"Look at {C.LOGS_REL}driver.log, then send RESUME.", "urgent")
        return
    if errors_in_a_row >= C.MAX_TASK_ERRORS:
        C.pause(state, "Driver paused: errors", f"{errors_in_a_row} tasks in a row hit a driver error, the last in "
                f"{task_id}: {str(error)[:200]}. Look at {C.LOGS_REL}driver.log, then send RESUME.", "urgent")
