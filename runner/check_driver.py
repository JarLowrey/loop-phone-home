"""Shows whether the driver is running, what it is doing, and its last log lines.

Run from the folder that holds worker.json (or with WORKER_CONFIG set):
  python check_driver.py [number of log lines, default 5]
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import driver as D  # noqa: E402
import worker_task as W  # noqa: E402


def main():
    W.configure()
    D.init_paths()
    lines = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    other = D.running_driver()
    print(f"RUNNING: driver pid {other}" if other else "NOT RUNNING: no live driver process")
    state = D.read_json(D.PATHS["state"])
    if state:
        print(f"  task: {state.get('task') or '-'}, paused: {bool(state.get('paused'))}, "
              f"busy: {bool(state.get('busy'))}, mode: {state.get('mode', '-')}")
        if state.get("resume_at"):
            import time
            print(f"  restarts by itself at {time.strftime('%a %H:%M', time.localtime(state['resume_at']))}")
    if D.PATHS["log"].exists():
        log = D.PATHS["log"].read_text(encoding="utf-8", errors="replace").splitlines()
        print("  last log lines:")
        for line in log[-lines:]:
            print("    " + line[:180])


if __name__ == "__main__":
    main()
