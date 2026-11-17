"""The hook of the driver's task sessions. Each session works in its own slot (a git worktree), and the hook keeps it
there. As a PreToolUse hook:
- a Bash command that starts with `cd <project root> &&` (or `cd . &&`) runs without that prefix, and
  `cd <dir>; <read-only commands>` runs as `(cd <dir> && <commands>)`;
- any other Bash command that changes folder (cd, pushd or popd, alone or in a chain) is refused, because a session that
  leaves the project root writes its files under the wrong path;
- a Bash command that names the owner's main tree or another slot is refused;
- a full test run (--test-call, without one of the --partial flags) is refused when its Bash timeout is shorter than
  --full-timeout-ms, since a cut-off run loses its output; and refused when a full run has already passed in this
  session on the same tree (the receipt below), since the gate uses that run;
- a whole-file Read of a --no-whole-read file (the task and progress files) is refused: the session greps, then reads a
  range;
- an Edit or Write outside the slot's own worktree is refused, and so is one to a file that another running task holds
  (leases.py). A file nobody holds is leased to this slot as it is written;
- from its --deadline on (a few minutes before the driver would kill the session), every tool call is refused with an
  order to write the last message now, so a session that ran out of time still says what it found.
With --post it is the PostToolUse hook of Bash instead: after a full test run it writes the slot's receipt (--receipt),
the run's output with a hash of the scope's files (--scope) and an HMAC of both under the key in LOOP_RECEIPT_KEY. The
gate trusts a receipt whose signature checks, whose output passes and whose tree is still the one the run tested, and
then skips its own run of the suite.
The driver passes the hook to each task session with --settings, and the slot's arguments in the JSON file --config
names; the owner's own sessions do not get it. It reads the hook's JSON on stdin and prints a decision, or nothing to
let the call run. It imports nothing of the driver but leases.py, so it needs no config."""
import argparse
import datetime
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import leases as L  # noqa: E402

RECEIPT_KEY_ENV = "LOOP_RECEIPT_KEY"
# a segment of a command line: split on && || ; | and new lines; a leading ( opens a subshell
SEGMENTS = re.compile(r"&&|\|\||[;|\n]")
CHANGES_FOLDER = re.compile(r"^\(?\s*(?:cd|pushd|popd)(?:\s|$)")
# the start of a heredoc: <<WORD, <<-WORD, <<'WORD' or <<"WORD"; group 1 is the - that lets the end line be indented
HEREDOC = re.compile(r"<<(-?)\s*(?:'([^'\n]+)'|\"([^\"\n]+)\"|([A-Za-z_][A-Za-z0-9_]*))")
CD_REASON = ("cd is refused in task sessions: run every command from the project root, and write paths from there. A "
             "cd puts files under the wrong folder. The one cd allowed is a leading `cd <the project root> && ...`; no "
             "other cd, pushd or popd.")
OTHER_TREE_REASON = ("This session works in its own copy of the project, the folder it started in. Commands and edits "
                     "must stay in it: {path} is another copy (the owner's, or another task's).")
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
DEADLINE_REASON = ("Time is up for this session: no more tool calls. Write your last message now: what you changed, "
                   "what you found (the cause, the fix you would make, what you checked), and what is left. The "
                   "driver keeps it for the next attempt.")
WRITE_MARKS = re.compile(r"(^|[\s;&|(])(sed\s+-i|tee|rm|mv|cp|python\s+-|touch|>|>>)(\s|$)")
SECOND_RUN_REASON = ("A full test run already passed in this session, on the tree as it is now: the gate uses that "
                     "run. Run part of the suite for anything else you check.")
BIG_FILE_REASON = ("{name} is long: grep for the heading or task id you need, then Read that range with offset and "
                   "limit. Your own task is already in the prompt.")
TIMEOUT_REASON = ("A full test run can take longer than the Bash tool's default timeout, and a cut-off run loses its "
                  "output. Run it again with the timeout parameter set to {timeout}, or run part of the suite while "
                  "you work and the full suite once at the end.")


def changes_folder(command):
    """True when any command in the line is cd, pushd or popd. Quoted text and heredoc bodies are not commands."""
    return any(CHANGES_FOLDER.match(part.strip()) for part in SEGMENTS.split(unquoted(command or "")))


