"""Running one task: the snapshot it starts from, the session or worker run that does it, the gate its work must pass,
and the commit, retry or block that follows. RUNNERS lists the kinds of task; a new kind is one more row. The triage
after a run of blocks is here too, since it passes the same gate."""
import datetime
import re
import subprocess
import sys
import time
import traceback

import budget as B
import commands as K
import phone as P
import prompts
import runtime as R
import sessions as X
import settings as S
import tasks as T
import workspace as WS
import worker_task as W

C = W.C


def rules_text():
    path = C.project / C.rules_file
    return path.read_text(encoding="utf-8") if path.exists() else "(no rules file)"


def check_passed(run, spec="", opus=False, require_done_when=False):
    """What a commit needs, checked here against the task's snapshot and not taken from the session's word: nothing
    changed outside the scope, no loop-folder file the task does not name, no test removed or making fewer asserts
    unless the task names it, the test run passes now, and the Done when line holds. The tests run once; an Opus task or
    a triage that changed nothing in the scope skips them. Returns (reason, totals); the reason is empty on a pass."""
    snap = R.current_snap()
    extra = WS.outside_scope(snap)
    if extra:
        return f"changed outside the scope: {', '.join(extra[:3])}", ""
    stray = WS.loop_changes(snap, WS.allowed_loop_files(spec, opus))
    if stray:
        return f"changed files in {WS.loop_folder()} the task does not name: {', '.join(stray[:3])}", ""
    if run.get("tests_removed"):
        return f"{run['tests_removed']} tests were removed", ""
    before, after = R.RUNTIME.get("tests_before"), W.count_tests()
    if before is not None and after is not None and after < before:
        return f"tests were removed: {before} before the task, {after} now", ""
    removed = WS.removed_tests(snap)
    if removed:
        return f"test functions were removed: {', '.join(removed[:5])}", ""
    loosened = WS.weakened_tests(snap, spec)
    if loosened:
        return f"tests the task does not name make fewer asserts: {', '.join(loosened[:5])}", ""
    output, totals = "", "-"
    if not opus or any(W.in_scope(path) for path in WS.task_changes(snap)):
        output = X.run_tests()
        totals = W.totals_line(output)
        if not totals or C.pass_marker not in totals:
            return f"the driver's own test run: {totals or output[:200] or 'no totals printed'}", totals
    return WS.check_done_when(spec, output, require_done_when), totals


def commit_made_during(task_id):
    """True when a commit was made while the task ran (by hand, or by a session that got round the deny list). The
    task's work may be in it, out of the checks' reach, so the driver neither judges nor commits it: it pauses with the
    changes left as they are, and the task stays open."""
    snap = R.current_snap()
    if not WS.head_moved(snap):
        return False
    R.log(f"HEAD moved during {task_id} (from {snap['head'][:8]} to {WS.head()[:8]}); pausing")
    P.pause(R.RUNTIME["state"], "Driver paused: a commit was made during a task", f"HEAD moved while {task_id} ran, so "
            "the driver did not check or commit it. Its uncommitted changes are left in place. Look at git log, then "
            "send RESUME: the task starts again, and leftovers in the scope are saved and undone first.", "urgent")
    return True


