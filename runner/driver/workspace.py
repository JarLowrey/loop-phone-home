"""The repository as a task sees it. A task starts with a snapshot: the commit HEAD points at and every path already
changed. Each check compares against that snapshot, not against HEAD, so a commit made during the task cannot hide what
the task changed, and the owner's own edits, the loop folder included, are never the task's.

The test checks are language-neutral: test_function_regex finds a test, any_function_regex finds where it ends, and
assert_regex counts its asserts (keys in worker.json)."""
import re
import subprocess
import time

import runtime as R
import worker_task as W

C = W.C
DONE_WHEN_ITEM = re.compile(r"\b(test|file|output) `([^`]+)`")


def git(*args, stdin=None):
    return subprocess.run(["git", *args], cwd=C.project, input=stdin, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def head():
    return git("rev-parse", "HEAD").stdout.strip()


def snapshot():
    """The state a task starts from: HEAD's commit and every path already changed."""
    return {"head": head(), "dirty": set(W.changed_paths())}


def head_moved(snap):
    """True when a commit was made since the snapshot. The task's work may then be in that commit, out of the checks'
    reach, so the driver does not judge or commit it."""
    return bool(snap) and head() != snap["head"]


def base(snap):
    """The commit a task's checks compare against: its snapshot's, or HEAD with no snapshot."""
    return (snap or {}).get("head") or "HEAD"


def task_changes(snap):
    """Every path changed since the snapshot, the loop folder included."""
    return sorted(set(W.changed_paths()) - (snap or {}).get("dirty", set()))


def loop_folder():
    """The folder of the loop file, with a trailing slash."""
    return C.loop_file.rsplit("/", 1)[0] + "/"


def scope_changes():
    """Uncommitted changes in the scope: what a task would build on if it started now."""
    return sorted(path for path in W.changed_paths() if W.in_scope(path))


def outside_scope(snap):
    """Paths the task changed outside the scope and the loop folder. A task must not touch these."""
    return [path for path in task_changes(snap) if not W.in_scope(path) and not path.startswith(loop_folder())]


def paragraph(spec, label):
    """The paragraph of a task that starts with label (such as **Done when.**), to the next blank line."""
    lines = spec.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(label):
            found = []
            for part in lines[index:]:
                if not part.strip():
                    break
                found.append(part)
            return " ".join(found)
    return ""


def done_when(spec):
    """The task's Done when checks as (kind, value) pairs: test `name`, file `path` or output `text`. The line may wrap."""
    return DONE_WHEN_ITEM.findall(paragraph(spec, "**Done when.**"))


def done_files(spec):
    """The files a task's Done when line names. They are committed with the scope."""
    return [value for kind, value in done_when(spec) if kind == "file"]


def allowed_loop_files(spec, opus):
    """The loop-folder paths a task may change: the ones its Files and Done when lines name, and for an Opus task or a
    triage also the task and progress files. A named path ending in / allows the whole folder."""
    folder = re.escape(loop_folder())
    named = set(re.findall(rf"`({folder}[^`]+)`", paragraph(spec, "**Files.**")))
    named.update(path for path in done_files(spec) if path.startswith(loop_folder()))
    if opus:
        named.update((C.tasks_file, C.progress_file))
    return named


def loop_changes(snap, allowed):
    """The loop-folder paths the task changed that it was not allowed to, such as the loop file itself."""
    folders = tuple(path for path in allowed if path.endswith("/"))
    return [path for path in task_changes(snap)
            if path.startswith(loop_folder()) and path not in allowed and not path.startswith(folders)]


def test_names(text):
    return set(re.findall(C.test_function_regex, text, re.M))


def removed_tests(snap):
    """Test functions the snapshot's commit has in a changed file and that no file in the scope has now. A test that
    moved to another file is not removed."""
    before = set()
    for path in task_changes(snap):
        shown = git("show", f"{base(snap)}:{path}")
        if shown.returncode == 0:
            before |= test_names(shown.stdout)
    if not before:
        return []
    now_names = set()
    for folder in C.scope_dirs:
        for path in (C.project / folder).rglob("*"):
            if path.is_file():
                now_names |= test_names(path.read_text(encoding="utf-8", errors="replace"))
    return sorted(before - now_names)


def assert_counts(text):
    """{test function: how many asserts it makes} for one file's text. A test runs to the next function."""
    counts = {}
    starts = [(match.start(), match.group(1)) for match in re.finditer(C.any_function_regex, text, re.M)]
    starts.append((len(text), ""))
    tests = test_names(text)
    for (start, name), (end, _) in zip(starts, starts[1:]):
        if name in tests:
            counts[name] = len(re.findall(C.assert_regex, text[start:end]))
    return counts


def weakened(before_text, now_text, spec, path):
    """The test functions in one file that make fewer asserts than before, where the task names neither the function nor
    the file. A task may rewrite the asserts it names; any other drop is a test loosened to pass."""
    before = assert_counts(before_text)
    now = assert_counts(now_text)
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if stem in spec:
        return []
    return [f"{stem}.{name} ({count} to {now[name]} asserts)" for name, count in before.items()
            if name in now and now[name] < count and name not in spec]


def weakened_tests(snap, spec):
    """The test functions the task loosened (see weakened), over every file it changed."""
    found = []
    for path in task_changes(snap):
        shown = git("show", f"{base(snap)}:{path}")
        current = C.project / path
        if shown.returncode == 0 and current.is_file():
            found += weakened(shown.stdout, current.read_text(encoding="utf-8", errors="replace"), spec, path)
    return found


def check_done_when(spec, output, required=True):
    """The first Done when check that does not hold, or ''. A test is a function of that name, in any language, in a file
    under the scope. Without the line, the task is refused when required is set."""
    checks = done_when(spec)
    if not checks:
        return "the task has no **Done when.** line, so the driver cannot tell that it was done" if required else ""
    for kind, value in checks:
        if kind == "file":
            if not (C.project / value).exists() or not git("status", "--porcelain", "--", value).stdout.strip():
                return f"Done when: {value} was not written"
        elif kind == "test":
            pattern = re.compile(rf"\b{re.escape(value)}\s*\(")
            files = [path for folder in C.scope_dirs for path in (C.project / folder).rglob("*") if path.is_file()]
            if not any(pattern.search(path.read_text(encoding="utf-8", errors="replace")) for path in files):
                return f"Done when: no test function {value} in the scope"
        elif value not in output:
            return f"Done when: the test run did not print {value!r}"
    return ""


def undo_changes(paths):
    """Saves, then undoes, uncommitted changes: tracked files go back to HEAD, new files are deleted, and the folders that
    leaves empty are removed. The copies go to <logs>/revert-<time>/files/."""
    if not paths:
        return
    backup = C.logs / f"revert-{time.strftime('%Y%m%d-%H%M%S')}"
    for path in paths:
        source = C.project / path
        if source.is_file():
            target = backup / "files" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        if git("cat-file", "-e", f"HEAD:{path}").returncode == 0:
            git("checkout", "HEAD", "--", path)
        else:
            source.unlink(missing_ok=True)
            folder = source.parent
            while folder != C.project and folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
                folder = folder.parent
    R.log(f"undid {len(paths)} changed files, saved in {backup}: {', '.join(paths[:5])}")
