"""A PreToolUse hook for the driver's task sessions. Each session works in its own slot (a git worktree), and the hook
keeps it there:
- a Bash command that changes folder (cd or pushd, alone or in a chain) is refused, because a session that leaves the
  project root writes its files under the wrong path, such as src/src/;
- a Bash command that names the owner's main tree or another slot is refused;
- an Edit or Write outside the slot's own worktree is refused, and so is one to a file that another running task holds
  (leases.py). A file nobody holds is leased to this slot as it is written.
The driver passes it to each task session with --settings and its slot's arguments (--root, --slot, --leases and one
--forbid per other tree); the owner's own sessions do not get it. It reads the hook's JSON on stdin and prints a deny
decision, or nothing to let the call run."""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import leases as L  # noqa: E402

# a segment of a command line: split on && || ; | and new lines; a leading ( opens a subshell
SEGMENTS = re.compile(r"&&|\|\||[;|\n]")
CHANGES_FOLDER = re.compile(r"^\(?\s*(?:cd|pushd|popd)(?:\s|$)")
CD_REASON = ("cd is refused in task sessions: run every command from the project root, and write paths from there "
             "(src/..., tools/...). A cd puts files under the wrong folder.")
OTHER_TREE_REASON = ("This session works in its own copy of the project, the folder it started in. Commands and edits "
                     "must stay in it: {path} is another copy (the owner's, or another task's).")
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
# files a session may write anywhere in its slot without a lease: the logs folder (--logs sets it)
LOGS_PREFIX = "loop/logs/"


def changes_folder(command):
    """True when any command in the line is cd, pushd or popd."""
    return any(CHANGES_FOLDER.match(part.strip()) for part in SEGMENTS.split(command or ""))


def spellings(path):
    """The ways a shell command may write a Windows folder: C:\\a\\b, C:/a/b and /c/a/b, all lower case."""
    path = os.path.abspath(path)
    forward = path.replace("\\", "/")
    drive, rest = forward[0].lower(), forward[2:]
    return {path.lower(), forward.lower(), f"/{drive}{rest}".lower()}


def names_tree(command, trees):
    """The first of trees that the command names, or ''."""
    lowered = (command or "").lower()
    for tree in trees:
        for spelling in spellings(tree):
            if re.search(re.escape(spelling) + r"(?=[\\/\"'\s;|&)]|$)", lowered):
                return tree
    return ""


def inside(path, tree):
    path, tree = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(tree))
    return path == tree or path.startswith(tree.rstrip("\\/") + os.sep)


def decide(event, root, slot, leases, forbid):
    """The reason to refuse this tool call, or '' to let it run."""
    tool = event.get("tool_name")
    given = event.get("tool_input") or {}
    if tool == "Bash":
        command = given.get("command", "")
        if changes_folder(command):
            return CD_REASON
        other = names_tree(command, forbid)
        return OTHER_TREE_REASON.format(path=other) if other else ""
    if tool not in WRITE_TOOLS:
        return ""
    target = given.get("file_path") or given.get("notebook_path") or ""
    if not target:
        return ""
    full = target if os.path.isabs(target) else os.path.join(root, target)
    for tree in forbid:
        if inside(full, tree):
            return OTHER_TREE_REASON.format(path=tree)
    if not inside(full, root) or not leases:
        return ""
    relative = os.path.relpath(os.path.abspath(full), os.path.abspath(root)).replace("\\", "/")
    if relative.startswith(LOGS_PREFIX):
        return ""
    refused = L.acquire(leases, slot, relative)
    if refused:
        return (f"{refused}. Finish the task without changing it if you can. If the task cannot be done without it, "
                f"stop now and say that you need {relative}: the driver runs the task again once that task has landed.")
    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.getcwd())
    parser.add_argument("--slot", default="")
    parser.add_argument("--leases", default="")
    parser.add_argument("--forbid", action="append", default=[])
    parser.add_argument("--logs", default="")
    args = parser.parse_args()
    global LOGS_PREFIX
    LOGS_PREFIX = args.logs.rstrip("/") + "/" if args.logs else LOGS_PREFIX
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    reason = decide(event, args.root, args.slot, args.leases, args.forbid)
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
