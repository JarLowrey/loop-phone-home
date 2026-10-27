"""The loop driver: runs the open tasks of a task file with Claude Code sessions, side by side in git worktrees, and
lands only work that passes its own checks. No orchestrating session: the loop is this code.

Usage, from the folder that holds worker.json (or with WORKER_CONFIG set); <runner> is this repo's runner folder:
  python <runner>/driver             the supervisor: runs the driver, and restarts it after a crash (waits longer each time)
  python <runner>/driver --run       the driver alone, with no supervisor (the supervisor starts it this way)
  python <runner>/driver --shutdown  kills the supervisor, the driver and its sessions now, undoes the unfinished tasks
                                     in their slots, and leaves the driver paused
  python <runner>/driver --model sonnet [high]   who does the tasks from the next one on (the MODEL command)
  python <runner>/driver --approve | --deny      answers every approval the driver is waiting for (the phone's buttons)
  python <runner>/driver --install-autostart     starts the driver at each logon (Windows Startup folder; elsewhere it
                                                 prints a crontab line), unless the owner ended it last
  python <runner>/driver --autostart             what the Startup file runs
  python <runner>/driver/check_driver.py         is it running, on what, and the last log lines
  python <runner>/driver/simulate.py             what parallel_slots and parallel_window would do to the queue
  python <runner>/driver/report.py               where the loop's time and money went, from <logs>/events.jsonl

Each task is done by one Claude session (task_model, sonnet by default; MODEL from the phone changes it) in a slot: a
git worktree of its own outside the project, reset to the main branch's last commit. Up to parallel_slots tasks run side
by side (scheduler.py says which may). The owner's main tree is never a task's: edits there are never judged or undone,
and the driver reads only committed tasks. A task's work lands only when the driver's own checks pass in its slot:
nothing changed outside the scope, no loop-folder file the task does not name, no test removed or making fewer asserts
unless the task names it, the tests pass, and the task's Done when line holds. Then, one slot at a time, it is rebased
onto the newest commit (added lines on both sides are merged), checked for doubled names and tested again if other work
landed meanwhile, and the main branch fast-forwards to it. Work that cannot land goes back to the queue. A task that
fails is undone and tried once more by escalate_model with the failure as feedback; a second failure blocks it. Two
blocks in a row start an Opus triage; two more after it pause the driver. A task whose Who line says Opus runs alone.

Config: worker.json (loop_common.CONFIG_DEFAULTS lists every key the driver reads). The loop file (loop_file) holds a
driver-settings block, read again at each pass, so an edit takes effect without a restart.

The modules, each with one job:
  mainloop.py   the loop, the supervisor, the autostart          runners.py   one task: session, gate, landing
  scheduler.py  which task starts next, in which slot            slots.py     worktrees, rebase, landing, push
  commands.py   the owner's commands, approvals, shutdown        tasks.py     the task and progress files, blocks, requeues
  sessions.py   Claude sessions and test runs                    budget.py    usage limits, the daily budget, reserves
  opus.py       the phone's OPUS: and OPUS WRITE:                workspace.py the gate's git checks, a task's files
  settings.py   the driver-settings block and the limits         runtime.py   driver.json, the log, pid files
  phone.py      ntfy                                             prompts.py   every prompt text
  monitor.py    the clean-checkout check and monitor_command     task_hook.py task sessions stay in their slot
  leases.py     which running task holds which file           simulate.py  the scheduler over the open queue
  events.py     <logs>/events.jsonl, the timed record             report.py    reads it: bottlenecks and waste
  packet.py     the facts a review task starts from              loop_common.py  the config, paths, git, the test run

Logs, in the logs folder (logs_dir):
  events.jsonl   every phase of every task run with its duration and outcome, why a free slot waited, pauses,
                 approvals and monitor runs (events.py lists the fields); report.py reads it
  driver.log     every decision and every session step, in words; one file a day
  driver.json    the driver's state now           usage.jsonl   every session's tokens and cost
  <task>.<kind>.<model>.<time>.jsonl and <task>.<model>.<time>.md   each session's stream and last message
  <task>.gate.<time>.log   each gate's test output   revert-<time>/   undone files, with REASON.txt
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import commands as K  # noqa: E402
import mainloop as M  # noqa: E402

if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    args = sys.argv[1:]
    if "--shutdown" in args:
        K.cli_shutdown()
        sys.exit(0)
    if "--install-autostart" in args:
        M.install_autostart()
        sys.exit(0)
    if "--remove-autostart" in args:
        M.STARTUP_FILE.unlink(missing_ok=True)
        print("autostart removed")
        sys.exit(0)
    if "--autostart" in args:
        sys.exit(M.autostart())
    for flag, word in (("--model", "MODEL"), ("--approve", "APPROVE"), ("--deny", "DENY")):
        if flag in args:
            K.queue_request(" ".join([word, *args[args.index(flag) + 1:]]))
            sys.exit(0)
    sys.exit(M.run_driver() if "--run" in args else M.supervise())
