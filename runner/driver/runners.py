"""Running one task in its slot: the session that does it, the gate its work must pass, and the landing, retry, requeue
or block that follows. RUNNERS lists the kinds of task; a new kind is one more row."""
import datetime
import re
import threading
import traceback

import budget as B
import commands as K
import events as E
import leases as L
import loop_common as C
import packet as PK
import phone as P
import prompts
import runtime as R
import sessions as X
import settings as S
import slots as SL
import tasks as T
import workspace as W

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


def rules_text(root):
    """The project rules file (rules_file) as the slot has it, or '' when the project has none."""
    path = root / C.CFG.rules_file
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def check_passed(slot, spec, opus=False):
    """What a commit needs, checked here in the task's slot and not taken from the session's word: nothing changed
    outside the scope, no loop-folder file the task does not name, no test removed or making fewer asserts unless the
    task names it, the test run passes now, and the task's Done when line holds. The tests run once; an
    Opus task that changed nothing in the scope skips them. Returns (reason to block or '', the totals line).
    The gate's time, its test run's time and output, and its verdict go to events.jsonl; the output is kept in
    <logs>/<task>.gate.<time>.log, and kept for a retry's feedback."""
    timer = E.Timer()
    found = {"test_seconds": 0.0, "output": ""}
    reason, totals = gate(slot, spec, opus, found)
    log = ""
    if found["output"]:
        log = f"{R.ctx().get('task', 'task')}.gate.{E.stamp()}.log"
        (C.LOGS / log).write_text(found["output"] + "\n", encoding="utf-8", newline="\n")
    load = C.suite_load(found["output"])
    E.emit("gate", seconds=timer.seconds(), test_seconds=found["test_seconds"], totals=totals, reason=reason, log=log,
           suite_seconds=load[0] if load else None, suite_limit=load[1] if load else None)
    R.ctx()["gate_output"] = found["output"]
    warn_suite_load(load)
    return reason, totals


def warn_suite_load(load):
    """Tells the phone once a day when the test run took more than SUITE_WARN_SHARE of its time limit (timing_regex
    in the config), before a slow suite starts failing tasks that did nothing wrong."""
    state = R.RUNTIME.get("state")
    if not load or state is None or load[0] < S.SUITE_WARN_SHARE * load[1]:
        return
    today = datetime.date.today().isoformat()
    with R.STATE_LOCK:
        if state.get("suite_warned") == today:
            return
        state["suite_warned"] = today
        R.write_driver_state(state)
    R.log(f"test suite near its limit: {load[0]:g} s of {load[1]} s")
    P.post("Test suite near its limit", f"The test run took {load[0]:g} s of its {load[1]} s limit "
           f"({R.ctx().get('task', 'a gate')}). Past the limit every task fails its gate. Make the slow tests cheaper, "
           "or run them in parallel. The gate's log has the output.", "high")


def suite_fault(slot, task_id):
    """After a gate whose tests ran out of time: the same tests on HEAD, with no task's changes, in the baseline slot.
    If HEAD's own suite times out or fails, the fault is not the task's: its work is undone, it stays open, and the
    driver pauses with an alert; returns None. Otherwise returns a note for the task's failure reason."""
    with BASELINE_LOCK:
        baseline = SL.Slot("baseline")
        SL.prepare(baseline)
        R.log(f"{task_id}: the gate's tests ran out of time; running HEAD's own suite ({baseline.base[:10]})")
        output = X.run_tests(baseline.root)
    failure, load = C.test_failure(output), C.suite_load(output)
    E.emit("baseline", head=baseline.base[:10], totals=C.totals_line(output), timed_out=C.timed_out(output),
           suite_seconds=load[0] if load else None)
    if C.timed_out(output) or failure:
        SL.revert(slot, "HEAD's own test suite fails or runs out of time")
        R.pause(R.RUNTIME["state"], "Driver paused: the test suite fails on HEAD", f"{task_id}'s tests ran out of time, "
                f"and so do HEAD's ({baseline.base[:10]}) with no task's changes: {(failure or output)[:200]}. "
                f"{task_id} stays open. Make the slow tests cheaper, then send RESUME.", "urgent")
        return None
    took = f", {load[0]:g} s of {load[1]} s" if load else ""
    return f" (HEAD's own suite passes: {C.totals_line(output)}{took}, so this change made a test slow or hang)"


