"""Reads <logs>/events.jsonl and says where the loop's time and money went: the answers an audit of bottlenecks,
blockages and slowdowns starts from. Plain text, so a person or a model can read it.

Usage, from the project root:
  python <runner>/driver/report.py                  everything recorded
  python <runner>/driver/report.py --since 2026-10-27        from that day on
  python <runner>/driver/report.py --task T-07      one task's runs, event by event

Sections: throughput, where task time goes (session, gate tests, landing, waits), why slots sat idle, slow tasks,
failures (blocks, requeues, escalations, failed sessions), friction inside sessions (hook refusals, Bash commands cut
off, failed tool calls), pauses, pushes, and monitor runs."""
import argparse
import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import loop_common as C  # noqa: E402

# the idle reasons scheduler.next_start() gives, by the words that start or mark them
IDLE_KINDS = [("runs alone", "a task that runs alone (Opus, no Files line, or requeued too often)"),
              ("waits for", "a dependency has not landed (After line, a named task, a refused file)"),
              ("shares", "the next tasks share a file with a running one"),
              ("is held by", "a file the next task needs is held by a running task"),
              ("outside the window", "the next startable task is outside parallel_window"),
              ("no open task", "no open task is left to start")]


def load(path, since=""):
    events = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("time", "") >= since:
            events.append(event)
    return sorted(events, key=lambda item: item.get("t", 0))


def hours(seconds):
    return f"{seconds / 3600:.2f} h"


def minutes(seconds):
    return f"{seconds / 60:.1f} min"


def idle_kind(reason):
    for marker, kind in IDLE_KINDS:
        if marker in (reason or ""):
            return kind
    return "unknown (no schedule_wait recorded yet)"


def runs(events):
    """{(task, run): {phase totals, outcome, slot}} from the events of each task run."""
    table = collections.defaultdict(lambda: collections.defaultdict(float))
    for event in events:
        if "task" not in event or "run" not in event:
            continue
        row = table[(event["task"], event["run"])]
        kind = event["event"]
        if kind == "session":
            row["session"] += event.get("seconds") or 0
            row["cost"] += event.get("cost_usd") or 0
            row["sessions"] += 1
        elif kind == "gate":
            row["gate_tests"] += event.get("test_seconds") or 0
            row["gate_other"] += max(0.0, (event.get("seconds") or 0) - (event.get("test_seconds") or 0))
        elif kind == "catch_up":
            row["catch_up"] += event.get("seconds") or 0
        elif kind == "land":
            row["lock_wait"] += event.get("lock_wait") or 0
            row["retest"] += event.get("retest_seconds") or 0
            row["owner_wait"] += event.get("owner_wait") or 0
            row["land_other"] += max(0.0, (event.get("seconds") or 0) - (event.get("lock_wait") or 0)
                                     - (event.get("retest_seconds") or 0) - (event.get("owner_wait") or 0))
        elif kind == "task_end":
            row["wall"] = event.get("seconds") or 0
            row["outcome"] = event.get("outcome")
            row["reason"] = event.get("reason", "")
            row["slot"] = event.get("slot")
            row["end"] = event.get("t")
    return table


def idle_time(events):
    """{idle kind: slot-seconds} and the observed slot-seconds: how long free slots waited, and why. Time counts while
    the driver runs; a pause counts as 'paused'."""
    buckets = collections.Counter()
    slots, busy, reason, paused, last, observed = 1, set(), "", False, None, 0.0
    for event in events:
        now = event.get("t", 0)
        if last is not None:
            span = now - last
            observed += span * slots
            free = max(0, slots - len(busy))
            if free:
                buckets["paused" if paused else idle_kind(reason)] += span * free
        kind = event["event"]
        if kind == "driver_start":
            slots = int((event.get("settings") or {}).get("parallel_slots", 1) or 1)
            busy, reason, paused, last = set(), "", False, now
            continue
        if kind == "driver_stop":
            last = None
            continue
        if kind == "settings" and "parallel_slots" in (event.get("changed") or {}):
            slots = int(event["changed"]["parallel_slots"])
        elif kind == "task_start":
            busy.add(event.get("slot"))
            reason, paused = "", False
        elif kind == "task_end":
            busy.discard(event.get("slot"))
        elif kind == "schedule_wait":
            reason = event.get("reason", "")
        elif kind == "pause":
            paused = True
        elif kind == "resume":
            paused = False
        if last is not None:
            last = now
    return buckets, observed


def section(title):
    print(f"\n== {title}")


