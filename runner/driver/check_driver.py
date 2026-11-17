"""Shows whether the build loop's driver is running, what it is doing, who does the tasks, what waits for approval,
today's spending, and its last log lines.

Usage, from the project root:
  python <runner>/driver/check_driver.py [number of log lines, default 5]
or python <runner>/driver --check.
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import commands as K  # noqa: E402
import core as C  # noqa: E402


def last_log_lines(count):
    """The end of today's driver.log, read from its last 200 kB."""
    if not C.DRIVER_LOG.exists():
        return []
    with open(C.DRIVER_LOG, "rb") as log:
        log.seek(0, 2)
        log.seek(max(0, log.tell() - 200000))
        return log.read().decode("utf-8", errors="replace").splitlines()[-count:]


def start_text(state, driver):
    """The commit the driver with pid driver started from, as the driver recorded it at its start. A rule the driver
    reads once takes effect only after a restart, so this says whether the running one has it. A record another pid
    wrote is not this driver's."""
    if state.get("start_commit") and (not driver or state.get("start_pid") == driver):
        text = f"started from {state['start_commit'][:120]}"
        stale = stale_driver_commits(state)
        if stale:
            text += f"; {len(stale)} commits to the driver's code since: restart the driver"
        return text
    return "start commit unknown"


def stale_driver_commits(state):
    """The commits (`%h %s`) to the driver's own folder, in the driver's repository, since the commit the driver started
    from. The driver imports its modules once, so each is code the running driver does not have. Empty when the start
    is unknown, the driver is in no repository, or git fails."""
    start = (state.get("start_commit") or "").split(" ", 1)[0]
    if not start or C.DRIVER_REPO is None:
        return []
    result = C.git("log", "--format=%h %s", f"{start}..HEAD", "--", C.DRIVER_REL or ".", root=C.DRIVER_REPO)
    if result.returncode:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    driver = C.read_pid(C.DRIVER_PID)
    supervisor = C.read_pid(C.SUPERVISOR_PID)
    state = C.read_json(C.DRIVER_STATE)
    if driver and C.pid_alive(driver):
        watched = f", supervisor pid {supervisor}" if supervisor and C.pid_alive(supervisor) else ", no supervisor"
        print(f"RUNNING: driver pid {driver}{watched}, {start_text(state, driver)}")
    elif supervisor and C.pid_alive(supervisor):
        print(f"RESTARTING: the supervisor (pid {supervisor}) is waiting to start the driver again; last "
              f"{start_text(state, None)}")
    else:
        print(f"NOT RUNNING: no live driver process; last {start_text(state, None)}")
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
    print(f"  {K.T.progress_text()}")
    odd = K.T.unparsed_headings(K.T.read(K.T.tasks_file()))
    if odd:
        print(f"  unparsed headings: {len(odd)}: {'; '.join(odd[:2])}")
    print(f"  spent today: ${C.spend_on():.2f}")
    if C.REQUEST_FILE.exists():
        waiting = [line for line in C.REQUEST_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
        print(f"  PC commands waiting for the driver: {', '.join(waiting)}")
    lines = last_log_lines(count)
    if lines:
        print("  last log lines:")
        for line in lines:
            print("    " + line[:180])


if __name__ == "__main__":
    main()
