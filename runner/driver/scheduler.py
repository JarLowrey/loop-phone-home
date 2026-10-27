"""The scheduler: which open task starts next, in which slot. When nothing runs, the first open task starts. Otherwise
it looks at the next parallel_window open tasks that are not running, in file order, and starts the first one that
- is not solo() while nothing solo runs, and passes no solo task waiting before it;
- waits on nothing still open: its **After.** line, the earlier open tasks whose ids it names or that name it ("M3-11
  builds on this"), and the tasks whose files it was refused last time (state "waits");
- shares no file with a running task (its Files line and the files its session took since), nor with an earlier task
  it would pass, so every file still changes in file order. A shared file is the exception, while the two tasks' specs
  name no function of it in common (two tasks that both change end_fight never run together).
Each task runs in its own thread, in its own slot; the main thread's loop answers the phone and calls fill() and wait()."""
import re
import threading

import events as E
import leases as L
import loop_common as C
import runners as RN
import runtime as R
import settings as S
import slots as SL
import tasks as T
import workspace as W

AFTER_LINE = re.compile(r"^\*\*After\.\*\*(.*)$", re.M)
AFTER_ID = C.TASK_ID


def after(spec):
    """The task ids a task's **After.** line names: work it builds on that its Files line cannot show."""
    match = AFTER_LINE.search(spec)
    return AFTER_ID.findall(match.group(1)) if match else []