def model_attempt(task_id, title, spec, model, effort, feedback):
    """One session of the model does the task. Returns None when nothing more is to be done (committed, stopped, or a
    limit paused the driver), or the reason it failed."""
    W.CURRENT_TASK["id"] = task_id
    prompt = prompts.TASK_PROMPT.format(brief=C.project_brief, scope=", ".join(C.scope), loop=WS.loop_folder(),
                                        test_command=C.test_command_text, rules_file=C.rules_file, rules=rules_text(),
                                        task=spec)
    if feedback:
        prompt += prompts.FEEDBACK_NOTE.format(model=feedback[0], reason=feedback[1], tests=X.test_output()[-4000:])
    R.log(f"task starts: {task_id}: {title} ({model}, effort {effort or 'default'}, {len(prompt)} characters, "
          f"up to {C.task_turns} turns, ${S.MODEL_BUDGET_USD[model]:g})")
    text, error = X.run_session(prompt, task_id, model, effort, C.task_turns, C.task_timeout,
                                B.session_budget(S.MODEL_BUDGET_USD[model]), "task")
    if R.RUNTIME.get("stopping"):
        return None
    (C.logs / f"{task_id}.{model}.md").write_text((text or error) + "\n", encoding="utf-8", newline="\n")
    if commit_made_during(task_id):
        return None
    if error:
        if B.hit_usage_limit(task_id, error, R.RUNTIME["state"]) or B.cut_by_daily_budget(error):
            T.revert_unfinished()
            return None
        return f"{model} could not do it: {error}"
    reason, totals = check_passed({}, spec)
    if reason:
        R.log(f"task check failed: {reason}")
        return reason
    name = S.MODEL_NAMES[model]
    body = f"Written by {name} (effort {effort or 'default'}), checked by the driver.\n\nIts own summary:\n{text[:1500]}"
    T.finish_with_commit(task_id, title, totals, body, f"\n\nCo-Authored-By: {name} <noreply@anthropic.com>",
                         sorted(WS.allowed_loop_files(spec, False)) + WS.done_files(spec))
    return None


def model_task(task_id, title, spec, state):
    """The task model does the task. If it fails, its changes are undone and the escalation model tries once, with the
    failure as feedback. A second failure blocks the task. With approvals on, an Opus or Fable attempt waits for
    approval first."""
    model, effort = K.task_model(state)
    escalate = S.get("escalate_model")
    plan = [(model, effort)]
    if escalate and S.MODEL_RANK[escalate] > S.MODEL_RANK[model]:
        plan.append((escalate, effort))
    reason = ""
    for index, (who, level) in enumerate(plan):
        if K.needs_approval(who, level) and not K.approved(
                state, f"{task_id} {title}: {who}, effort {level or 'default'}, up to ${S.MODEL_BUDGET_USD[who]:g}"):
            if index == 0:
                P.pause(state, "Loop paused: not approved", f"{task_id} stays open. Send RESUME to ask again, or MODEL "
                        "sonnet to use another model.")
                return
            reason += f" (the retry by {who} was not approved)"
            break
        if index:
            T.revert_unfinished()
            R.log(f"{task_id}: {plan[index - 1][0]} failed ({reason}); {who} tries it once")
        reason = model_attempt(task_id, title, spec, who, level, (plan[index - 1][0], reason) if index else None)
        if reason is None:
            return
    T.block(task_id, reason, "task_failed")


def run_worker(task_id, state, feedback=None):
    """Runs worker_task.py for a task and returns its status once it has ended."""
    command = [sys.executable, R.PATHS["worker"], task_id]
    if feedback:
        command.append(str(feedback))
    R.log(f"worker starts: {task_id}" + (" (a retry, with the test output)" if feedback else ""))
    before = C.status_file.stat().st_mtime if C.status_file.exists() else 0
    group = {} if sys.platform == "win32" else {"start_new_session": True}
    process = subprocess.Popen(command, cwd=C.project, env=T.driver_env(), **group)
    R.CHILD["pid"] = process.pid
    with X.polling():
        while process.poll() is None:
            time.sleep(2)
    R.CHILD["pid"] = None
    if not C.status_file.exists() or C.status_file.stat().st_mtime <= before:
        R.log(f"worker did not start {task_id} (exit {process.returncode}); its status file did not change")
        return {"state": "not_started"}
    run = R.read_json(C.status_file)
    R.log(f"worker ends: {task_id}: state {run.get('state')}, exit {process.returncode}")
    return run