def record_attempt(task_id, model, outcome):
    """Keeps what a session said at the end, with how its attempt ended, for the task's next attempt (ATTEMPTS_NOTE):
    the last MAX_ATTEMPTS_KEPT of them, until the task lands."""
    state = R.RUNTIME.get("state")
    if state is None:
        return
    words = (R.ctx().get("last_words") or "").strip() or "(nothing)"
    with R.STATE_LOCK:
        kept = state.setdefault("attempts", {}).setdefault(task_id, [])
        kept.append({"when": R.now(), "model": model, "outcome": str(outcome)[:600], "words": words[:3000]})
        del kept[:-S.MAX_ATTEMPTS_KEPT]
        R.write_driver_state(state)


def attempts_note(task_id):
    """The earlier attempts at the task, for its prompt, or ''."""
    kept = ((R.RUNTIME.get("state") or {}).get("attempts") or {}).get(task_id) or []
    if not kept:
        return ""
    lines = []
    for item in kept:
        words = "\n".join("  " + line for line in item["words"].splitlines())
        lines.append(f"- {item['model']}, {item['when']}: {item['outcome']}\n  What it said at the end:\n{words}")
    return prompts.ATTEMPTS_NOTE.format(attempts="\n".join(lines))


def gate(slot, spec, opus, found):
    """check_passed's checks, in order; found gets the test run's time and output."""
    snap, root = slot.snap(), slot.root
    extra = W.outside_scope(snap, S.SCOPE, root)
    if extra:
        return f"changed outside the scope: {', '.join(extra[:3])}", ""
    stray = W.loop_changes(snap, W.allowed_loop_files(spec, opus), root)
    if stray:
        return f"changed loop-folder files the task does not name: {', '.join(stray[:3])}", ""
    removed = W.removed_tests(snap, root)
    if removed:
        return f"test functions were removed: {', '.join(removed[:5])}", ""
    loosened = W.weakened_tests(snap, spec, root)
    if loosened:
        return f"tests the task does not name make fewer asserts: {', '.join(loosened[:5])}", ""
    output, totals = "", ""
    if not opus or C.code_changed(W.task_changes(snap, root)):
        tests = E.Timer()
        output = X.run_tests(root)
        found.update(test_seconds=tests.seconds(), output=output)
        R.ctx()["last_test_output"] = output
        totals = C.totals_line(output)
        failure = C.test_failure(output)
        if failure:
            return f"the driver's own test run: {failure}", totals
    return W.check_done_when(spec, output, root), totals


def commit_made_during(slot, task_id):
    """True when a commit was made in the slot while the task ran (by a session that got round the deny list). The
    task's work may be in it, out of the checks' reach, so the driver neither judges nor lands it: it pauses, and the
    task stays open. The next start of the slot saves and undoes what is there."""
    if C.head(slot.root) == slot.base:
        return False
    R.log(f"a commit was made in slot {slot.name} during {task_id} (from {slot.base[:8]} to {C.head(slot.root)[:8]}); "
          "pausing")
    R.pause(R.RUNTIME["state"], "Driver paused: a commit was made during a task", f"{task_id}'s session committed in "
            f"its slot ({slot.root}), so the driver did not check or land it. Look at git log there, then send RESUME: "
            "the task starts again from a clean slot.", "urgent")
    return True


def land(slot, state, task_id, title, message, paths, row):
    """Commits the checked work in the slot and lands it with the task's marks. Returns '' or a Requeue."""
    SL.commit_work(slot, message, paths)
    failed = SL.integrate(slot, message, T.done_marks(task_id, row))
    if failed:
        return Requeue(failed)
    R.ctx().update(outcome="landed", reason="")
    T.task_committed(state, task_id)
    return ""