def unquoted(command):
    """The command with every quoted span, escaped character and heredoc body blanked, so only its own separators
    are left to split on."""
    out, i, n, pending = [], 0, len(command), []
    while i < n:
        c = command[i]
        if c == "\\" and i + 1 < n:
            out.append("  ")
            i += 2
        elif c in "'\"":
            end = i + 1
            while end < n and command[end] != c:
                end += 2 if c == '"' and command[end] == "\\" else 1
            end = min(end + 1, n)
            out.append(" " * (end - i))
            i = end
        elif command.startswith("<<", i) and not command.startswith("<<<", i):
            match = HEREDOC.match(command, i)
            if match:
                pending.append((match.group(2) or match.group(3) or match.group(4), bool(match.group(1))))
                out.append(" " * (match.end() - i))
                i = match.end()
            else:
                out.append(c)
                i += 1
        elif c == "\n" and pending:
            out.append(c)
            i += 1
            for word, tabs in pending:
                while i < n:
                    end = command.find("\n", i)
                    end = n if end < 0 else end
                    line = command[i:end]
                    out.append(" " * (end - i) + ("\n" if end < n else ""))
                    i = end + 1
                    if (line.lstrip("\t") if tabs else line) == word:
                        break
            pending = []
        else:
            out.append(c)
            i += 1
    return "".join(out)


def strip_root_cd(command, root):
    """The command without a leading `cd <root> &&` (or `cd . &&`), or None when it does not start with one."""
    match = re.match(r"\s*cd\s+(?:\"([^\"]*)\"|'([^']*)'|(\S+))\s*(?:&&|;)\s*(.*)$", command or "", re.S)
    if not match:
        return None
    target = match.group(1) or match.group(2) or match.group(3)
    if target in (".", "./"):
        return match.group(4)
    target = target.rstrip("\\/").lower()
    return match.group(4) if target and target in spellings(root) else None


def rewrite_cd(command):
    """`cd <dir>; <read-only commands>` as `(cd <dir> && <commands>)`, or None when the command is not that
    shape: no cd, a cd not first, or anything after it that writes (WRITE_MARKS on the unquoted text)."""
    flat = unquoted(command or "")
    match = re.match(r"^\s*cd\s+(\S+)\s*(?:;|&&)\s*(.+)$", flat, re.S)
    if not match:
        return None
    if WRITE_MARKS.search(match.group(2)) or changes_folder(match.group(2)):
        return None
    target = command[match.start(1):match.end(1)]
    if target.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", target):
        return None
    if all(part in ("", "..", ".") for part in re.split(r"[\\/]", target)) and ".." in target:
        return None
    rest = command[match.start(2):match.end(2)].rstrip()
    return f"(cd {target} && {rest})"


def spellings(path):
    """The ways a shell command may write a folder, all lower case: as given, with forward slashes, and on Windows as
    /c/a/b too."""
    path = os.path.abspath(path)
    forward = path.replace("\\", "/")
    found = {path.lower(), forward.lower()}
    if re.match(r"[A-Za-z]:", forward):
        found.add(f"/{forward[0].lower()}{forward[2:]}".lower())
    return found


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


def runs_tests(command, test_call):
    """True when the command runs the tests (test_call, a regex), rather than only naming the script."""
    return bool(test_call) and bool(re.search(test_call, command or "", re.I))


def full_run(command, test_call, partial):
    """True when the command runs the whole suite: a test call without one of the partial flags."""
    return runs_tests(command, test_call) and not any(flag in (command or "") for flag in partial)


def alone(command):
    """True when the command is one call and its output is all the call's own: no pipe, no command chained before or
    after it, no redirect of its output (2>&1 is fine). A receipt holds the output a run printed, so a run filtered
    through tail or followed by an echo must not earn one."""
    flat = re.sub(r"\d?>&\d", " ", unquoted(command or "")).strip()
    return not re.search(r"[|;&\n<>`]|\$\(", flat)


def needs_timeout(command, timeout, test_call, partial, full_timeout_ms):
    """True for a full test call whose Bash timeout is unset or too short to let the suite finish."""
    if not full_timeout_ms or not full_run(command, test_call, partial):
        return False
    return not timeout or timeout < full_timeout_ms


