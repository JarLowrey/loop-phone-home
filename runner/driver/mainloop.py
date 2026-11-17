"""The driver's own loop, the supervisor that restarts it, and the logon autostart."""
import os
import pathlib
import subprocess
import sys
import time
import traceback

import budget as B
import commands as K
import leases as L
import core as C
import monitor as MO
import opus as O
import scheduler as SC
import slots as SL
import tasks as T

DRIVER_DIR = pathlib.Path(__file__).resolve().parent
STARTUP_FILE = (pathlib.Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" /
                "Startup" / f"{C.PROJECT.name}-driver.cmd")
ALREADY_RUNNING = 3
USAGE_HINT = (f"python {DRIVER_DIR.name} alone starts the driver; its other commands are listed at the top of its "
              f"__main__.py. To run the driver's tests: python -m unittest discover -s {DRIVER_DIR / 'tests'}")
RIVAL_TEXT = ("no driver started: another driver (pid {pid}) holds the slots in {slots}. Two drivers on one slots "
              "folder reset each other's work. Stop it first: python <runner>/driver --shutdown, or end that process. If "
              "none is running, delete {owner}.")
# why: the driver ends with this once a commit changed the driver's own code, so the supervisor starts it again at once on the
# new code. Python reads the driver's modules only at start: a fix a triage lands is not used until then
RESTART_FOR_CODE = 4
# the supervisor: a crash within STABLE_SECONDS of a start counts toward MAX_QUICK_CRASHES, and each one doubles the wait
RESTART_WAIT = 60
RESTART_WAIT_MAX = 1800
STABLE_SECONDS = 600
MAX_QUICK_CRASHES = 5
# why: after MAX_QUICK_CRASHES the supervisor rests this long, then tries again, so a passing fault does not end the loop
GIVE_UP_WAIT = 7200


def main():
    """The driver: each pass reads the settings, answers commands, then fills the free slots with the next open tasks,
    unless it is paused, over budget or asked to end. OPUS WRITE and the triage wait until every slot is idle."""
    other = C.read_pid(C.DRIVER_PID)
    if other and other != os.getpid() and C.pid_alive(other):
        print(f"a driver is already running (pid {other}). Stop it with python <runner>/driver --shutdown first.")
        return ALREADY_RUNNING
    if not C.SUPERVISED:
        rival = SL.other_owner()
        if rival:
            C.log(RIVAL_TEXT.format(pid=rival, slots=SL.SLOTS_DIR, owner=SL.owner_file()))
            return ALREADY_RUNNING
        SL.claim_slots()
    branch = C.main_branch()
    if branch in ("", "HEAD"):
        print("the main tree is on no branch. Check out the branch the driver should land work on, then start it.")
        return 1
    C.RUNTIME["branch"] = branch
    C.refresh()
    state = C.read_driver_state()
    C.RUNTIME["state"] = state
    # why: a fresh start always runs: a pause saved by an earlier shutdown must not hold the driver idle. A restart for
    # new driver code is no fresh start, so a pause in force then is kept
    kept_pause = bool(state.pop("restart_paused", False))
    state.update(paused=kept_pause, busy=False, task=None, approvals={}, exit_after=False, slots=[], ended_by=None,
                 branch=branch, mode="supervised" if C.SUPERVISED else "alone", told_code_changed=False)
    state.pop("snap", None)
    C.write_driver_state(state)
    L.write(SL.LEASES, {})
    C.DRIVER_PID.write_text(str(os.getpid()), encoding="utf-8", newline="\n")
    scheduler = SC.Scheduler(state)
    C.RUNTIME["scheduler"] = scheduler
    C.RUNTIME["driver_tree"] = C.driver_tree()
    C.log(f"driver started ({state['mode']}) on {branch}, {C.PARALLEL_SLOTS} slot(s) in {SL.SLOTS_DIR}, "
          f"{'paused' if kept_pause else 'not paused'}. {K.worker_text(state)}")
    C.emit("driver_start", pid=os.getpid(), branch=branch, commit=C.head()[:10], settings=dict(C.LAST_SETTINGS),
           worker=K.worker_text(state))
    was_paused = False
    while True:
        C.refresh()
        scheduler.resize()
        K.handle_commands(state)
        C.write_driver_state(state)
        if state.get("paused") and not was_paused:
            C.log("paused: no new task starts until RESUME is sent")
        was_paused = bool(state.get("paused"))
        if state.get("exit_after"):
            if not scheduler.idle() or MO.running():
                scheduler.wait(C.POLL_SECONDS)
                continue
            state["exit_after"] = False
            state["ended_by"] = "owner"
            C.write_driver_state(state)
            O.wait_for_opus()
            T.send_digest(state)
            C.post("Loop exited", "The driver has ended. Start it on the PC with python <runner>/driver to continue.")
            C.log("driver exits on request")
            C.emit("driver_stop", reason="WIND DOWN")
            return 0
        if code_changed() and scheduler.idle() and not MO.running():
            if C.SUPERVISED:
                restart_for_code(state)
                return RESTART_FOR_CODE
            if not state.get("told_code_changed"):
                state["told_code_changed"] = True
                C.log("the driver's code changed since it started; it runs without a supervisor, so restart it")
                C.post("Driver code changed", "A commit changed the driver's code since the driver started. It has no "
                       "supervisor, so restart it on the PC to use the new code.", "high")
        if state.get("opus_writes"):
            # why: an OPUS WRITE runs even while paused, because the person asked for it; it never runs beside a task
            if scheduler.idle():
                O.run_opus_write(state, state["opus_writes"].pop(0), scheduler.slot("1"))
                C.write_driver_state(state)
                continue
            scheduler.wait(C.POLL_SECONDS)
            continue
        if state.get("paused") and state.get("resume_at") and time.time() >= state["resume_at"]:
            state["paused"] = False
            state["resume_at"] = None
            C.write_driver_state(state)
            C.log("the pause has run out: resuming")
            C.emit("resume", reason="the pause ran out")
            C.post("Loop resumed", "The usage limit or the daily budget has reset, so the driver carries on.")
        if not state.get("paused") and not B.over_budget(state):
            scheduler.fill()
            if scheduler.idle() and T.next_task() is None:
                T.send_digest(state)
                C.pause(state, "Loop finished", T.queue_summary(), "default")
                continue
        elif scheduler.free_slot():
            scheduler.record_idle("paused" if state.get("paused") else "over the daily budget")
        if MO.due(state) and not state.get("paused") and not MO.running():
            MO.start(state)
        scheduler.wait(C.POLL_SECONDS)


