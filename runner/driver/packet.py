"""The review packet: what the driver already knows about a stretch of work, handed to an Opus review (a -R or -V
task) in its prompt so it does not spend turns gathering it. Opus turns are the loop's main cost: each one re-reads
the whole conversation, so a review that opens a dozen files at the start pays for them on every turn after.

The packet holds the commit range, the files and functions it changed, a scan for names a merge doubled, what each
task in the range cost and how often it failed (from usage.jsonl and events.jsonl), the latest test totals, and the
Blocked list. It is facts, not findings: the review still judges."""
import collections
import json
import re
import subprocess

import loop_common as C
import slots as SL
import tasks as T

REVIEW_ID = C.REVIEW_ID
RANGE_AFTER = re.compile(r"starts at the commit after `([^`]+)`")
# a function or signal a diff adds or changes the line of
CHANGED_FUNC = re.compile(r"^[+-](?:static )?(func|signal) (\w+)", re.M)


def is_review(task_id):
    return bool(REVIEW_ID.search(task_id))


def subject_commit(task_id):
    """The newest commit whose subject starts with '<task_id>:', or ''."""
    found = C.git("log", "-1", "--format=%H", f"--grep=^{re.escape(task_id)}:", "-E")
    return found.stdout.strip()


def range_start(spec):
    """The commit the review starts after: the one its Build names, else the last review's commit, else 20 back."""
    named = RANGE_AFTER.search(spec)
    if named and subject_commit(named.group(1)):
        return subject_commit(named.group(1)), f"the commit of {named.group(1)}, as the task says"
    for line in C.git("log", "-300", "--format=%H %s").stdout.splitlines():
        commit, _, subject = line.partition(" ")
        if is_review(subject.split(":", 1)[0]):
            return commit, f"the last review's commit ({subject[:60]})"
    return C.git("rev-parse", "HEAD~20").stdout.strip(), "20 commits back (no earlier review found)"


def task_costs(ids):
    """{task: [dollars, sessions, failed sessions]} from usage.jsonl."""
    table = collections.defaultdict(lambda: [0.0, 0, 0])
    if not C.USAGE_LOG.exists():
        return table
    for line in C.USAGE_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("task") in ids:
            entry = table[row["task"]]
            entry[0] += row.get("cost_usd") or 0
            entry[1] += 1
            entry[2] += 1 if row.get("is_error") else 0
    return table


def task_events(ids):
    """{task: Counter of requeue, block, escalate, task_end outcomes} from events.jsonl."""
    table = collections.defaultdict(collections.Counter)
    path = C.LOGS / "events.jsonl"
    if not path.exists():
        return table
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("task") in ids and event.get("event") in ("requeue", "block", "escalate", "task_end"):
            table[event["task"]][event["event"] if event["event"] != "task_end" else event.get("outcome")] += 1
    return table


def latest_totals():
    """The newest gate's test totals in events.jsonl, or ''."""
    path = C.LOGS / "events.jsonl"
    if not path.exists():
        return ""
    for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        if '"event": "gate"' in line and '"totals": "' in line:
            event = json.loads(line)
            if event.get("totals"):
                return f"{event['totals']} (gate of {event.get('task')} at {event['time']}, tests {event.get('test_seconds')} s)"
    return ""


def blocked_lines():
    path = T.progress_file()
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    match = re.search(r"^## Blocked\n(.*?)(?=^## )", text, re.M | re.S)
    return [line for line in (match.group(1) if match else "").splitlines() if line.startswith("- ")]


def build(task_id, spec):
    """The packet's text, for the review's prompt."""
    base, why = range_start(spec)
    commits = C.git("log", "--reverse", "--format=%h %s", f"{base}..HEAD").stdout.strip().splitlines()
    ids = sorted({line.split(" ", 1)[1].split(":", 1)[0] for line in commits if ":" in line})
    stat = C.git("diff", "--stat=120", f"{base}..HEAD", "--", *C.SCOPE).stdout.strip().splitlines()
    changed = C.git("diff", "--name-only", f"{base}..HEAD", "--", *C.SCOPE).stdout.split()
    functions = collections.defaultdict(set)
    for path in [path for path in changed if C.is_source(path) and not path.endswith(tuple(C.CFG.keyed_files))]:
        diff = C.git("diff", "-U0", f"{base}..HEAD", "--", path).stdout
        for _, name in CHANGED_FUNC.findall(diff):
            functions[path].add(name)
    doubled = SL.duplicates(C.PROJECT, changed)
    costs = task_costs(set(ids))
    events = task_events(set(ids))
    lines = [f"# Review packet for {task_id}", "",
             f"Range: {base[:10]}..HEAD ({why}), {len(commits)} commits. Read a file's change with "
             f"`git diff {base[:10]} HEAD -- <path>`; open a whole file only where a finding needs it.", "",
             "## Commits", *[f"- {line}" for line in commits[-60:]], "",
             "## What changed in the scope (git diff --stat)", *stat[-80:], "",
             "## Top-level names each source change added or changed",
             *[f"- {path}: {', '.join(sorted(names))}" for path, names in
               sorted(functions.items())], "",
             "## Names declared twice in one changed file (a merge may have doubled them)",
             *([f"- {item}" for item in doubled] or ["- none"]), "",
             "## Each task in the range: spend and failures",
             "| task | cost | sessions | failed sessions | requeued | blocked | escalated |", "|---|---|---|---|---|---|---|"]
    for task in ids:
        cost, sessions, failed = costs.get(task, [0.0, 0, 0])
        counts = events.get(task, collections.Counter())
        lines.append(f"| {task} | ${cost:.2f} | {sessions} | {failed} | {counts['requeue']} | {counts['block']} | "
                     f"{counts['escalate']} |")
    lines += ["", f"## Latest test run: {latest_totals() or 'no gate recorded in events.jsonl yet'}", "",
              f"## Blocked now ({C.PROGRESS_REL})", *(blocked_lines() or ["- none"])]
    return "\n".join(lines) + "\n"


def safe_build(task_id, spec):
    """build(), or a note saying why there is no packet: a packet that fails must not stop the review."""
    try:
        return build(task_id, spec)
    except (OSError, ValueError, subprocess.SubprocessError, IndexError) as error:
        return f"(No review packet: {error}. Gather the range yourself, as the review guide says.)\n"
