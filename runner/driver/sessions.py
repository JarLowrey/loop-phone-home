"""Claude sessions and test runs: finding Claude Code, starting a session, reading its stream, killing it, the driver's
own test run with a timeout, and answering the phone while any of them runs."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager

import runtime as R
import settings as S
import worker_task as W

C = W.C
EXTENSIONS = pathlib.Path.home() / ".vscode" / "extensions"
# the phone's command handler; commands.py sets it, so this module need not import that one
HANDLE_COMMANDS = None


def version_key(folder):
    """The numbers in an extension folder's version, so 2.1.310 sorts after 2.1.99."""
    match = re.search(r"claude-code-([\d.]+)", folder.name)
    return tuple(int(part) for part in match.group(1).split(".") if part) if match else ()


def claude_exe():
    """Claude Code: claude_exe from the config (or CLAUDE_EXE) when it names a path, else claude on PATH, else the newest
    VS Code extension's copy, so an extension update does not break the driver."""
    if C.claude_exe and C.claude_exe != "claude":
        return C.claude_exe
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    found = sorted(EXTENSIONS.glob("anthropic.claude-code-*/resources/native-binary/claude*"),
                   key=lambda path: version_key(path.parent.parent.parent))
    return str(found[-1]) if found else "claude"


@contextmanager
def polling():
    """Answers the phone from a thread while the body runs (a session, a worker run or a test run), so STATUS, STOP,
    WIND DOWN and SHUTDOWN NOW work mid-task."""
    stop = threading.Event()
    threading.Thread(target=poll_while, args=(stop,), daemon=True).start()
    try:
        yield
    finally:
        stop.set()


def poll_while(stop):
    """Runs in its own thread: answers the phone every poll_seconds. SHUTDOWN NOW ends the whole driver from here."""
    while not stop.wait(S.get("poll_seconds")):
        try:
            HANDLE_COMMANDS(R.RUNTIME["state"])
            R.write_state(R.RUNTIME["state"])
        except SystemExit:
            R.PATHS["pid"].unlink(missing_ok=True)
            R.log("driver ended by SHUTDOWN NOW")
            os._exit(0)
        except Exception as error:
            R.log(f"command poll failed: {error}")


def test_output():
    """The test command's output. A run longer than test_timeout is killed with everything it started, and its output
    then has no totals line, so it counts as failed."""
    if not C.final_tests:
        return "(test runs are switched off: WORKER_FINAL_TESTS=0)"
    group = {} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(C.test_command, shell=isinstance(C.test_command, str), cwd=C.project,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", **group)
    try:
        out, _ = process.communicate(timeout=C.test_timeout)
    except subprocess.TimeoutExpired:
        W.kill_tree(process.pid)
        process.communicate()
        return f"test run timed out after {C.test_timeout} s"
    return out.strip()


def run_tests():
    """The driver's own test run, with the phone answered meanwhile."""
    with polling():
        return test_output()


def event_parts(event):
    """What one stream-json event says, as (kind, value) pairs: ("say", text), ("step", tool and input),
    ("tool_result", text), ("system", subtype and message) and ("result", the event). Any other shape gives nothing,
    so an event the parser does not know never stops a session."""
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    message = event.get("message")
    if kind == "result":
        return [("result", event)]
    if kind == "system":
        # why: a system event (such as permission_denied) carries its message as a plain string
        return [("system", f"{event.get('subtype', 'system')}: {message}")] if isinstance(message, str) else []
    content = message.get("content") if isinstance(message, dict) else None
    parts = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if kind == "assistant" and part.get("type") == "text" and str(part.get("text", "")).strip():
            parts.append(("say", str(part["text"]).strip()))
        elif kind == "assistant" and part.get("type") == "tool_use":
            parts.append(("step", f"{part.get('name')} {json.dumps(part.get('input'))[:160]}"))
        elif kind == "user" and part.get("type") == "tool_result":
            parts.append(("tool_result", str(part.get("content"))[:120].replace("\n", " | ")))
    return parts


def run_session(prompt, task_id, model, effort, turns, timeout, budget, kind):
    """Runs one Claude session with tools. Each step is logged as it happens, and the raw stream is kept in
    <task>.<kind>.jsonl. Returns (text, error); one of them is empty."""
    command = [claude_exe(), "-p", "--model", model, "--tools", S.TASK_TOOLS, "--allowedTools",
               *S.TASK_TOOLS.split(","), "--disable-slash-commands", "--strict-mcp-config", "--output-format",
               "stream-json", "--verbose", "--max-turns", str(turns), "--max-budget-usd", str(budget)]
    if effort:
        command += ["--effort", effort]
    if C.deny_tools:
        command += ["--disallowedTools", *C.deny_tools]
    group = {} if os.name == "nt" else {"start_new_session": True}   # its own process group, so the kill reaches it all
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               cwd=C.project, env=W.helper_env(), text=True, encoding="utf-8", errors="replace",
                               bufsize=1, **group)
    R.CHILD["pid"] = process.pid
    timer = threading.Timer(timeout, W.kill_tree, args=(process.pid,))
    timer.start()
    process.stdin.write(prompt)
    process.stdin.close()
    started = time.time()
    steps = 0
    result = {}
    texts = []
    try:
        with polling(), open(C.logs / f"{task_id}.{kind}.jsonl", "w", encoding="utf-8", newline="\n") as raw:
            for line in process.stdout:
                raw.write(line)
                try:
                    event = json.loads(line)
                except ValueError:
                    R.log(f"{model} output: {line.rstrip()[:200]}")
                    continue
                for part, value in event_parts(event):
                    if part == "result":
                        result = value
                    elif part == "say":
                        texts.append(value)
                        R.log(f"{model} says: {value[:200]}")
                    elif part == "step":
                        steps += 1
                        R.log(f"{model} step {steps}: {value}")
                    elif part == "tool_result":
                        R.log(f"{model} tool result: {value}")
                    else:
                        R.log(f"{model} {value[:200]}")
            code = process.wait()
    finally:
        R.CHILD["pid"] = None
        timer.cancel()
    seconds = round(time.time() - started)
    if result:
        W.log_usage({"who": model, "kind": kind, "effort": effort, "is_error": bool(result.get("is_error")),
                     "turns": result.get("num_turns"), "seconds": seconds,
                     "cost_usd": result.get("total_cost_usd"), "usage": result.get("usage")})
        R.log(f"{model} finished in {seconds} s: {result.get('num_turns')} turns, cost {result.get('total_cost_usd')} "
              f"USD, error {bool(result.get('is_error'))}, {result.get('subtype', '')}")
    if not result or result.get("is_error") or not result.get("result"):
        detail = str(result.get("result") or result.get("subtype") or "no result")[:300]
        return "", f"the {model} session ended with exit {code} after {seconds} s: {detail}"
    return str(result["result"]).strip() or "\n".join(texts), ""