def code_changed():
    """True when a commit since the driver started changed the driver's folder (in the driver's own repository, which
    may be the project's): the code running is not the code committed."""
    started = C.RUNTIME.get("driver_tree")
    now = C.driver_tree()
    return bool(started and now and now != started)


def restart_for_code(state):
    """Ends the driver so the supervisor starts it on the new code. A pause in force is kept across it."""
    state["restart_paused"] = bool(state.get("paused"))
    C.write_driver_state(state)
    C.log("the driver's code changed since it started: restarting to load the new code")
    C.post("Driver restarting", "A commit changed the driver's code, so the driver restarts to use the new code. Nothing "
           "was running.")
    C.emit("driver_stop", reason="driver code changed")


def start_refusal(args):
    """Why the command line must not start a driver ('' when it may): a flag no branch of __main__ knows, which would
    start one by falling through (a mistyped flag once did), or a place a driver must not start from."""
    if args:
        return f"unknown argument {' '.join(args)}. {USAGE_HINT}"
    return C.start_problem()


def supervise():
    """Runs the driver (--run) and starts it again when it crashes. A clean end (WIND DOWN, SHUTDOWN NOW) ends the
    supervisor too. Each crash soon after a start doubles the wait; after MAX_QUICK_CRASHES it rests GIVE_UP_WAIT, then
    tries again."""
    other = C.read_pid(C.SUPERVISOR_PID) or C.read_pid(C.DRIVER_PID)
    if other and other != os.getpid() and C.pid_alive(other):
        print(f"a driver is already running (pid {other}). Stop it with python <runner>/driver --shutdown first.")
        return ALREADY_RUNNING
    rival = SL.other_owner()
    if rival:
        C.log(RIVAL_TEXT.format(pid=rival, slots=SL.SLOTS_DIR, owner=SL.owner_file()))
        return ALREADY_RUNNING
    C.LOGS.mkdir(parents=True, exist_ok=True)
    SL.claim_slots()
    C.SUPERVISOR_PID.write_text(str(os.getpid()), encoding="utf-8", newline="\n")
    quick_crashes = 0
    try:
        while True:
            started = time.time()
            code = subprocess.call([sys.executable, str(DRIVER_DIR), "--run"], cwd=C.PROJECT,
                                   env={**os.environ, "DRIVER_SUPERVISED": "1",
                                        "WORKER_CONFIG": str(C.CFG.config_path)})
            if code in (0, ALREADY_RUNNING):
                C.log(f"supervisor ends: the driver ended with exit {code}")
                return code
            if code == RESTART_FOR_CODE:
                C.log("supervisor: the driver ended to load new code; starting it again now")
                quick_crashes = 0
                continue
            quick_crashes = quick_crashes + 1 if time.time() - started < STABLE_SECONDS else 1
            if quick_crashes > MAX_QUICK_CRASHES:
                C.log(f"supervisor rests after {MAX_QUICK_CRASHES} quick crashes")
                C.post("Loop driver down", f"It crashed {MAX_QUICK_CRASHES} times soon after starting. The supervisor "
                       f"tries again in {GIVE_UP_WAIT // 3600} h. Look at {C.LOGS_REL}driver.log; send nothing to wait, "
                       "or run python <runner>/driver --shutdown on the PC to stop it.", "urgent")
                time.sleep(GIVE_UP_WAIT)
                quick_crashes = 0
                continue
            wait = min(RESTART_WAIT * 2 ** (quick_crashes - 1), RESTART_WAIT_MAX)
            C.log(f"supervisor: the driver ended with exit {code}; restarting it in {wait} s")
            time.sleep(wait)
    finally:
        SL.release_slots()
        if C.read_pid(C.SUPERVISOR_PID) == os.getpid():
            C.SUPERVISOR_PID.unlink(missing_ok=True)