def worker_attempt(slot, task_id, title, spec, model, effort, feedback):
    """One session of the model does the task. Returns None when nothing more is to be done (landed, stopped, or a limit
    paused the driver), a Requeue when the checked work could not land, or the reason it failed."""
    R.log(f"worker starts: {task_id}: {title} ({model}, effort {effort or 'default'}, slot {slot.name})")
    prompt = prompts.WORKER_PROMPT.format(rules=rules_text(slot.root), task=C.task_context(spec))
    prompt += attempts_note(task_id)
    if feedback:
        prompt += prompts.FEEDBACK_NOTE.format(model=feedback[0], reason=feedback[1],
                                               tests=X.tests_summary(R.ctx().get("last_test_output", "")))
    R.log(f"worker prompt: {len(prompt)} characters, up to {S.WORKER_TURNS} turns, {S.WORKER_TIMEOUT} s, "
          f"${S.WORKER_BUDGET_USD[model]:g}")
    text, error = X.run_session(prompt, task_id, model, effort, S.WORKER_TURNS, S.WORKER_TIMEOUT,
                                B.session_budget(S.WORKER_BUDGET_USD[model]), "worker", slot)
    if R.RUNTIME.get("stopping"):
        return None
    (C.LOGS / f"{task_id}.{model}.{E.stamp()}.md").write_text((text or error) + "\n", encoding="utf-8", newline="\n")
    if commit_made_during(slot, task_id):
        return None
    if error:
        if B.hit_usage_limit(task_id, error) or B.cut_by_daily_budget(error) or B.never_started(task_id, error):
            SL.revert(slot)
            return None
        return f"{model} could not do it: {error}"
    behind = SL.catch_up(slot)
    if behind:
        return Requeue(behind)
    reason, totals = check_passed(slot, spec)
    if reason and C.timed_out(R.ctx().get("gate_output")):
        note = suite_fault(slot, task_id)
        if note is None:
            return None
        reason += note
    if reason:
        R.log(f"worker check failed: {reason}")
        held = L.refusals(SL.LEASES, slot.name)
        if held:
            return Requeue(f"it needed {', '.join(f'{file} (held by {task})' for file, task in held[:3])}: {reason}",
                           [task for _, task in held])
        return reason
    R.log(f"worker checks passed ({totals}): scope, loop files, tests kept, tests, done when; landing")
    name = S.WORKER_MODELS[model]
    message = (f"{task_id}: {title}\n\nWritten by {name} (effort {effort or 'default'}), checked by the driver.\n\n"
               f"Its own list:\n{text[:1500]}\n\nCo-Authored-By: {name} <noreply@anthropic.com>\n")
    row = (f"| {datetime.date.today().isoformat()} | {task_id} | done | {totals} | 1 attempt(s) · {title} · "
           f"{model} |")
    failed = land(slot, R.RUNTIME["state"], task_id, title, message,
                  S.SCOPE + sorted(W.allowed_loop_files(spec, False)) + W.done_files(spec), row)
    if failed:
        return failed
    T.announce(R.RUNTIME["state"], f"{task_id} {title}: {totals} ({model})")
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
            state, f"{task_id} {title}: {model}, effort {effort or 'default'}, up to ${S.WORKER_BUDGET_USD[model]:g}"):
        R.pause(state, "Loop paused: not approved", f"{task_id} stays open. Send RESUME to ask again, or "
                "MODEL sonnet to use another model.")
        return
    result = worker_attempt(slot, task_id, title, spec, model, effort, None)
    if result is None:
        return
    record_attempt(task_id, model, f"requeued: {result}" if isinstance(result, Requeue) else f"failed: {result}")
    if isinstance(result, Requeue):
        T.requeue(slot, task_id, result, state, result.wait_for)
        return
    if not (S.ESCALATE_MODEL and S.MODEL_RANK[S.ESCALATE_MODEL] > S.MODEL_RANK[model]):
        T.block(slot, task_id, title, result, "worker_failed")
        return
    queued = {"from": model, "effort": effort, "reason": str(result)}
    if S.PARALLEL_SLOTS <= 1:
        SL.prepare(slot)
        escalate(slot, task_id, title, spec, state, queued)
        return
    SL.revert(slot, f"{model} failed; {S.ESCALATE_MODEL} runs it alone next: {str(result)[:200]}")
    with R.STATE_LOCK:
        state.setdefault("escalations", {})[task_id] = queued
        R.write_driver_state(state)
    R.log(f"{task_id}: {model} failed ({result}); {S.ESCALATE_MODEL} runs it alone once the other slots are idle")
    R.ctx().update(outcome="escalating", reason=str(result)[:400])
    E.emit("escalate", **{"from": model, "to": S.ESCALATE_MODEL, "reason": str(result)[:400], "queued": True})


