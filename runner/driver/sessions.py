"""Claude sessions and test runs: starting one, reading its stream, killing it, and answering the phone while it runs."""
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import budget as B
import events as E
import loop_common as C
import runtime as R
import settings as S
import slots as SL

# the phone's command handler; commands.py sets it, so this module need not import that one
HANDLE_COMMANDS = None
TASK_HOOK = pathlib.Path(__file__).resolve().parent / "task_hook.py"
# the tools the task hook checks: the shell, and every tool that writes a file
HOOKED_TOOLS = "Bash|Edit|Write|MultiEdit|NotebookEdit"
# why: Claude Code moves a Bash command to the background after 2 minutes, and a test run with two slots takes 110 s or
# more, so a session's own test runs were cut off and polled; 10 minutes covers a run under load
BASH_TIMEOUT_MS = "600000"
# words in a tool result that mean the task hook refused the call, or Claude Code backgrounded a slow command
DENIAL_MARKERS = ("cd is refused", "another copy of the project", "This session works in its own copy")
BASH_TIMEOUT_MARKER = "did not complete within"


def task_settings(slot=None):
    """The --settings of a task session: the task hook, with its slot's root, lease file and the trees it must not
    touch. Passed with --settings, so only the driver's sessions get it, not the owner's own."""
    command = f'"{sys.executable}" "{TASK_HOOK}"'
    if slot is not None:
        command += f' --root "{slot.root}" --slot {slot.name} --leases "{SL.LEASES}" --logs "{C.CFG.logs_dir}"'
        command += "".join(f' --forbid "{tree}"' for tree in SL.other_roots(slot))
    return json.dumps({"env": {"BASH_DEFAULT_TIMEOUT_MS": BASH_TIMEOUT_MS, "BASH_MAX_TIMEOUT_MS": BASH_TIMEOUT_MS},
                       "hooks": {"PreToolUse": [{"matcher": HOOKED_TOOLS, "hooks": [
                           {"type": "command", "command": command, "timeout": 10}]}]}})


def kill_tree(pid):
    """Kills a process and everything it started, so no session is left behind."""
    if pid:
        C.kill_tree(pid)
        R.log(f"killed process tree {pid}")


