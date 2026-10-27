"""Shows whether the driver is running, what it is doing, who does the tasks, what waits for approval, today's
spending, and its last log lines.

Run from the folder that holds worker.json (or with WORKER_CONFIG set):
  python check_driver.py [number of log lines, default 5]
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import driver as D  # noqa: E402
import worker_task as W  # noqa: E402


def last_log_lines(count):
    """The end of today's driver.log, read from its last 200 kB."""
    if not D.PATHS["log"].exists():
        return []
    with open(D.PATHS["log"], "rb") as log:
        log.seek(0, 2)
        log.seek(max(0, log.tell() - 200000))
        return log.read().decode("utf-8", errors="replace").splitlines()[-count:]


def main():
    W.configure()
    D.init_paths()
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    driver = D.read_pid(D.PATHS["pid"])
    supervisor = D.read_pid(D.PATHS["supervisor"])
    if driver and W.pid_alive(driver):
        watched = f", supervisor pid {supervisor}" if supervisor and W.pid_alive(supervisor) else ", no supervisor"
        print(f"RUNNING: driver pid {driver}{watched}")
    elif supervisor and W.pid_alive(supervisor):
        print(f"RESTARTING: the supervisor (pid {supervisor}) is waiting to start the driver again")
    else:
        print("NOT RUNNING: no live driver process")
    state = D.read_json(D.PATHS["state"])
    if state:
        print(f"  task: {state.get('task') or '-'}, paused: {bool(state.get('paused'))}, "
              f"busy: {bool(state.get('busy'))}, mode: {state.get('mode', '-')}, heartbeat: {state.get('heartbeat', '-')}")
        print(f"  {D.model_text(state)}")
        for code, request in (state.get("approvals") or {}).items():
            print(f"  WAITING FOR APPROVAL ({code}, asked {request.get('asked')}): {request.get('what')}")
        if state.get("resume_at"):
            print(f"  restarts by itself at {time.strftime('%a %H:%M', time.localtime(state['resume_at']))}")
    print(f"  spent today: ${D.spend_on():.2f}")
    if D.PATHS["request"].exists():
        waiting = [line for line in D.PATHS["request"].read_text(encoding="utf-8").splitlines() if line.strip()]
        print(f"  machine commands waiting for the driver: {', '.join(waiting)}")
    lines = last_log_lines(count)
    if lines:
        print("  last log lines:")
        for line in lines:
            print("    " + line[:180])


if __name__ == "__main__":
    main()