def report(events):
    if not events:
        print("No events recorded yet: <logs>/events.jsonl is empty or missing. The driver writes it from the "
              "commit that added events.py on; restart a driver started before that.")
        return
    table = runs(events)
    ends = [row for row in table.values() if row.get("outcome")]
    outcomes = collections.Counter(row["outcome"] for row in ends)
    sessions = [event for event in events if event["event"] == "session"]
    cost = sum(event.get("cost_usd") or 0 for event in sessions)
    span = events[-1]["t"] - events[0]["t"]
    print(f"{len(events)} events from {events[0]['time']} to {events[-1]['time']} ({hours(span)} of wall time)")

    section("Throughput")
    landed = outcomes.get("landed", 0)
    print(f"task runs finished: {len(ends)}: " + ", ".join(f"{count} {name}" for name, count in outcomes.most_common()))
    print(f"landed per hour of wall time: {landed / (span / 3600):.2f}" if span > 0 and landed else "landed: 0")
    print(f"spent: ${cost:.2f} over {len(sessions)} sessions; per landed task: "
          f"${cost / landed:.2f}" if landed else f"spent: ${cost:.2f} over {len(sessions)} sessions")
    wasted = sum(row["cost"] for row in ends if row["outcome"] != "landed")
    print(f"spent on runs that did not land: ${wasted:.2f}")

    section("Where task time goes (all runs, slot-time)")
    phases = collections.Counter()
    for row in ends:
        known = 0.0
        for phase in ("session", "gate_tests", "gate_other", "catch_up", "lock_wait", "retest", "owner_wait",
                      "land_other"):
            phases[phase] += row[phase]
            known += row[phase]
        phases["other (prepare, prompts, git, approvals)"] += max(0.0, row["wall"] - known)
    total = sum(phases.values()) or 1
    names = {"session": "model sessions", "gate_tests": "gate test runs", "gate_other": "gate checks",
             "catch_up": "moving work onto the newest commit", "lock_wait": "waiting for the landing lock",
             "retest": "tests run again after a rebase", "owner_wait": "waiting for the owner's main tree",
             "land_other": "landing (rebase, commit, push)"}
    for phase, seconds in phases.most_common():
        print(f"  {names.get(phase, phase):45} {minutes(seconds):>11}  {100 * seconds / total:4.1f}%")

    section("Why free slots sat idle (slot-time)")
    buckets, observed = idle_time(events)
    idle = sum(buckets.values())
    print(f"slot-time observed: {hours(observed)}; idle: {hours(idle)} ({100 * idle / (observed or 1):.0f}%)")
    for kind, seconds in buckets.most_common():
        print(f"  {kind:75} {minutes(seconds):>11}")
    waits = collections.Counter(event.get("reason", "") for event in events if event["event"] == "schedule_wait")
    if waits:
        print("  most frequent wait reasons:")
        for reason, count in waits.most_common(5):
            print(f"    {count:3} x {reason[:150]}")

    section("Slowest task runs (wall time)")
    for (task, run), row in sorted(table.items(), key=lambda item: -item[1].get("wall", 0))[:10]:
        if not row.get("outcome"):
            continue
        print(f"  {task} run {run} ({row['outcome']}): {minutes(row['wall'])}, session {minutes(row['session'])}, "
              f"gate tests {minutes(row['gate_tests'])}, retest {minutes(row['retest'])}, lock "
              f"{minutes(row['lock_wait'])}, owner {minutes(row['owner_wait'])}, ${row['cost']:.2f}")

    section("Failures")
    for kind, label in (("block", "blocked"), ("requeue", "requeued"), ("escalate", "escalated")):
        found = [event for event in events if event["event"] == kind]
        print(f"{label}: {len(found)}")
        for reason, count in collections.Counter(" ".join(str(event.get("reason", "")).split()[:8])
                                                 for event in found).most_common(5):
            print(f"    {count:3} x {reason}")
    failed = [event for event in sessions if event.get("is_error")]
    print(f"sessions that failed: {len(failed)}")
    for reason, count in collections.Counter(f"{event.get('terminal_reason') or event.get('subtype')}"
                                             for event in failed).most_common(5):
        print(f"    {count:3} x {reason}")

    section("Friction inside sessions")
    timeouts = sum(event.get("bash_timeouts") or 0 for event in sessions)
    denials = [denial for event in sessions for denial in event.get("denials") or []]
    errors = sum(event.get("tool_errors") or 0 for event in sessions)
    tests = sum(event.get("test_runs") or 0 for event in sessions)
    turns = sum(event.get("turns") or 0 for event in sessions)
    print(f"Bash commands cut off by Claude Code's timeout: {timeouts}")
    print(f"tool calls refused by the task hook: {len(denials)}")
    for denial, count in collections.Counter(denial[:90] for denial in denials).most_common(5):
        print(f"    {count:3} x {denial}")
    print(f"failed tool calls: {errors}; test runs by sessions: {tests}; turns: {turns}")

    section("Pauses, pushes and monitor runs")
    pauses = collections.Counter(event.get("title") for event in events if event["event"] == "pause")
    for title, count in pauses.most_common():
        print(f"  pause {count} x {title}")
    pushes = [event for event in events if event["event"] == "push"]
    print(f"pushes: {len(pushes)}, failed {sum(1 for event in pushes if not event.get('ok'))}, "
          f"slowest {max((event.get('seconds') or 0 for event in pushes), default=0):.0f} s")
    for event in (event for event in events if event["event"] == "monitor_run"):
        print(f"  monitor run {event['time']}: clean checkout {event.get('clean')}, monitor exit "
              f"{event.get('monitor_exit')}")


def task_detail(events, task):
    for event in events:
        if event.get("task") == task:
            fields = {key: value for key, value in event.items() if key not in ("time", "t", "event", "task")}
            print(f"{event['time']} {event['event']:13} {json.dumps(fields)[:300]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default="")
    parser.add_argument("--task", default="")
    args = parser.parse_args()
    events = load(C.LOGS / "events.jsonl", args.since)
    if args.task:
        task_detail(events, args.task)
    else:
        report(events)


if __name__ == "__main__":
    main()