def tree_hash(root, scope):
    """sha256 over what the working tree at root holds under the scope's folders: each tracked path with its blob hash
    from `git ls-files -s`, replaced by the sha1 of the file's bytes where the file is modified or untracked, and a
    'deleted' mark where it is gone. Same tree, same hash; any edit, a new file or a deletion changes it."""
    scope = [item.rstrip("/") for item in scope] or ["."]
    listed = subprocess.run(["git", "ls-files", "-s", "-z", "--", *scope], cwd=root, capture_output=True)
    entries = {}
    for item in listed.stdout.decode("utf-8", "replace").split("\0"):
        if "\t" in item:
            meta, path = item.split("\t", 1)
            entries[path] = meta.split()[1]
    status = subprocess.run(["git", "status", "--porcelain=v1", "-z", "-uall", "--", *scope], cwd=root,
                            capture_output=True)
    items = status.stdout.decode("utf-8", "replace").split("\0")
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        if len(item) < 4:
            continue
        path = item[3:]
        if "R" in item[:2] or "C" in item[:2]:
            index += 1   # why: -z writes a rename's old path as the next item, which the new path replaces
        file = os.path.join(root, path)
        if os.path.isfile(file):
            with open(file, "rb") as handle:
                entries[path] = hashlib.sha1(handle.read()).hexdigest()
        else:
            entries[path] = "deleted"
    digest = hashlib.sha256()
    for path in sorted(entries):
        digest.update(f"{path} {entries[path]}\n".encode("utf-8"))
    return digest.hexdigest()


def receipt_signature(key, tree, output):
    """The HMAC-SHA256 of a receipt's tree hash and output under the key."""
    return hmac.new(key.encode("utf-8"), f"{tree}\n{output}".encode("utf-8"), hashlib.sha256).hexdigest()


def run_passed(output, totals_marker, pass_marker, fail_markers):
    """True when the output's last totals line holds pass_marker and no line starts with a fail marker: the driver's
    own test_failure, for a hook that loads no config."""
    totals = [line for line in (output or "").splitlines() if totals_marker and totals_marker in line]
    if not totals or pass_marker not in totals[-1]:
        return False
    return not any(line.startswith(tuple(fail_markers)) for line in output.splitlines()) if fail_markers else True