def worker_task(task_id, title, spec, state):
    """Runs the worker on a task. A state that allows it is retried with the test output, up to max_attempts. A task
    that passes is committed; anything else is blocked."""
    feedback = None
    while True:
        run = run_worker(task_id, state, feedback)
        if commit_made_during(task_id):
            return
        outcome = run.get("state")
        if outcome == "passed":
            reason, totals = check_passed(run, spec)
            if reason:
                T.block(task_id, reason, outcome)
                return
            T.finish_with_commit(task_id, title, totals, f"Written by the worker (attempt {run.get('attempt', 1)}).",
                                 extra_paths=sorted(WS.allowed_loop_files(spec, False)) + WS.done_files(spec))
            return
        if outcome in ("stopped", "not_started"):
            state["paused"] = True
            R.write_state(state)
            R.log(f"{task_id} was {outcome}: paused, the task stays open")
            return
        if outcome in S.get("retry_states") and run.get("attempt", 1) < S.get("max_attempts"):
            feedback = C.logs / f"{task_id}.feedback.txt"
            feedback.write_text(f"The last attempt ended in state {outcome}. Its test output:\n"
                                f"{X.test_output()[-4000:]}\n", encoding="utf-8", newline="\n")
            R.log(f"{task_id} will be retried with its test output (attempt {run.get('attempt', 1) + 1})")
            continue
        T.block(task_id, f"state {outcome}" + (f", {run.get('totals')}" if run.get("totals") else ""), outcome)
        return


def is_opus_task(spec):
    """A task whose Who line says Opus is a review or planning task: Opus does it once, at high effort."""
    return bool(re.search(r"^\*\*Who\.\*\* Opus\b", spec, re.M))


def do_opus_task(task_id, title, spec, state):
    """Runs a task marked Opus in one session (once the phone approves it, with approvals on). Its work must pass the
    same gate as a task model's, with the task and progress files also allowed and the Done when line required; then
    everything it changed since the snapshot is committed."""
    if not K.approved(state, f"{task_id} {title}: Opus, effort {S.OPUS_EFFORT}, up to ${S.OPUS_TASK_BUDGET_USD:g}"):
        P.pause(state, "Loop paused: not approved", f"{task_id} stays open. Send RESUME to ask again.")
        return
    W.CURRENT_TASK["id"] = task_id
    prompt = prompts.OPUS_TASK_PROMPT.format(rules=rules_text(), test_command=C.test_command_text, task=spec)
    R.log(f"opus task starts: {task_id}: {title}")
    text, error = X.run_session(prompt, task_id, S.OPUS_MODEL, S.OPUS_EFFORT, S.OPUS_TASK_TURNS, S.OPUS_TASK_TIMEOUT,
                                B.session_budget(S.OPUS_TASK_BUDGET_USD), "opus")
    if R.RUNTIME.get("stopping"):
        return
    (C.logs / f"{task_id}.opus.md").write_text((text or error) + "\n", encoding="utf-8", newline="\n")
    if commit_made_during(task_id):
        return
    if error and (B.hit_usage_limit(task_id, error, state) or B.cut_by_daily_budget(error)):
        T.revert_unfinished()
        return
    reason, totals = (f"Opus could not do it: {error}", "-") if error else check_passed({}, spec, True, True)
    if reason:
        T.block(task_id, reason, "opus_failed")
        return
    T.set_heading(task_id, "x")
    T.add_progress(row=f"| {datetime.date.today().isoformat()} | {task_id} | done | {totals} | {title} · Opus |")
    paths = sorted(set(WS.task_changes(R.current_snap())) | {C.tasks_file, C.progress_file})
    T.commit(f"{task_id}: {title}\n\n{(text or '')[:1500]}\n\nCo-Authored-By: {S.MODEL_NAMES['opus']} "
             "<noreply@anthropic.com>\n", paths)
    P.post("Committed", f"{task_id} {title} (Opus)")
    T.task_committed(state)


