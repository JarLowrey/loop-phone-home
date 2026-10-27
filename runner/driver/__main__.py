"""The loop driver: runs the open tasks in the task file one at a time, without an orchestrating session.

Usage, from the folder that holds worker.json (or with WORKER_CONFIG set), where <runner> is this repo's runner folder:
  python <runner>/driver             the supervisor: runs the driver, and starts it again after a crash
  python <runner>/driver --run       the driver alone, with no supervisor (the supervisor starts it this way)
  python <runner>/driver --worker    as the first form, but the worker does each task (worker_task.py) and Haiku reviews
  python <runner>/driver --shutdown  ends the supervisor, the driver and its session, reverts the unfinished task, stops
  python <runner>/driver --model sonnet [high]   who does the tasks from the next one on (the MODEL command)
  python <runner>/driver --approve | --deny      answers every approval the driver is waiting for (the phone's buttons)
  python <runner>/driver --install-autostart [--worker]   starts the driver at each logon (Windows Startup folder; on
                                     other systems it prints a crontab line), unless the owner ended it last
  python <runner>/driver --autostart [--worker]           what the Startup file runs
  python <runner>/driver/check_driver.py [lines]           is it running, on what, and the last log lines

Each task is done by one session of the task model (task_model in worker.json, sonnet by default; MODEL changes it). A
task starts from a snapshot (HEAD and the paths already changed), and is committed only when the driver's own checks
pass against it: no commit was made during the task, nothing changed outside the scope, no file in the loop folder
changed that the task does not name, no test was removed or makes fewer asserts unless the task names it, the test run
passes now (within test_timeout), and the task's Done when line holds. A task that fails is undone and tried once more
by the escalation model with the failure as feedback; a second failure blocks it. Two blocks in a row start an Opus
triage of what they share; two more after it pause the driver. A task whose Who line says Opus is a review or planning
task, and Opus does it. No session may spend more than what is left of daily_budget_usd.

With approvals on (the default), every automated call to Opus at medium effort or more, and every call to Fable, waits
for an Approve button on the phone (or --approve on the machine). approvals: off in the driver-settings block lets them
run within their budgets.

Commands from the phone: send MAN, or see prompts.MAN_TEXT.

Config: worker.json (see worker_task.py DEFAULTS). The driver reads loop_file, progress_file, open_task_pattern,
rules_file, project_brief, sync, task_model, task_turns, task_timeout, test_timeout, test_function_regex,
any_function_regex, assert_regex, deny_tools, usage_log and token_file from it. loop_file holds a driver-settings block
(poll_seconds, max_attempts, retry_states, escalate_model, daily_budget_usd, approval_hours, approvals), which is read
again at each pass, so an edit takes effect without a restart.

The modules, each with one job:
  mainloop.py   the loop, the supervisor, the autostart          runners.py    one task: snapshot, run, gate, commit
  commands.py   the owner's commands, approvals, shutdown        tasks.py      task file, progress file, commits, blocks
  sessions.py   Claude sessions and test runs                    budget.py     usage limits and the daily budget
  workspace.py  the snapshot and the gate's git checks           settings.py   the driver-settings block and the limits
  runtime.py    driver.json, the log, pid files                  phone.py      messages to the phone
  prompts.py    every prompt text
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]
import worker_task as W  # noqa: E402

if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    W.configure()
    import commands as K  # noqa: E402
    import mainloop as M  # noqa: E402
    import runtime as R  # noqa: E402
    R.init_paths()
    argv = sys.argv[1:]
    mode_args = ["--worker"] if "--worker" in argv else []
    if "--shutdown" in argv:
        K.cli_shutdown()
        sys.exit(0)
    if "--install-autostart" in argv:
        M.install_autostart(mode_args)
        sys.exit(0)
    if "--remove-autostart" in argv:
        M.STARTUP_FILE.unlink(missing_ok=True)
        print("autostart removed")
        sys.exit(0)
    if "--autostart" in argv:
        sys.exit(M.autostart(mode_args))
    for flag, word in (("--model", "MODEL"), ("--approve", "APPROVE"), ("--deny", "DENY")):
        if flag in argv:
            K.queue_request(" ".join([word, *argv[argv.index(flag) + 1:]]))
            sys.exit(0)
    sys.exit(M.run_driver(solo=not mode_args) if "--run" in argv else M.supervise(mode_args))