def read_receipt(path):
    """A JSON file as a dict, or {} when it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def response_text(response):
    """What a Bash call printed, from the PostToolUse event's tool_response: its stdout and stderr when it is a dict,
    else the response as text."""
    if isinstance(response, dict):
        parts = [response.get("stdout") or "", response.get("stderr") or ""]
        if not any(parts) and response.get("output"):
            parts = [str(response["output"])]
        return "\n".join(part for part in parts if part).strip()
    return "" if response is None else str(response).strip()


def full_run_passed(args, root):
    """True when the slot's receipt was written since the session started, says the run passed, and the scope's files
    are still the ones it tested."""
    record = read_receipt(args.receipt) if args.receipt else {}
    if not record.get("passed") or record.get("written", 0) < (args.started or 0):
        return False
    return record.get("tree") == tree_hash(root, args.scope)


def decide(event, args, allow_cd=False):
    """The reason to refuse this tool call, or '' to let it run."""
    root = args.root
    if args.deadline and time.time() >= args.deadline:
        return DEADLINE_REASON
    tool = event.get("tool_name")
    given = event.get("tool_input") or {}
    if tool == "Bash":
        command = given.get("command", "")
        if not allow_cd and changes_folder(command):
            return CD_REASON
        if needs_timeout(command, given.get("timeout"), args.test_call, args.partial, args.full_timeout_ms):
            return TIMEOUT_REASON.format(timeout=args.full_timeout_ms)
        if full_run(command, args.test_call, args.partial) and full_run_passed(args, root):
            return SECOND_RUN_REASON
        other = names_tree(command, args.forbid)
        return OTHER_TREE_REASON.format(path=other) if other else ""
    if tool == "Read":
        name = (given.get("file_path") or "").replace("\\", "/")
        whole = "offset" not in given and "limit" not in given
        for big in args.no_whole_read:
            if whole and (name == big or name.endswith("/" + big)):
                return BIG_FILE_REASON.format(name=big)
        return ""
    if tool not in WRITE_TOOLS:
        return ""
    target = given.get("file_path") or given.get("notebook_path") or ""
    if not target:
        return ""
    full = target if os.path.isabs(target) else os.path.join(root, target)
    for tree in args.forbid:
        if inside(full, tree):
            return OTHER_TREE_REASON.format(path=tree)
    if not inside(full, root) or not args.leases:
        return ""
    relative = os.path.relpath(os.path.abspath(full), os.path.abspath(root)).replace("\\", "/")
    if args.logs and relative.startswith(args.logs):
        return ""
    refused = L.acquire(args.leases, args.slot, relative)
    if refused:
        return (f"{refused}. Finish the task without changing it if you can. If the task cannot be done without it, "
                f"stop now and say that you need {relative}: the driver runs the task again once that task has landed.")
    return ""


def after_bash(event, args):
    """PostToolUse: writes the receipt of a full test run. Any other call, or a hook without --receipt, writes nothing."""
    given = event.get("tool_input") or {}
    command = strip_root_cd(given.get("command", ""), args.root) or given.get("command", "")
    if event.get("tool_name") != "Bash" or not args.receipt or not full_run(command, args.test_call, args.partial):
        return
    if not alone(command):
        return
    response = event.get("tool_response")
    output = response_text(response)
    interrupted = isinstance(response, dict) and bool(response.get("interrupted"))
    passed = not interrupted and run_passed(output, args.totals_marker, args.pass_marker, args.fail_marker)
    tree = tree_hash(args.root, args.scope)
    key = os.environ.get(RECEIPT_KEY_ENV, "")
    record = {"tree": tree, "output": output, "passed": passed, "written": time.time(),
              "time": datetime.datetime.now().isoformat(timespec="seconds"),
              "signature": receipt_signature(key, tree, output) if key else ""}
    os.makedirs(os.path.dirname(os.path.abspath(args.receipt)), exist_ok=True)
    with open(args.receipt, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, indent=1)


def parser():
    found = argparse.ArgumentParser()
    found.add_argument("--root", default=os.getcwd())
    found.add_argument("--slot", default="")
    found.add_argument("--leases", default="")
    found.add_argument("--forbid", action="append", default=[])
    found.add_argument("--deadline", type=float, default=0.0)
    found.add_argument("--started", type=float, default=0.0)
    found.add_argument("--logs", default="")
    found.add_argument("--test-call", default="")
    found.add_argument("--partial", action="append", default=[])
    found.add_argument("--full-timeout-ms", type=int, default=0)
    found.add_argument("--no-whole-read", action="append", default=[])
    found.add_argument("--receipt", default="")
    found.add_argument("--scope", action="append", default=[])
    found.add_argument("--totals-marker", default="passed, ")
    found.add_argument("--pass-marker", default=" 0 failed")
    found.add_argument("--fail-marker", action="append", default=[])
    found.add_argument("--post", action="store_true")
    found.add_argument("--config", default="")
    return found


def parse(argv=None):
    """The hook's arguments: those of the JSON file --config names (the driver writes one per session, so no regex has
    to survive a shell's quoting), under any given on the command line."""
    found = parser()
    first, _ = found.parse_known_args(argv)
    if first.config:
        values = read_receipt(first.config)
        found.set_defaults(**{key.replace("-", "_"): value for key, value in values.items()})
    return found.parse_args(argv)


def main(argv=None):
    args = parse(argv)
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if args.post:
        try:
            after_bash(event, args)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass   # why: without a receipt the gate runs the tests itself, so a failed write costs time, not safety
        return 0
    given = event.get("tool_input") or {}
    stripped = None
    if event.get("tool_name") == "Bash":
        stripped = strip_root_cd(given.get("command", ""), args.root)
    allow_cd = False
    if event.get("tool_name") == "Bash" and stripped is None:
        stripped = rewrite_cd(given.get("command", ""))
        allow_cd = stripped is not None
    if stripped is not None:
        event = dict(event, tool_input=dict(given, command=stripped))
    reason = decide(event, args, allow_cd)
    if stripped is not None and not reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                 "updatedInput": event["tool_input"]}}))
    elif reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