@contextmanager
def polling():
    """Answers the phone from a thread while the body runs (a session or a test run), so STATUS, STOP, WIND DOWN and
    SHUTDOWN NOW work mid-task. Only the main thread needs it: while slots work in their own threads, the main thread's
    loop answers the phone itself."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    stop = threading.Event()
    threading.Thread(target=poll_while, args=(stop,), daemon=True).start()
    try:
        yield
    finally:
        stop.set()


def poll_while(stop):
    """Runs in its own thread while a session or test run runs: answers the phone's commands every POLL_SECONDS. SHUTDOWN
    NOW ends the whole driver from here."""
    while not stop.wait(S.POLL_SECONDS):
        try:
            HANDLE_COMMANDS(R.RUNTIME["state"])
            R.write_driver_state(R.RUNTIME["state"])
        except SystemExit:
            R.DRIVER_PID.unlink(missing_ok=True)
            R.log("driver ended by SHUTDOWN NOW")
            os._exit(0)
        except Exception as error:
            R.log(f"command poll failed: {error}")


def run_tests(root=None):
    """The test run in the main tree or a slot, with the phone answered meanwhile, since a run takes minutes."""
    with polling():
        return C.test_output(root=root)


def tests_summary(output):
    """The failures and the totals of a test run's output, for a retry's feedback: the lines that say fail or error, and
    the totals line."""
    keep = [line for line in (output or "").splitlines()
            if "fail" in line.lower() or "error" in line.lower() or C.CFG.totals_marker in line]
    return "\n".join(keep[-60:]) or "(no test output)"


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


class SessionStats:
    """What a session did, counted from its stream for the session event: tool calls by name, failed tool calls, the
    task hook's refusals, Bash commands Claude Code cut off, and the session's own test runs."""

    def __init__(self):
        self.tools = {}
        self.tool_errors = 0
        self.denials = []
        self.bash_timeouts = 0
        self.test_runs = 0

    def read(self, event):
        message = event.get("message") if isinstance(event, dict) else None
        if isinstance(event, dict) and event.get("type") == "system" and "denied" in str(event.get("subtype", "")):
            self.denials.append(str(message)[:160])
        content = message.get("content") if isinstance(message, dict) else None
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use":
                name = str(part.get("name"))
                self.tools[name] = self.tools.get(name, 0) + 1
                if name == "Bash" and C.TEST_MARKER in json.dumps(part.get("input")):
                    self.test_runs += 1
            elif part.get("type") == "tool_result":
                text = json.dumps(part.get("content")) if not isinstance(part.get("content"), str) else part["content"]
                if part.get("is_error"):
                    self.tool_errors += 1
                if any(marker in text for marker in DENIAL_MARKERS) or "is being changed by task" in text:
                    self.denials.append(text[:160])
                if BASH_TIMEOUT_MARKER in text:
                    self.bash_timeouts += 1


def session_event(stats, result, model, effort, kind, seconds, raw_log):
    """Records one session in events.jsonl: its cost, tokens, why it ended and what it did."""
    usage = result.get("usage") or {}
    E.emit("session", model=model, effort=effort, kind=kind, seconds=seconds, turns=result.get("num_turns"),
           cost_usd=result.get("total_cost_usd"), is_error=bool(result.get("is_error")) or not result,
           subtype=result.get("subtype"), terminal_reason=result.get("terminal_reason"),
           tokens_in=usage.get("input_tokens"), tokens_out=usage.get("output_tokens"),
           cache_read=usage.get("cache_read_input_tokens"), cache_write=usage.get("cache_creation_input_tokens"),
           tools=stats.tools, tool_errors=stats.tool_errors, denials=stats.denials[:10],
           bash_timeouts=stats.bash_timeouts, test_runs=stats.test_runs, log=raw_log.name)


def run_session(prompt, task_id, model, effort, turns, timeout, budget, kind, slot=None):
    """Runs one Claude session with tools, in its slot's worktree (the main tree without one). Each step is logged as it
    happens, and the raw stream is kept in <logs>/<task>.<kind>.<model>.<time>.jsonl, one file per session, so a
    retry or a requeue never overwrites an earlier attempt's. Returns (text, error); one of them is empty."""
    command = [C.CLAUDE, "-p", "--model", model, "--tools", "Bash,Read,Edit,Write,Glob,Grep",
               "--allowedTools", "Bash", "Read", "Edit", "Write", "Glob", "Grep",
               "--disallowedTools", *C.DISALLOWED_TOOLS, "--disable-slash-commands", "--strict-mcp-config",
               "--output-format", "stream-json", "--verbose", "--max-turns", str(turns),
               "--max-budget-usd", str(budget), "--settings", task_settings(slot)]
    if effort:
        command += ["--effort", effort]
    key = slot.name if slot is not None else kind
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               cwd=slot.root if slot is not None else C.PROJECT, env=C.helper_env(), text=True,
                               encoding="utf-8", errors="replace", bufsize=1)
    R.CHILDREN[key] = process.pid
    timer = threading.Timer(timeout, kill_tree, args=(process.pid,))
    timer.start()
    process.stdin.write(prompt)
    process.stdin.close()
    started = time.time()
    steps = 0
    result = {}
    texts = []
    stats = SessionStats()
    raw_log = C.LOGS / f"{task_id}.{kind}.{model}.{E.stamp()}.jsonl"
    R.ctx()["session_log"] = raw_log.name
    try:
        with polling(), open(raw_log, "w", encoding="utf-8", newline="\n") as raw:
            for line in process.stdout:
                raw.write(line)
                try:
                    event = json.loads(line)
                except ValueError:
                    R.log(f"{model} output: {line.rstrip()[:200]}")
                    continue
                stats.read(event)
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
        R.CHILDREN.pop(key, None)
        timer.cancel()
        B.release_reservation()
    seconds = round(time.time() - started)
    R.ctx()["last_session"] = {"turns": result.get("num_turns") or 0, "seconds": seconds, "steps": steps}
    session_event(stats, result, model, effort, kind, seconds, raw_log)
    if stats.bash_timeouts:
        R.log(f"{model}: {stats.bash_timeouts} Bash command(s) ran past Claude Code's timeout and went to the background")
    if result:
        C.log_usage({"task": task_id, "who": model, "kind": kind, "effort": effort,
                     "slot": slot.name if slot is not None else "", "is_error": bool(result.get("is_error")),
                     "turns": result.get("num_turns"), "seconds": seconds,
                     "cost_usd": result.get("total_cost_usd"), "usage": result.get("usage")})
        R.log(f"{model} finished in {seconds} s: {result.get('num_turns')} turns, cost {result.get('total_cost_usd')} "
              f"USD, error {bool(result.get('is_error'))}, {result.get('subtype', '')}")
    if not result or result.get("is_error") or not result.get("result"):
        return "", failure_text(model, code, seconds, result)
    return str(result["result"]).strip() or "\n".join(texts), ""


def failure_text(model, code, seconds, result):
    """Why a session failed, in one line. A session stopped by a limit (max_turns, a budget) says which, with its turns,
    since its result text is often empty and "exit 1" alone reads as a crash."""
    detail = str(result.get("result") or result.get("subtype") or "no result")[:300]
    reason = result.get("terminal_reason") or ""
    stopped = f", stopped by {reason} after {result.get('num_turns')} turns" if reason and reason != "completed" else ""
    return f"the {model} session ended with exit {code} after {seconds} s{stopped}: {detail}"