def run_driver():
    """--run: the driver itself. An error it did not catch is logged and sent to the phone, and the process ends with
    exit 1, which the supervisor restarts."""
    try:
        return main()
    except (KeyboardInterrupt, SystemExit):
        C.log("driver stopped")
        raise
    except Exception as error:
        C.log(f"driver crashed: {error}\n{traceback.format_exc()[-1500:]}")
        C.emit("driver_stop", reason=f"crashed: {str(error)[:300]}")
        then = ("The supervisor starts it again shortly." if C.SUPERVISED
                else "It is not running: start it on the PC with python <runner>/driver.")
        C.post("Loop driver crashed", f"{error}. {then}", "urgent")
        return 1
    finally:
        if not C.SUPERVISED:
            SL.release_slots()
        if C.read_pid(C.DRIVER_PID) == os.getpid():
            C.DRIVER_PID.unlink(missing_ok=True)


def install_autostart():
    """Starts the driver at each logon through --autostart: on Windows, a file in the Startup folder that starts it
    minimised; elsewhere it prints the crontab line to add."""
    config = C.CFG.config_path
    if os.name != "nt":
        print(f'add this line with crontab -e:\n@reboot cd "{C.PROJECT}" && WORKER_CONFIG="{config}" '
              f'"{sys.executable}" "{DRIVER_DIR}" --autostart')
        return
    STARTUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STARTUP_FILE.write_text(f'@echo off\r\ncd /d "{C.PROJECT}"\r\nset "WORKER_CONFIG={config}"\r\nstart '
                            f'"{C.PROJECT.name} driver" /min "{sys.executable}" "{DRIVER_DIR}" --autostart\r\n',
                            encoding="utf-8")
    print(f"the driver starts at each logon unless you ended it last: {STARTUP_FILE}")


def autostart():
    """--autostart, at logon: starts the supervisor unless the owner ended the driver last (WIND DOWN, SHUTDOWN NOW or
    --shutdown), so a reboot or a crash resumes the loop and a deliberate stop stays stopped."""
    if C.read_driver_state().get("ended_by") == "owner":
        C.log("autostart skipped: the owner ended the driver last")
        return 0
    C.log("autostart: starting the driver after logon")
    return supervise()