def run_triage(state, blocked):
    """Opus looks for what a run of blocked tasks shares, between tasks, and may fix it or rewrite the tasks. Its work
    passes the same gate as an Opus task's, then is committed, or undone if it fails."""
    state.pop("triage", None)
    snap = WS.snapshot()
    R.set_snap(state, snap)
    W.CURRENT_TASK["id"] = "triage"
    prompt = prompts.TRIAGE_PROMPT.format(blocked=blocked, logs=C.logs_dir, progress_file=C.progress_file,
                                          tasks_file=C.tasks_file, test_command=C.test_command_text, rules=rules_text())
    R.log(f"triage starts: {blocked[:200]!r}")
    name = f"triage-{time.strftime('%Y%m%d-%H%M%S')}"
    text, error = X.run_session(prompt, name, S.OPUS_MODEL, S.OPUS_EFFORT, S.TRIAGE_TURNS, S.TRIAGE_TIMEOUT,
                                B.session_budget(S.TRIAGE_BUDGET_USD), "triage")
    try:
        if R.RUNTIME.get("stopping") or commit_made_during("the triage"):
            return
        changed = WS.task_changes(snap)
        reason = f"Opus could not do it: {error}" if error else (check_passed({}, "", True)[0] if changed else "")
        if reason:
            T.revert_unfinished()
            P.post("Triage undone", f"{reason[:300]}. Its summary is in {C.logs_dir}/{name}.triage.jsonl.", "high")
            return
        if not changed:
            P.post("Triage: no change", (text or "No shared cause found.")[:400])
            return
        T.commit(f"Triage of blocked tasks\n\n{blocked}\n\n{(text or '')[:1500]}\n\nCo-Authored-By: "
                 f"{S.MODEL_NAMES['opus']} <noreply@anthropic.com>\n", sorted(set(changed)))
        P.post("Triage committed", (text or "")[:400])
    finally:
        R.set_snap(state, None)


# the kinds of task, tried in order: (does this task belong to the kind, given its spec and whether the task model works
# alone, the function that runs it). A new kind of task is one more row here
RUNNERS = [
    (lambda spec, solo: is_opus_task(spec), do_opus_task),
    (lambda spec, solo: solo, model_task),
    (lambda spec, solo: True, worker_task),
]


def do_task(task_id, title, state, solo):
    """Starts a task from a clean scope and a snapshot, then hands it to the first runner whose kind it is."""
    spec = W.find_spec(task_id)
    if not T.clear_leftovers(task_id):
        P.pause(state, "Driver paused", f"Could not start {task_id}: changes are left over from an earlier run. Look at "
                "the driver log, then send RESUME.")
        return
    snap = WS.snapshot()
    R.set_snap(state, snap)
    if snap["dirty"]:
        R.log(f"already changed before {task_id}, left alone: {', '.join(sorted(snap['dirty'])[:5])}")
    R.RUNTIME["tests_before"] = W.count_tests()
    runner = next(run for kind, run in RUNNERS if kind(spec, solo))
    runner(task_id, title, spec, state)


def task_crashed(task_id, error, errors_in_a_row, state):
    """A task raised an error the driver did not expect. Its changes are undone and it is blocked with the error, so the
    loop goes on. If blocking fails too, or MAX_TASK_ERRORS tasks in a row hit an error, the driver pauses."""
    R.log(f"driver error during {task_id}: {error}\n{traceback.format_exc()[-1500:]}")
    if R.RUNTIME.get("stopping"):
        return
    try:
        T.block(task_id, f"the driver hit an error during the task: {str(error)[:300]}", "driver_error")
    except Exception as second:
        R.log(f"could not block {task_id} after the error: {second}")
        P.pause(state, "Driver paused: error", f"{task_id}: {str(error)[:200]}. Blocking it failed too ({second}). "
                "Look at the driver log, then send RESUME.", "urgent")
        return
    if errors_in_a_row >= S.MAX_TASK_ERRORS:
        P.pause(state, "Driver paused: errors", f"{errors_in_a_row} tasks in a row hit a driver error, the last in "
                f"{task_id}: {str(error)[:200]}. Look at the driver log, then send RESUME.", "urgent")