class Scheduler:
    def __init__(self, state):
        self.state = state
        self.slots = {}
        self.finished = threading.Event()
        self.errors_in_a_row = 0
        # why no task starts in a free slot, as next_start() last found it, and the last one recorded in events.jsonl
        self.wait_reason = ""
        self.wait_next = None
        self.recorded_wait = None
        self.resize()

    def resize(self):
        """One slot per parallel_slots. A slot past the count is dropped once it is idle."""
        for number in range(1, S.PARALLEL_SLOTS + 1):
            self.slots.setdefault(str(number), SL.Slot(number))
        for name in [name for name in self.slots if int(name) > S.PARALLEL_SLOTS and not self.slots[name].task]:
            del self.slots[name]

    def busy(self):
        return [slot for slot in self.slots.values() if slot.task]

    def idle(self):
        return not self.busy()

    def free_slot(self):
        return next((self.slots[str(number)] for number in range(1, S.PARALLEL_SLOTS + 1)
                     if str(number) in self.slots and not self.slots[str(number)].task), None)

    def slot(self, name):
        return self.slots.setdefault(name, SL.Slot(name))

    def solo(self, task_id, spec):
        """True for a task that runs only when nothing else does: an Opus task, a task whose Files line names nothing
        (so its files cannot be known), and one that could not land max_requeues times."""
        requeued = (self.state.get("requeues") or {}).get(task_id, 0)
        return RN.is_opus_task(spec) or not W.task_files(spec) or requeued >= S.MAX_REQUEUES

    def depends(self, task_id, spec, earlier, open_ids):
        """The open tasks this one must wait for: its After line, the earlier open tasks it names or that name it, and
        the tasks whose files it was refused."""
        waits = set(after(spec)) | set((self.state.get("waits") or {}).get(task_id, []))
        waits |= {other for other, other_spec in earlier if other in W.mentioned_tasks(spec)
                  or task_id in W.mentioned_tasks(other_spec)}
        return waits & open_ids

    def conflicts(self, spec, other_spec):
        """The files two tasks may not change at the same time: any file both name, except a shared file whose
        functions the two specs do not both name."""
        found = []
        for mine in W.task_files(spec):
            for theirs in W.task_files(other_spec):
                if not (L.covers(mine, theirs) or L.covers(theirs, mine)):
                    continue
                path = theirs if mine.endswith("/") else mine
                if any(L.covers(shared, path) for shared in S.SHARED_FILES) and not (
                        W.named_functions(spec, path) & W.named_functions(other_spec, path)):
                    continue
                found.append(path)
        return found

    def next_start(self, text):
        """(id, title, spec) of the task to start now, or None to wait. When it waits, wait_reason says why, for each
        task it looked at, and wait_next names the first of them."""
        running = {slot.task: slot.spec for slot in self.busy()}
        opened = T.open_tasks(text)
        waiting = [item for item in opened if item[0] not in running]
        self.wait_next = waiting[0][0] if waiting else None
        if not waiting:
            return self.waits("no open task is waiting")
        if not running:
            task_id, title = waiting[0]
            return task_id, title, C.find_spec(task_id, text)
        solo = [other for other, other_spec in running.items() if self.solo(other, other_spec)]
        if solo:
            return self.waits(f"{solo[0]} runs alone")
        order = [item[0] for item in opened]
        open_ids = set(order)
        oldest = min(order.index(other) for other in running if other in order) if set(running) & open_ids else 0
        leases = L.read(SL.LEASES)
        passed = []
        reasons = []
        for task_id, title in waiting[:S.PARALLEL_WINDOW]:
            if order.index(task_id) - oldest >= S.PARALLEL_WINDOW:
                reasons.append(f"{task_id}: outside the window")
                break
            spec = C.find_spec(task_id, text)
            if self.solo(task_id, spec):
                reasons.append(f"{task_id}: runs alone, so it waits for every slot to be idle")
                break
            earlier = [(other, other_spec) for other, other_spec in running.items()] + passed
            depends = self.depends(task_id, spec, earlier, open_ids)
            clash = None
            for other, other_spec in earlier:
                files = self.conflicts(spec, other_spec)
                if files:
                    clash = (other, files)
                    break
            held = L.overlap(leases, [path for path in W.task_files(spec)
                                      if not any(L.covers(shared, path) for shared in S.SHARED_FILES)])
            if not (depends or clash or held):
                return task_id, title, spec
            if depends:
                reasons.append(f"{task_id}: waits for {', '.join(sorted(depends))} to land")
            elif clash:
                reasons.append(f"{task_id}: shares {clash[1][0].rsplit('/', 1)[-1]} with {clash[0]}")
            else:
                reasons.append(f"{task_id}: {held[0][0].rsplit('/', 1)[-1]} is held by {held[0][2]}")
            passed.append((task_id, spec))
        return self.waits("; ".join(reasons) or "nothing in the window can start")

    def waits(self, reason):
        self.wait_reason = reason
        return None

    def fill(self):
        """Starts tasks in free slots while next_start() allows one."""
        text = C.tasks_text()
        while True:
            slot = self.free_slot()
            pick = self.next_start(text) if slot else None
            if not pick:
                if slot:
                    self.record_wait()
                return
            self.recorded_wait = None
            self.start(slot, *pick)

    def record_wait(self):
        """Records once, in events.jsonl, why a free slot stays idle, each time the reason or the running tasks change:
        the data an audit needs to see what keeps a slot from working."""
        running = sorted(slot.task for slot in self.busy())
        key = (self.wait_reason, tuple(running))
        if key == self.recorded_wait:
            return
        self.recorded_wait = key
        free = sum(1 for number in range(1, S.PARALLEL_SLOTS + 1) if not self.slots[str(number)].task)
        E.emit("schedule_wait", free_slots=free, next=self.wait_next, reason=self.wait_reason, running=running)

    def start(self, slot, task_id, title, spec):
        slot.task = task_id
        slot.spec = spec
        slot.started = R.now()
        slot.base = None
        SL.lease(slot, task_id, spec)
        self.save()
        slot.thread = threading.Thread(target=self.run, args=(slot, task_id, title, spec), daemon=True,
                                       name=f"slot-{slot.name}")
        slot.thread.start()

    def run(self, slot, task_id, title, spec):
        """A slot's thread: does the task, and whatever happens, frees the slot and wakes the main loop. Its start and
        its end, with the outcome and the wall time, go to events.jsonl."""
        with R.STATE_LOCK:
            runs = self.state.setdefault("runs", {})
            runs[task_id] = runs.get(task_id, 0) + 1
        R.ctx().clear()
        R.ctx().update(slot=slot.name, task=task_id, run=runs[task_id],
                       prefix=f"[slot {slot.name}] " if S.PARALLEL_SLOTS > 1 else "")
        timer = E.Timer()
        E.emit("task_start", title=title, files=W.task_files(spec), solo=self.solo(task_id, spec),
               base=SL.main_tip()[:10])
        try:
            RN.do_task(slot, task_id, title, spec, self.state)
            self.errors_in_a_row = 0
        except Exception as error:
            self.errors_in_a_row += 1
            R.ctx()["outcome"] = R.ctx().get("outcome") or "crashed"
            R.ctx()["reason"] = R.ctx().get("reason") or str(error)[:300]
            RN.task_crashed(slot, task_id, title, error, self.errors_in_a_row, self.state)
        finally:
            outcome = R.ctx().get("outcome") or ("stopped" if R.RUNTIME.get("stopping") else "paused")
            E.emit("task_end", outcome=outcome, reason=R.ctx().get("reason", ""), seconds=timer.seconds())
            SL.release(slot)
            slot.task = None
            with R.STATE_LOCK:
                T.after_blocks(self.state)
                self.save()
            self.finished.set()

    def save(self):
        """Keeps the busy slots in driver.json: what STATUS shows and what --shutdown from another process reverts."""
        with R.STATE_LOCK:
            busy = self.busy()
            self.state["slots"] = [slot.record() for slot in busy]
            self.state["busy"] = bool(busy)
            self.state["task"] = ", ".join(slot.task for slot in busy) or None
            R.write_driver_state(self.state)

    def wait(self, seconds):
        """Sleeps until a slot finishes or seconds pass."""
        self.finished.wait(seconds)
        self.finished.clear()
