"""Shows whether the build loop's driver is running, what it is doing, who does the tasks, what waits for approval,
today's spending, and its last log lines.

Usage, from the project root:
  python <runner>/driver/check_driver.py [number of log lines, default 5]
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import commands as K  # noqa: E402
import loop_common as C  # noqa: E402
import runtime as R  # noqa: E402


def last_log_lines(count):
    """The end of today's driver.log, read from its last 200 kB."""
    if not R.DRIVER_LOG.exists():
        return []
    with open(R.DRIVER_LOG, "rb") as log:
        log.seek(0, 2)
        log.seek(max(0, log.tell() - 200000))
        return log.read().decode("utf-8", errors="replace").splitlines()[-count:]


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    driver = R.read_pid(R.DRIVER_PID)
    supervisor = R.read_pid(R.SUPERVISOR_PID)
    if driver and C.pid_alive(driver):
        watched = f", supervisor pid {supervisor}" if supervisor and C.pid_alive(supervisor) else ", no supervisor"
        print(f"RUNNING: driver pid {driver}{watched}")
    elif supervisor and C.pid_alive(supervisor):
        print(f"RESTARTING: the supervisor (pid {supervisor}) is waiting to start the driver again")
    else:
        print("NOT RUNNING: no live driver process")
    state = R.read_json(R.DRIVER_STATE)
    if state:
        print(f"  task: {state.get('task') or '-'}, paused: {bool(state.get('paused'))}, busy: {bool(state.get('busy'))}, "
              f"mode: {state.get('mode', '-')}, heartbeat: {state.get('heartbeat', '-')}")
        print(f"  {K.worker_text(state)}")
        for slot in state.get("slots") or []:
            print(f"  slot {slot['slot']}: {slot['task']} since {slot.get('started', '?')}, in {slot.get('root', '?')}")
        for code, request in (state.get("approvals") or {}).items():
            print(f"  WAITING FOR APPROVAL ({code}, asked {request.get('asked')}): {request.get('what')}")
        if state.get("resume_at"):
            print(f"  restarts by itself at {time.strftime('%a %H:%M', time.localtime(state['resume_at']))}")
    print(f"  spent today: ${C.spend_on():.2f}")
    if R.REQUEST_FILE.exists():
        waiting = [line for line in R.REQUEST_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
        print(f"  PC commands waiting for the driver: {', '.join(waiting)}")
    lines = last_log_lines(count)
    if lines:
        print("  last log lines:")
        for line in lines:
            print("    " + line[:180])


if __name__ == "__main__":
    main()
