"""The scheduler: which open task starts next, in which slot. When nothing runs, the first open task starts. Otherwise
it looks at the next parallel_window open tasks that are not running, in file order, and starts the first one that
- is not solo() while nothing solo runs, and passes no solo task waiting before it;
- waits on nothing still open: its **After.** line, the earlier open tasks whose ids it names or that name it ("T-11
  builds on this"), and the tasks whose files it was refused last time (state "waits");
- shares no file with a running task (its Files line and the files its session took since), nor with an earlier task
  it would pass, so every file still changes in file order. A shared file is the exception, while the two tasks' specs
  name no function of it in common (two tasks that both change parse_config never run together).
A task whose worker failed and that waits for the escalation model (state "escalations") comes first: nothing new starts
until every slot is idle, and then it runs alone.
Each task runs in its own thread, in its own slot; the main thread's loop answers the phone and calls fill() and wait()."""
import re
import threading

import leases as L
import core as C
import runners as RN
import slots as SL
import tasks as T

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
        for number in range(1, C.PARALLEL_SLOTS + 1):
            self.slots.setdefault(str(number), SL.Slot(number))
        for name in [name for name in self.slots if int(name) > C.PARALLEL_SLOTS and not self.slots[name].task]:
            del self.slots[name]

    def busy(self):
        return [slot for slot in self.slots.values() if slot.task]

    def idle(self):
        return not self.busy()

    def free_slot(self):
        return next((self.slots[str(number)] for number in range(1, C.PARALLEL_SLOTS + 1)
                     if str(number) in self.slots and not self.slots[str(number)].task), None)

    def slot(self, name):
        return self.slots.setdefault(name, SL.Slot(name))

    def solo(self, task_id, spec):
        """True for a task that runs only when nothing else does: an Opus task, a task whose Files line names nothing
        (so its files cannot be known), and one that could not land max_requeues times."""
        requeued = (self.state.get("requeues") or {}).get(task_id, 0)
        return (T.is_senior_task(spec) or not SL.task_files(spec) or requeued >= C.MAX_REQUEUES
                or task_id in (self.state.get("escalations") or {}))

    def depends(self, task_id, spec, earlier, open_ids):
        """The open tasks this one must wait for: its After line, the earlier open tasks it names or that name it, and
        the tasks whose files it was refused."""
        waits = set(after(spec)) | set((self.state.get("waits") or {}).get(task_id, []))
        waits |= {other for other, other_spec in earlier if other in SL.mentioned_tasks(spec)
                  or task_id in SL.mentioned_tasks(other_spec)}
        return waits & open_ids

    def conflicts(self, spec, other_spec):
        """The files two tasks may not change at the same time: any file both name, except a shared file whose
        functions the two specs do not both name."""
        found = []
        for mine in SL.task_files(spec):
            for theirs in SL.task_files(other_spec):
                if not (L.covers(mine, theirs) or L.covers(theirs, mine)):
                    continue
                path = theirs if mine.endswith("/") else mine
                if any(L.covers(shared, path) for shared in C.SHARED_FILES) and not (
                        SL.named_functions(spec, path) & SL.named_functions(other_spec, path)):
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
        escalated = [item for item in waiting if item[0] in (self.state.get("escalations") or {})]
        if escalated:
            task_id, title = escalated[0]
            if running:
                return self.waits(f"{task_id} waits for every slot to be idle, then {C.ESCALATE_MODEL} runs it alone")
            return task_id, title, C.find_spec(task_id, text)
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
        for task_id, title in waiting[:C.PARALLEL_WINDOW]:
            if order.index(task_id) - oldest >= C.PARALLEL_WINDOW:
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
            held = L.overlap(leases, [path for path in SL.task_files(spec)
                                      if not any(L.covers(shared, path) for shared in C.SHARED_FILES)])
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

    def record_idle(self, reason):
        """Records why free slots sit idle when fill() is not even asked: the driver is paused or over its budget.
        Without this the idle time has no reason in events.jsonl, and the usage report cannot explain it."""
        self.wait_reason = reason
        self.record_wait()

    def record_wait(self):
        """Records once, in events.jsonl, why a free slot stays idle, each time the reason or the running tasks change:
        the data an audit needs to see what keeps a slot from working."""
        running = sorted(slot.task for slot in self.busy())
        key = (self.wait_reason, tuple(running))
        if key == self.recorded_wait:
            return
        self.recorded_wait = key
        free = sum(1 for number in range(1, C.PARALLEL_SLOTS + 1) if not self.slots[str(number)].task)
        C.emit("schedule_wait", free_slots=free, next=self.wait_next, reason=self.wait_reason, running=running)

    def start(self, slot, task_id, title, spec):
        slot.task = task_id
        slot.spec = spec
        slot.started = C.now()
        slot.base = None
        fault = SL.precheck(spec)
        if fault:
            self.save()
            slot.thread = threading.Thread(target=self.refuse, args=(slot, task_id, title, fault), daemon=True,
                                           name=f"slot-{slot.name}")
            slot.thread.start()
            return
        SL.lease(slot, task_id, spec)
        self.save()
        slot.thread = threading.Thread(target=self.run, args=(slot, task_id, title, spec), daemon=True,
                                       name=f"slot-{slot.name}")
        slot.thread.start()

    def refuse(self, slot, task_id, title, fault):
        """A slot's thread for a task whose text shows a fault: blocks it with the fault as the reason, starts no
        session, and frees the slot."""
        C.ctx().clear()
        C.ctx().update(slot=slot.name, task=task_id, prefix=f"[slot {slot.name}] " if C.PARALLEL_SLOTS > 1 else "")
        try:
            T.block(slot, task_id, title, fault, "refused")
        finally:
            slot.task = None
            with C.STATE_LOCK:
                T.after_blocks(self.state)
                self.save()
            self.finished.set()

    def run(self, slot, task_id, title, spec):
        """A slot's thread: does the task, and whatever happens, frees the slot and wakes the main loop. Its start and
        its end, with the outcome and the wall time, go to events.jsonl."""
        with C.STATE_LOCK:
            runs = self.state.setdefault("runs", {})
            runs[task_id] = runs.get(task_id, 0) + 1
        C.ctx().clear()
        C.ctx().update(slot=slot.name, task=task_id, run=runs[task_id],
                       prefix=f"[slot {slot.name}] " if C.PARALLEL_SLOTS > 1 else "")
        timer = C.Timer()
        C.emit("task_start", title=title, files=SL.task_files(spec), solo=self.solo(task_id, spec),
               base=SL.main_tip()[:10])
        try:
            RN.do_task(slot, task_id, title, spec, self.state)
            self.errors_in_a_row = 0
        except Exception as error:
            self.errors_in_a_row += 1
            C.ctx()["outcome"] = C.ctx().get("outcome") or "crashed"
            C.ctx()["reason"] = C.ctx().get("reason") or str(error)[:300]
            RN.task_crashed(slot, task_id, title, error, self.errors_in_a_row, self.state)
        finally:
            outcome = C.ctx().get("outcome") or ("stopped" if C.RUNTIME.get("stopping") else "paused")
            C.emit("task_end", outcome=outcome, reason=C.ctx().get("reason", ""), seconds=timer.seconds())
            SL.release(slot)
            slot.task = None
            with C.STATE_LOCK:
                T.after_blocks(self.state)
                self.save()
            self.finished.set()

    def save(self):
        """Keeps the busy slots in driver.json: what STATUS shows and what --shutdown from another process reverts."""
        with C.STATE_LOCK:
            busy = self.busy()
            self.state["slots"] = [slot.record() for slot in busy]
            self.state["busy"] = bool(busy)
            self.state["task"] = ", ".join(slot.task for slot in busy) or None
            C.write_driver_state(self.state)

    def wait(self, seconds):
        """Sleeps until a slot finishes or seconds pass."""
        self.finished.wait(seconds)
        self.finished.clear()