def escalate(slot, task_id, title, spec, state, queued):
    """The escalation model's one attempt, with the worker's failure as feedback. A failure, or a denied approval,
    blocks the task."""
    who, level = S.ESCALATE_MODEL or queued["from"], queued.get("effort")
    with R.STATE_LOCK:
        (state.get("escalations") or {}).pop(task_id, None)
        R.write_driver_state(state)
    reason = queued["reason"]
    if K.needs_approval(who, level) and not K.approved(
            state, f"{task_id} {title}: {who}, effort {level or 'default'}, up to ${S.WORKER_BUDGET_USD[who]:g}"):
        T.block(slot, task_id, title, f"{reason} (the retry by {who} was not approved)", "worker_failed")
        return
    R.log(f"{task_id}: {queued['from']} failed ({reason}); {who} tries it once")
    E.emit("escalate", **{"from": queued["from"], "to": who, "reason": reason[:400]})
    result = worker_attempt(slot, task_id, title, spec, who, level, (queued["from"], reason))
    if result is None:
        return
    record_attempt(task_id, who, f"requeued: {result}" if isinstance(result, Requeue) else f"failed: {result}")
    if isinstance(result, Requeue):
        T.requeue(slot, task_id, result, state, result.wait_for)
        return
    T.block(slot, task_id, title, result, "worker_failed")


def is_opus_task(spec):
    """A task whose Who line says Opus is a review or a task revision: Opus does it once, at the effort its Effort line
    names (high without one)."""
    return bool(re.search(r"^\*\*Who\.\*\* Opus\b", spec, re.M))


def opus_effort(spec):
    """The effort an Opus task asks for in an **Effort.** line (low, medium, high, xhigh or max), else OPUS_EFFORT."""
    match = re.search(r"^\*\*Effort\.\*\* (\w+)", spec, re.M)
    return match.group(1).lower() if match and match.group(1).lower() in S.WORKER_EFFORTS else S.OPUS_EFFORT


