"""The driver's own loop, the supervisor that restarts it, and the logon autostart."""
import os
import pathlib
import subprocess
import sys
import time
import traceback

import budget as B
import commands as K
import phone as P
import runners as RN
import runtime as R
import settings as S
import tasks as T
import worker_task as W

C = W.C
DRIVER_DIR = pathlib.Path(__file__).resolve().parent
STARTUP_FILE = (pathlib.Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" /
                "Startup" / "ntfy-er-driver.cmd")
ALREADY_RUNNING = 3
# the supervisor: a crash within STABLE_SECONDS of a start counts toward MAX_QUICK_CRASHES, and each one doubles the wait
RESTART_WAIT = 60
RESTART_WAIT_MAX = 1800
STABLE_SECONDS = 600
MAX_QUICK_CRASHES = 5
# why: after MAX_QUICK_CRASHES the supervisor rests this long, then tries again, so a passing fault does not end the loop
GIVE_UP_WAIT = 7200


def main(solo=True):
    """The driver: each pass reads the settings, answers commands, runs a queued triage, then the next open task, unless
    it is paused, over budget or asked to end."""
    other = R.read_pid(R.PATHS["pid"])
    if other and other != os.getpid() and W.pid_alive(other):
        print(f"a driver is already running (pid {other}). Stop it with --shutdown, or send SHUTDOWN NOW. If pid "
              f"{other} is not a driver, delete {R.PATHS['pid']}")
        return ALREADY_RUNNING
    state = R.read_state()
    state.update({"paused": False, "resume_at": None, "wind_down": False, "busy": False, "solo": solo, "approvals": {},
                  "snap": None, "ended_by": None,
                  "mode": ("model alone" if solo else "worker with Haiku review")
                  + (", supervised" if R.SUPERVISED else "")})
    R.RUNTIME["state"] = state
    R.PATHS["pid"].write_text(str(os.getpid()), encoding="utf-8", newline="\n")
    R.write_state(state)
    S.refresh()
    R.log(f"driver started ({state['mode']}), not paused. {K.model_text(state)}")
    was_paused = False
    errors_in_a_row = 0
    while True:
        S.refresh()
        K.handle_commands(state)
        if state.get("wind_down"):
            state.update({"wind_down": False, "ended_by": "owner"})
            R.write_state(state)
            P.post("Loop exited", "The driver has ended. Start it again to continue.")
            R.log("driver ends on request")
            return 0
        if state.get("triage"):
            RN.run_triage(state, state["triage"])
            continue
        if state.get("paused") and state.get("resume_at") and time.time() >= state["resume_at"]:
            state.update({"paused": False, "resume_at": None})
            R.write_state(state)
            R.log("the pause has run out: resuming")
            P.post("Loop resumed", "The usage limit or the daily budget has reset, so the driver carries on.")
        if state.get("paused") or B.over_budget(state):
            if not was_paused:
                R.log("paused: no new task starts until RESUME")
            was_paused = True
            time.sleep(S.get("poll_seconds"))
            continue
        was_paused = False
        found = T.next_task()
        if found is None:
            P.pause(state, "Loop finished", "No open tasks are left in the task file. Send RESUME after you add some.",
                    "default")
            continue
        task_id, title = found
        state.update({"busy": True, "task": task_id, "task_started": R.now()})
        R.write_state(state)
        try:
            RN.do_task(task_id, title, state, solo)
            errors_in_a_row = 0
        except Exception as error:
            errors_in_a_row += 1
            RN.task_crashed(task_id, error, errors_in_a_row, state)
        state.update({"busy": False, "task": None})
        R.set_snap(state, None)
        T.after_blocks(state)


def supervise(args):
    """Runs the driver (--run) and starts it again when it crashes. A clean end (WIND DOWN, SHUTDOWN NOW) ends the
    supervisor too. Each crash soon after a start doubles the wait; after MAX_QUICK_CRASHES it rests GIVE_UP_WAIT, then
    tries again."""
    other = K.running_driver()
    if other:
        print(f"a driver is already running (pid {other}). Stop it with --shutdown first.")
        return ALREADY_RUNNING
    R.PATHS["supervisor"].write_text(str(os.getpid()), encoding="utf-8", newline="\n")
    env = {**os.environ, "DRIVER_SUPERVISED": "1", "WORKER_CONFIG": str(C.config_path)}
    quick_crashes = 0
    try:
        while True:
            started = time.time()
            code = subprocess.call([sys.executable, str(DRIVER_DIR), "--run", *args], env=env)
            if code in (0, ALREADY_RUNNING):
                R.log(f"supervisor ends: the driver ended with exit {code}")
                return code
            quick_crashes = quick_crashes + 1 if time.time() - started < STABLE_SECONDS else 1
            if quick_crashes > MAX_QUICK_CRASHES:
                R.log(f"supervisor rests after {MAX_QUICK_CRASHES} quick crashes")
                P.post("Loop driver down", f"It crashed {MAX_QUICK_CRASHES} times soon after starting. The supervisor "
                       f"tries again in {GIVE_UP_WAIT // 3600} h. Look at the driver log, or run --shutdown on the "
                       "machine to stop it.", "urgent")
                time.sleep(GIVE_UP_WAIT)
                quick_crashes = 0
                continue
            wait = min(RESTART_WAIT * 2 ** (quick_crashes - 1), RESTART_WAIT_MAX)
            R.log(f"supervisor: the driver ended with exit {code}; restarting it in {wait} s")
            time.sleep(wait)
    finally:
        if R.read_pid(R.PATHS["supervisor"]) == os.getpid():
            R.PATHS["supervisor"].unlink(missing_ok=True)


def run_driver(solo):
    """--run: the driver itself. An error it did not catch is logged and sent to the phone, and the process ends with
    exit 1, which the supervisor restarts."""
    try:
        return main(solo)
    except KeyboardInterrupt:
        R.log("driver stopped by Ctrl+C")
        return 0
    except Exception as error:
        R.log(f"driver crashed: {error}\n{traceback.format_exc()[-1500:]}")
        then = ("The supervisor starts it again shortly." if R.SUPERVISED
                else "It is not running: start it again on the machine.")
        P.post("Loop driver crashed", f"{error}. {then}", "urgent")
        return 1
    finally:
        if R.read_pid(R.PATHS["pid"]) == os.getpid():
            R.PATHS["pid"].unlink(missing_ok=True)


def install_autostart(args):
    """Starts the driver at each logon through --autostart: a file in the Windows Startup folder, or, elsewhere, the
    crontab line to add."""
    command = f'"{sys.executable}" "{DRIVER_DIR}" --autostart {" ".join(args)}'.strip()
    if os.name != "nt":
        print(f"add this line with crontab -e:\n@reboot cd {C.project} && WORKER_CONFIG={C.config_path} {command}")
        return
    STARTUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STARTUP_FILE.write_text(f'@echo off\r\ncd /d "{C.project}"\r\nset "WORKER_CONFIG={C.config_path}"\r\n'
                            f'start "loop driver" /min {command}\r\n', encoding="utf-8")
    print(f"the driver starts at each logon unless you ended it last: {STARTUP_FILE}")


def autostart(args):
    """--autostart, at logon: starts the supervisor unless the owner ended the driver last (WIND DOWN, SHUTDOWN NOW or
    --shutdown), so a reboot or a crash resumes the loop and a deliberate stop stays stopped."""
    if R.read_state().get("ended_by") == "owner":
        R.log("autostart skipped: the owner ended the driver last")
        return 0
    R.log("autostart: starting the driver after logon")
    return supervise(args)
