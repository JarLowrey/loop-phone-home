"""File leases between slots: which running task may change which file. A slot leases its task's Files line when the task
starts, and the task hook (task_hook.py) leases each further file the session writes, first come first served. A file
another slot holds is refused, with that task's name, unless it is a shared file. Leases live in one JSON file, so the
driver and the hook processes see the same ones; a lock file keeps their writes apart. Only the standard library is
used here, since the hook imports this module on its own."""
import json
import os
import pathlib
import time
from contextlib import contextmanager

# a lock file older than this was left by a process that died while holding it
STALE_LOCK_SECONDS = 10


@contextmanager
def locked(path):
    """Holds <path>.lock while the body runs. Creating the file is the lock, so it works across processes."""
    lock = pathlib.Path(str(path) + ".lock")
    deadline = time.time() + 2 * STALE_LOCK_SECONDS
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(handle)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS or time.time() > deadline:
                    lock.unlink(missing_ok=True)
            except OSError:
                pass
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def read(path):
    """{"slots": {name: {"task", "files"}}, "shared": [paths]}; empty when the file is missing or unreadable."""
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("slots", {})
    data.setdefault("shared", [])
    return data


def write(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8", newline="\n")
    os.replace(temp, path)


def covers(leased, path):
    """True when a lease on leased covers path: the same file, a file in a leased folder (written ending in /), or a
    file a tool makes beside the leased one (x.gd.uid beside x.gd, strings.en.translation beside strings.csv)."""
    if leased.endswith("/"):
        return path.startswith(leased)
    if path == leased:
        return True
    stem = leased.rsplit(".", 1)[0] if "." in leased.rsplit("/", 1)[-1] else leased
    return path.startswith(stem + ".")


def holder(data, path, me):
    """(slot, task) of another slot whose lease covers path, or None. A shared file has no holder."""
    if any(covers(shared, path) for shared in data["shared"]):
        return None
    for name, lease in data["slots"].items():
        if name != me and any(covers(leased, path) for leased in lease.get("files", [])):
            return name, lease.get("task", "")
    return None


def acquire(path, me, file):
    """Leases file to slot me unless another slot holds it. Returns '' when me may write it, else why not."""
    with locked(path):
        data = read(path)
        held = holder(data, file, me)
        lease = data["slots"].setdefault(me, {"task": "", "files": []})
        if held:
            refused = lease.setdefault("refused", [])
            if [file, held[1]] not in refused:
                refused.append([file, held[1]])
                write(path, data)
            return f"{file} is being changed by task {held[1]} in another copy of the project (slot {held[0]})"
        if not any(covers(leased, file) for leased in lease["files"]):
            lease["files"].append(file)
            write(path, data)
        return ""


def overlap(data, files, me=None):
    """The files of this list another slot holds, each with its holder: [(file, slot, task)]."""
    found = []
    for file in files:
        for name, lease in data["slots"].items():
            if name == me or any(covers(shared, file) or covers(file, shared) for shared in data["shared"]):
                continue
            if any(covers(leased, file) or covers(file, leased) for leased in lease.get("files", [])):
                found.append((file, name, lease.get("task", "")))
    return found


def start(path, me, task, files, shared):
    """Gives slot me the lease of a task's files, and records the shared list."""
    with locked(path):
        data = read(path)
        data["shared"] = list(shared)
        data["slots"][me] = {"task": task, "files": list(files)}
        write(path, data)


def release(path, me):
    with locked(path):
        data = read(path)
        if data["slots"].pop(me, None) is not None:
            write(path, data)


def refusals(path, me):
    """[(file, task)] the hook refused slot me since its task started, because another task held the file."""
    return [tuple(item) for item in read(path)["slots"].get(me, {}).get("refused", [])]


def files_of(path, me):
    """The files slot me holds now: its task's Files line and what its session wrote since."""
    return list(read(path)["slots"].get(me, {}).get("files", []))
