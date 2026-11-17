"""The build loop's driver. It runs the tasks in the task file (tasks_file in the config), without a Claude orchestrator
session. The config is worker.json in the current folder, or the file WORKER_CONFIG names; runner/worker.example.json
shows every key, and core.CONFIG_DEFAULTS says what each is for.

Usage, from the project root (<runner> is the folder that holds this driver):
  python <runner>/driver             the supervisor: runs the driver, and restarts it after a crash (waits longer each
                                     time)
  python <runner>/driver --run       the driver alone, with no supervisor (the supervisor starts it this way)
  python <runner>/driver --shutdown  kills the supervisor, the driver and its sessions now, reverts the unfinished
                                     tasks' changes in their slots, and leaves the driver paused
  python <runner>/driver --model sonnet [high]   who does the tasks from the next one on (the MODEL command)
  python <runner>/driver --approve | --deny      answers every approval the driver is waiting for (the phone's buttons)
  python <runner>/driver --install-autostart     starts the driver at each logon (a file in the Startup folder, or a
                                     crontab line elsewhere), unless the owner ended it last; --remove-autostart takes
                                     that file away
  python <runner>/driver --autostart             what the Startup file runs
  python <runner>/driver --status     rewrites and prints the status file: what is done, blocked and left
  python <runner>/driver --lint-tasks reads every open task's Files and Done when lines and prints each fault the gate
                                     would hit, before any session starts
  python <runner>/driver --tidy       moves landed task specs out of the task file into the archive file
  python <runner>/driver --check      is it running, from which commit, on what, and the last log lines
  python <runner>/driver --simulate   what parallel_slots and parallel_window would do to the queue
  python <runner>/driver --report     where the loop's time and money went, from <logs>/events.jsonl
  python <runner>/driver --monitor [--force]   the monitor (monitor.py) on the main branch's last commit, in a slot of
                                     its own, unless that commit has a record already; runs beside a driver
  python <runner>/driver --install-monitor-schedule   runs --monitor each night (a Windows scheduled task, or a crontab
                                     line elsewhere); --remove-monitor-schedule takes it away

A driver starts only from the main tree, never from a slot or a session the driver started, and an argument none of the
commands above knows is an error, never a start. One driver holds the slots folder (driver.owner in it), whichever tree
it was started from.

--check, --simulate and --report each have a file of their own (check_driver.py, simulate.py, report.py) and can still
be run by path; the flags are here so one command reaches everything.

Each task is done by one session of the worker model (task_model unless MODEL says otherwise), in a slot: a git worktree
of its own under slots_dir, reset to the main branch's last commit. Up to parallel_slots tasks run side by side (the
loop file's settings; scheduler.py says which may). The owner's main tree is never a task's: edits there are never
judged or undone, and the driver reads only committed tasks. A task's work lands only when the driver's own checks pass
in its slot: no commit was made during the task, nothing changed outside the scope, no loop-folder file changed that the
task does not name, no test function was removed or makes fewer asserts unless the task names it, every companion file
is in place, the config's gate_checks pass, the tests pass, and the task's Done when line holds. The tests run once: the
session's own full run counts when its signed receipt (task_hook.py) names the tree the slot holds. Then, one slot at a
time, the work is rebased onto the newest commit (added lines on both sides are merged), checked for doubled names and
tested again if other work landed meanwhile, and the main branch fast-forwards to it. Work that cannot land goes back to
the queue; after max_requeues it runs alone. A task that fails is undone and tried once more by the escalation model
(escalate_model in the loop file), alone once the other slots have landed their tasks, with the failure as feedback; if
that fails too, it is blocked. Two blocks in a row start an Opus triage of what they share; when its fix lands, the
blocked tasks open again. Two more blocks after it pause the driver. A task whose Who line says Opus is a review, and
Opus does it, alone, with the review packet (tasks.build) when its id is a review's. A task whose Who line says Fable is
an audit: Fable does it alone, and it must change nothing in the scope.

A gate whose tests run out of time is checked against HEAD's own suite: when HEAD's times out too, the fault is not the
task's, so it stays open and the driver pauses. From 5 minutes before a session's time limit the task hook refuses its
every tool call, so it ends by saying what it found; that, and each earlier attempt's last message, reach the task's
next attempt. The phone hears once a day when the test run passes 60% of its limit (timing_regex). When a commit changes
the driver's own code, the supervised driver restarts on it once nothing runs.

With approvals on in the loop file, every automated call to Opus at medium effort or more, and every call to Fable, waits
for an Approve button on the phone (or --approve on the PC). A denial pauses the driver; the task stays open.

Commands from the phone (any case, the whole message): see prompts.MAN_TEXT, or send MAN. Anything else is logged and
ignored. With a token file (token_file in the config), every call to ntfy carries that access token.

The modules, each with one job. core.py is the bottom of the stack and imports none of the others; every module below
imports it as C:
  core.py       the config, paths and git, the phone, driver.json and the log, the loop file's settings, events.jsonl
  mainloop.py   the loop, the supervisor, the autostart          runners.py   one task: session, gate, landing
  scheduler.py  which task starts next, in which slot            slots.py     worktrees, rebase, landing, push,
  commands.py   the owner's commands, approvals, shutdown                     and the gate's checks on a task's work
  sessions.py   Claude sessions and test runs                    tasks.py     the task, progress and status files,
  opus.py       the phone's OPUS: and OPUS WRITE:                             blocks, requeues, the review packet
  prompts.py    every prompt text                                budget.py    usage limits, the daily budget, reserves
  monitor.py    the clean-checkout and monitor commands, alerts  task_hook.py task sessions stay in their slot, and
  leases.py     which running task holds which file                           the receipt of their full test run
  check_driver.py / simulate.py / report.py   the read-only tools behind --check, --simulate and --report

Logs, in logs_dir:
  events.jsonl   the record for audits: every phase of every task run (session, gate, catch-up, landing, push) with
                 its duration and outcome, why a free slot waited, pauses, approvals, monitor runs (core.py lists the
                 fields). python <runner>/driver/report.py turns it into where the time and money went.
  driver.log     every decision and every session step, in words; one file a day, the older ones dated
  driver.json    the driver's state now           usage.jsonl   every session's tokens and cost
  <task>.<kind>.<model>.<time>.jsonl and <task>.<model>.<time>.md   each session's raw stream and last message
  <task>.gate.<time>.log   each gate's test output   revert-<time>/   undone files, with REASON.txt
  monitor-history.jsonl   each monitor run's record   receipt-<slot>.json   a slot's last full test run
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import commands as K  # noqa: E402
import core as C  # noqa: E402
import mainloop as M  # noqa: E402
import monitor as MO  # noqa: E402
import tasks as T  # noqa: E402


def record_start():
    """Records in driver.json the commit HEAD names in the driver's own repository, with this process's pid, so --check
    can say which code the running driver loaded. A start that finds another driver alive records nothing: it will not
    run."""
    other = C.read_pid(C.DRIVER_PID)
    if other and other != os.getpid() and C.pid_alive(other):
        return
    commit = C.driver_commit()
    with C.STATE_LOCK:
        state = C.read_driver_state()
        state.update(start_pid=os.getpid(), start_commit=commit or None)
        C.write_driver_state(state)


def refuse(problem):
    """Prints why no driver starts and ends the process; does nothing when problem is empty."""
    if problem:
        print(f"no driver started: {problem}")
        sys.exit(2)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--shutdown" in args:
        K.cli_shutdown()
        sys.exit(0)
    if "--status" in args:
        T.write_status()
        print(T.status_text())
        sys.exit(0)
    if "--monitor" in args:
        sys.exit(MO.cli_monitor([a for a in args if a != "--monitor"]))
    if "--install-monitor-schedule" in args:
        sys.exit(MO.install_schedule())
    if "--remove-monitor-schedule" in args:
        sys.exit(MO.remove_schedule())
    if "--lint-tasks" in args:
        sys.exit(T.cli_lint_tasks())
    if "--tidy" in args:
        sys.exit(T.cli_tidy())
    for flag, module in (("--check", "check_driver"), ("--simulate", "simulate"), ("--report", "report")):
        if flag in args:
            sys.argv = [module, *(a for a in args if a != flag)]
            sys.exit(__import__(module).main())
    if "--install-autostart" in args:
        M.install_autostart()
        sys.exit(0)
    if "--remove-autostart" in args:
        M.STARTUP_FILE.unlink(missing_ok=True)
        print("autostart removed")
        sys.exit(0)
    if "--autostart" in args:
        refuse(M.start_refusal([]))
        sys.exit(M.autostart())
    for flag, word in (("--model", "MODEL"), ("--approve", "APPROVE"), ("--deny", "DENY")):
        if flag in args:
            K.queue_request(" ".join([word, *args[args.index(flag) + 1:]]))
            sys.exit(0)
    if "--run" in args:
        refuse(M.start_refusal([a for a in args if a != "--run"]))
        record_start()
        sys.exit(M.run_driver())
    refuse(M.start_refusal(args))
    sys.exit(M.supervise())