def do_opus_task(slot, task_id, title, spec, state):
    """Runs a task marked Opus in one session (once the phone approves it, with approvals on). Its work must pass the
    same gate as a worker's, with the task and progress files also allowed; otherwise every change is undone and the
    task is blocked. Then everything it changed is landed. Work that passed but cannot land is kept on a held/ branch
    for the owner, since running Opus again costs far more than a merge by hand."""
    effort = opus_effort(spec)
    if not K.approved(state, f"{task_id} {title}: Opus, effort {effort}, up to ${S.OPUS_TASK_BUDGET_USD:g}"):
        R.pause(state, "Loop paused: not approved", f"{task_id} stays open. Send RESUME to ask again.")
        return
    R.log(f"opus task starts: {task_id}: {title} (slot {slot.name})")
    prompt = prompts.OPUS_TASK_PROMPT.format(rules=rules_text(slot.root), task=spec)
    prompt += attempts_note(task_id)
    if PK.is_review(task_id):
        review = PK.safe_build(task_id, spec)
        (C.LOGS / f"{task_id}.packet.{E.stamp()}.md").write_text(review, encoding="utf-8", newline="\n")
        prompt += prompts.PACKET_NOTE.format(packet=review)
    text, error = X.run_session(prompt, task_id, S.OPUS_MODEL, effort, S.OPUS_TASK_TURNS, S.OPUS_TASK_TIMEOUT,
                                B.session_budget(S.OPUS_TASK_BUDGET_USD), "opus", slot)
    if R.RUNTIME.get("stopping"):
        return
    (C.LOGS / f"{task_id}.opus.{E.stamp()}.md").write_text((text or error) + "\n", encoding="utf-8", newline="\n")
    if commit_made_during(slot, task_id):
        return
    if error and (B.hit_usage_limit(task_id, error) or B.cut_by_daily_budget(error) or B.never_started(task_id, error)):
        SL.revert(slot)
        return
    reason = f"Opus could not do it: {error}" if error else check_passed(slot, spec, opus=True)[0]
    if reason and not error and C.timed_out(R.ctx().get("gate_output")):
        note = suite_fault(slot, task_id)
        if note is None:
            return
        reason += note
    if reason:
        record_attempt(task_id, "opus", f"blocked: {reason}")
        T.block(slot, task_id, title, reason + ". Every change was undone; copies are in <logs>/revert-*.",
                "opus_failed")
        return
    message = (f"{task_id}: {title}\n\n{(text or '')[:1500]}\n\nCo-Authored-By: {S.WORKER_MODELS['opus']} "
               "<noreply@anthropic.com>\n")
    row = f"| {datetime.date.today().isoformat()} | {task_id} | done | - | 1 attempt(s) · {title} · Opus |"
    paths = sorted(set(W.task_changes(slot.snap(), slot.root)) | set(SL.BOOKKEEPING))
    failed = land(slot, state, task_id, title, message, paths, row)
    if failed:
        held = SL.hold(slot, task_id)
        T.block(slot, task_id, title, f"its checked work could not land ({failed}). It is kept on branch {held}: "
                "merge it by hand, then mark the task [x]", "opus_not_landed")
        return
    T.announce(state, f"{task_id} {title} (Opus)")


# the kinds of task, tried in order: (does this spec belong to the kind, the function that runs it). A new kind of task
# is one more row here
RUNNERS = [(is_opus_task, do_opus_task), (lambda spec: True, do_worker_task)]


def do_task(slot, task_id, title, spec, state):
    """Starts a task in a clean slot at the main branch's last commit, then hands it to the first runner whose kind it
    is."""
    SL.prepare(slot)
    runner = next(run for kind, run in RUNNERS if kind(spec))
    runner(slot, task_id, title, spec, state)


def task_crashed(slot, task_id, title, error, errors_in_a_row, state):
    """A task raised an error the driver did not expect. Its changes are undone and it is blocked with the error, so the
    loop goes on. If blocking fails too, or MAX_TASK_ERRORS tasks in a row hit an error, the driver pauses."""
    R.log(f"driver error during {task_id}: {error}\n{traceback.format_exc()[-1500:]}")
    if R.RUNTIME.get("stopping"):
        return
    try:
        T.block(slot, task_id, title, f"the driver hit an error during the task: {str(error)[:300]}", "driver_error")
    except Exception as second:
        R.log(f"could not block {task_id} after the error: {second}")
        R.pause(state, "Driver paused: error", f"{task_id}: {str(error)[:200]}. Blocking it failed too ({second}). "
                "Look at driver.log in the logs folder, then send RESUME.", "urgent")
        return
    if errors_in_a_row >= S.MAX_TASK_ERRORS:
        R.pause(state, "Driver paused: errors", f"{errors_in_a_row} tasks in a row hit a driver error, the last in "
                f"{task_id}: {str(error)[:200]}. Look at driver.log in the logs folder, then send RESUME.", "urgent")
