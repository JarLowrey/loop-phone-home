"""Simulates the scheduler over the open queue in the task file, so a change to the queue or to the parallel settings can
be judged before the driver runs it. It drives the real Scheduler.next_start(), with leases in a temp file and no
sessions. The times come from driver.log on 2026-10-27: a worker task is about 6 minutes of session and 2 of gate, an
Opus task about 30 minutes, and a task that lands after other work landed during its gate runs the tests again (2 min).

Usage, from the project root:
  python <runner>/driver/simulate.py                 one, two and three slots, with the window from the loop file
  python <runner>/driver/simulate.py --window 5      another window
  python <runner>/driver/simulate.py --slots 2 --timeline   which tasks ran side by side
"""
import argparse
import pathlib
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import leases as L  # noqa: E402
import loop_common as C  # noqa: E402
import runners as RN  # noqa: E402
import runtime as R  # noqa: E402
import scheduler as SC  # noqa: E402
import settings as S  # noqa: E402
import slots as SL  # noqa: E402
import tasks as T  # noqa: E402
import workspace as W  # noqa: E402

WORKER_MINUTES = 6.0
GATE_MINUTES = 2.0
RETEST_MINUTES = 2.0
OPUS_MINUTES = 30.0


def run(text, slots, window, timeline=False):
    """Minutes the open queue takes with these settings, and how many landings ran the tests again."""
    with tempfile.TemporaryDirectory() as folder, mock.patch.object(SL, "LEASES", pathlib.Path(folder) / "l.json"), \
            mock.patch.object(S, "PARALLEL_SLOTS", slots), mock.patch.object(S, "PARALLEL_WINDOW", window):
        scheduler = SC.Scheduler({})
        clock, retests, landings = 0.0, 0, []
        ends = {}
        while True:
            while True:
                slot = scheduler.free_slot()
                pick = scheduler.next_start(text) if slot else None
                if not pick:
                    break
                task_id, _, spec = pick
                slot.task, slot.spec, slot.started = task_id, spec, clock
                L.start(SL.LEASES, slot.name, task_id, W.task_files(spec), S.SHARED_FILES)
                ends[slot.name] = clock + (OPUS_MINUTES if RN.is_opus_task(spec) else WORKER_MINUTES + GATE_MINUTES)
                if timeline:
                    beside = [other.task for other in scheduler.busy() if other is not slot]
                    print(f"  {clock / 60:5.2f} h  slot {slot.name}: {task_id}" + (f"  beside {', '.join(beside)}"
                                                                                  if beside else ""))
            busy = scheduler.busy()
            if not busy:
                return clock, retests
            slot = min(busy, key=lambda item: ends[item.name])
            clock = max(clock, ends[slot.name])
            if not RN.is_opus_task(slot.spec) and any(landed > ends[slot.name] - GATE_MINUTES for landed in landings):
                clock += RETEST_MINUTES
                retests += 1
            landings.append(clock)
            text = text.replace(f"### [ ] {slot.task} ·", f"### [x] {slot.task} ·", 1)
            L.release(SL.LEASES, slot.name)
            slot.task, slot.spec = None, ""


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slots", type=int, default=0)
    parser.add_argument("--window", type=int, default=0)
    parser.add_argument("--timeline", action="store_true")
    args = parser.parse_args()
    R.log = lambda message: None
    S.refresh()
    text = C.tasks_text().replace("### [~] ", "### [ ] ")
    window = args.window or S.PARALLEL_WINDOW
    print(f"{len(T.open_tasks(text))} open tasks (gated ones counted), window {window}, shared files: "
          f"{', '.join(path.rsplit('/', 1)[-1] for path in S.SHARED_FILES)}")
    serial = run(text, 1, window)[0]
    for slots in [args.slots] if args.slots else [1, 2, 3]:
        minutes, retests = run(text, slots, window, args.timeline)
        print(f"{slots} slot(s): {minutes / 60:5.1f} h, {100 * (1 - minutes / serial):3.0f}% less than one slot, "
              f"{retests} landings tested again")


if __name__ == "__main__":
    main()
