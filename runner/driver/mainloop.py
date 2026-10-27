"""The driver's own loop, the supervisor that restarts it, and the logon autostart."""
import os
import pathlib
import subprocess
import sys
import time
import traceback

import budget as B
import commands as K
import events as E
import leases as L
import loop_common as C
import monitor as BW
import opus as O
import phone as P
import runtime as R
import scheduler as SC
import settings as S
import slots as SL
import tasks as T

DRIVER_DIR = pathlib.Path(__file__).resolve().parent
STARTUP_FILE = (pathlib.Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" /
                "Startup" / f"{C.PROJECT.name}-driver.cmd")
ALREADY_RUNNING = 3
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
    other = R.read_pid(R.DRIVER_PID)
    if other and other != os.getpid() and C.pid_alive(other):
        print(f"a driver is already running (pid {other}). Stop it with python <runner>/driver --shutdown first.")
        return ALREADY_RUNNING
    branch = C.main_branch()
    if branch in ("", "HEAD"):
        print("the main tree is on no branch. Check out the branch the driver should land work on, then start it.")
        return 1
    R.RUNTIME["branch"] = branch
    S.refresh()
    state = R.read_driver_state()
    R.RUNTIME["state"] = state
    # why: a fresh start always runs: a pause saved by an earlier shutdown must not hold the driver idle
    state.update(paused=False, busy=False, task=None, approvals={}, exit_after=False, slots=[], ended_by=None,
                 branch=branch, mode="supervised" if R.SUPERVISED else "alone")
    state.pop("snap", None)
    R.write_driver_state(state)
    L.write(SL.LEASES, {})
    R.DRIVER_PID.write_text(str(os.getpid()), encoding="utf-8", newline="\n")
    scheduler = SC.Scheduler(state)
    R.RUNTIME["scheduler"] = scheduler
    R.log(f"driver started ({state['mode']}) on {branch}, {S.PARALLEL_SLOTS} slot(s) in {SL.SLOTS_DIR}, not paused. "
          f"{K.worker_text(state)}")
    E.emit("driver_start", pid=os.getpid(), branch=branch, commit=C.head()[:10], settings=dict(S.LAST_SETTINGS),
           worker=K.worker_text(state))
    was_paused = False
    while True:
        S.refresh()
        scheduler.resize()
        K.handle_commands(state)
        R.write_driver_state(state)
        if state.get("paused") and not was_paused:
            R.log("paused: no new task starts until RESUME is sent")
        was_paused = bool(state.get("paused"))
        if state.get("exit_after"):
            if not scheduler.idle() or BW.running():
                scheduler.wait(S.POLL_SECONDS)
                continue
            state["exit_after"] = False
            state["ended_by"] = "owner"
            R.write_driver_state(state)
            O.wait_for_opus()
            T.send_digest(state)
            P.post("Loop exited", "The driver has ended. Start it on the PC with python <runner>/driver to continue.")
            R.log("driver exits on request")
            E.emit("driver_stop", reason="WIND DOWN")
            return 0
        if state.get("opus_writes"):
            # why: an OPUS WRITE runs even while paused, because the person asked for it; it never runs beside a task
            if scheduler.idle():
                O.run_opus_write(state, state["opus_writes"].pop(0), scheduler.slot("1"))
                R.write_driver_state(state)
                continue
            scheduler.wait(S.POLL_SECONDS)
            continue
        if state.get("paused") and state.get("resume_at") and time.time() >= state["resume_at"]:
            state["paused"] = False
            state["resume_at"] = None
            R.write_driver_state(state)
            R.log("the pause has run out: resuming")
            E.emit("resume", reason="the pause ran out")
            P.post("Loop resumed", "The usage limit or the daily budget has reset, so the driver carries on.")
        if not state.get("paused") and not B.over_budget(state):
            scheduler.fill()
            if scheduler.idle() and T.next_task() is None:
                T.send_digest(state)
                R.pause(state, "Loop finished", T.queue_summary(), "default")
                continue
        if BW.due(state) and not state.get("paused") and not BW.running():
            BW.start(state)
        scheduler.wait(S.POLL_SECONDS)


def supervise():
    """Runs the driver (--run) and starts it again when it crashes. A clean end (WIND DOWN, SHUTDOWN NOW) ends the
    supervisor too. Each crash soon after a start doubles the wait; after MAX_QUICK_CRASHES it rests GIVE_UP_WAIT, then
    tries again."""
    other = R.read_pid(R.SUPERVISOR_PID) or R.read_pid(R.DRIVER_PID)
    if other and other != os.getpid() and C.pid_alive(other):
        print(f"a driver is already running (pid {other}). Stop it with python <runner>/driver --shutdown first.")
        return ALREADY_RUNNING
    C.LOGS.mkdir(parents=True, exist_ok=True)
    R.SUPERVISOR_PID.write_text(str(os.getpid()), encoding="utf-8", newline="\n")
    quick_crashes = 0
    try:
        while True:
            started = time.time()
            code = subprocess.call([sys.executable, str(DRIVER_DIR), "--run"], cwd=C.PROJECT,
                                   env={**os.environ, "DRIVER_SUPERVISED": "1",
                                        "WORKER_CONFIG": str(C.CFG.config_path)})
            if code in (0, ALREADY_RUNNING):
                R.log(f"supervisor ends: the driver ended with exit {code}")
                return code
            quick_crashes = quick_crashes + 1 if time.time() - started < STABLE_SECONDS else 1
            if quick_crashes > MAX_QUICK_CRASHES:
                R.log(f"supervisor rests after {MAX_QUICK_CRASHES} quick crashes")
                P.post("Loop driver down", f"It crashed {MAX_QUICK_CRASHES} times soon after starting. The supervisor "
                       f"tries again in {GIVE_UP_WAIT // 3600} h. Look at driver.log in the logs folder; send nothing to wait, "
                       "or run python <runner>/driver --shutdown on the PC to stop it.", "urgent")
                time.sleep(GIVE_UP_WAIT)
                quick_crashes = 0
                continue
            wait = min(RESTART_WAIT * 2 ** (quick_crashes - 1), RESTART_WAIT_MAX)
            R.log(f"supervisor: the driver ended with exit {code}; restarting it in {wait} s")
            time.sleep(wait)
    finally:
        if R.read_pid(R.SUPERVISOR_PID) == os.getpid():
            R.SUPERVISOR_PID.unlink(missing_ok=True)


def run_driver():
    """--run: the driver itself. An error it did not catch is logged and sent to the phone, and the process ends with
    exit 1, which the supervisor restarts."""
    try:
        return main()
    except (KeyboardInterrupt, SystemExit):
        R.log("driver stopped")
        raise
    except Exception as error:
        R.log(f"driver crashed: {error}\n{traceback.format_exc()[-1500:]}")
        E.emit("driver_stop", reason=f"crashed: {str(error)[:300]}")
        then = ("The supervisor starts it again shortly." if R.SUPERVISED
                else "It is not running: start it on the PC with python <runner>/driver.")
        P.post("Loop driver crashed", f"{error}. {then}", "urgent")
        return 1
    finally:
        if R.read_pid(R.DRIVER_PID) == os.getpid():
            R.DRIVER_PID.unlink(missing_ok=True)


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
    if R.read_driver_state().get("ended_by") == "owner":
        R.log("autostart skipped: the owner ended the driver last")
        return 0
    R.log("autostart: starting the driver after logon")
    return supervise()
