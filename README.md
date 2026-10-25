# ntfy-er: phone alerts and replies for Claude Code and local-model loops

This folder holds the parts of a phone-notification setup for Claude Code work that runs unattended. It has three
parts that work together, and none of them is tied to one project:

| Part | Folder | What it does |
|---|---|---|
| Claude Code hook | `hook/` | Sends a phone alert when Claude Code asks for permission, or when a loop ends or needs a decision. |
| Worker runner | `runner/` | Hands one task at a time to a local model, through Claude Code, and reports its progress, stalls, loops, test results and rule breaks by phone. Answers `STATUS` and `MAN` from the phone while it runs. |
| Loop template | `loop-template/` | The prompt for an orchestrating Claude loop that plans tasks, hands them to the worker, verifies the results and commits. |

It was built for Windows, with Claude Code running in VS Code, a local model served by Ollama, and the orchestrating loop
on Claude. The runner works with any Anthropic-compatible model server, and the hook works with any Claude Code session.

Nothing in this folder names a project, a model, a path or a topic. Those come from your own config and topic files.

## Phone topics and secrets

Two topics, both long random names that act as shared secrets:

- **Alerts topic:** the hook and the runner post here. You subscribe to it in the ntfy app.
- **Inbox topic:** you post commands here from the ntfy app. The loop and the runner read it. The inbox name is the alerts
  topic plus `-inbox`.

Anyone who knows a topic name can read it (alerts) or post to it (inbox). Treat both names like passwords, and keep them
out of git. The only file that holds the alerts topic is `~/.claude/hooks/ntfy-topic.txt`.

## Setup

1. **Install the ntfy app** on your phone and open it.
2. **Make a random topic name** for alerts, for example in PowerShell:
   `'worker-loop-' + -join ((1..16) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })`
   Make a second one for the inbox, which the runner derives as `<name>-inbox`. Use names of at least 16 random characters.
3. **Subscribe** to the alerts topic in the ntfy app. Subscribe to the inbox topic too, if you want to see your replies.
4. **Create** `%USERPROFILE%\.claude\hooks\ntfy-topic.txt` with only the alerts topic name on one line. The hook and the
   runner trim it, so a trailing newline is fine. Use `hook/ntfy-topic.example.txt` as the model.
5. **Copy the hook**: `hook/phone.ps1` to `%USERPROFILE%\.claude\hooks\phone.ps1`.
6. **Register the hook** by merging `hook/settings-hooks.example.json` into `%USERPROFILE%\.claude\settings.json`.
   Replace `C:/Users/YOU/` with your own path. Keep your other settings, such as `model`; the `hooks` key is the only
   part this change adds.
7. **Test the alert path** without Claude. This message should alert:
   ```
   echo '{"hook_event_name":"Stop","last_assistant_message":"Test.\nLOOP_DONE 1 test finished."}' | powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$HOME/.claude/hooks/phone.ps1"
   ```
   A message that only *mentions* `LOOP_DONE` mid-sentence must not alert:
   ```
   echo '{"hook_event_name":"Stop","last_assistant_message":"I mention LOOP_DONE in prose."}' | powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$HOME/.claude/hooks/phone.ps1"
   ```
8. **Test the inbox path.** Post `Test` to the inbox topic from the ntfy app, then read it back:
   ```
   TOPIC=$(cat ~/.claude/hooks/ntfy-topic.txt)
   curl -s "https://ntfy.sh/${TOPIC}-inbox/json?poll=1&since=1h"
   ```

## The hook, step by step

`phone.ps1` does three things.

1. **Reads the event.** It reads stdin as JSON. The hooks reference documents `hook_event_name`, and
   `last_assistant_message` on `Stop`. The `message` field used for `Notification` is not documented there, so the script
   falls back to a generic text when it is missing.
2. **Decides whether to alert.**
   - `Notification` always alerts, at high priority. The settings file matches only permission prompts and elicitation
     dialogs, so these are the only notifications you get.
   - `Stop` alerts only when a **line** of the last assistant message starts with `LOOP_DONE` or `NEED_INPUT`. The check
     is per line with `^\s*MARKER\b`, so prose that mentions a marker mid-sentence stays silent.
3. **Posts.** It sends the text, capped at 500 characters, to `https://ntfy.sh/<topic>` with `Title` and `Priority`
   headers. Headers must be plain ASCII. The script always exits 0, so a failed send never blocks Claude Code.

## The worker runner (`runner/worker_task.py`)

The runner gives one task at a time to a local model and watches it. Everything project-specific is in a JSON config.

### Configure it

1. Copy `runner/worker.example.json` to `worker.json` next to the script, or to any path, and set `WORKER_CONFIG` to it.
2. Fill in the keys. The important ones:
   - `project_root`: the git repository root. Relative paths in the config are relative to the config file.
   - `tasks_file`: the task queue. A task is a heading matching `task_heading` (default `### [ ] <id> `), with its text
     running to the next heading or a `---` line.
   - `scope`: the path prefixes the worker may change. Anything else it changes is reported as out of scope.
   - `test_command`: the command that runs the tests, from `project_root`. It must print a line containing
     `totals_marker` (default `passed, `) with `pass_marker` (default ` 0 failed`) when everything passes.
   - `failure_regex`: names a failing test in the output, so repeated failures can be spotted.
   - `worker_model` (required), `model_server` and `claude_exe`: the local model, its Anthropic-compatible server, and the
     Claude Code executable. `WORKER_MODEL` and `CLAUDE_EXE` override them without editing the file.
   - `project_rules`: lines added to the worker's prompt, for language, style and shell notes.
   - `helper_model`: the model used for loop advice and reviews. It only reads files.
   - `max_review_rounds` and `review_references`: how many reviews a change gets, and the documents the
     reviewer checks it against.
   - Timers and limits: `max_minutes`, `progress_interval`, `early_progress_interval`, `stuck_check_interval`,
     `loop_idle_minutes`, `max_assists`.
3. `topic_file` points at the alerts topic file (default `~/.claude/hooks/ntfy-topic.txt`). If it is missing, nothing is sent.

### Run it

```
python worker_task.py <task-id>                    a first attempt
python worker_task.py <task-id> <feedback-file>    a retry, with the last test output
python worker_task.py --status                     the live or last run's state, as JSON
python worker_task.py --stop                       stops the worker's process tree
python worker_task.py --revert                     saves, then undoes, uncommitted changes in the scope paths
```

A first attempt refuses to start if there are leftover changes in the scope paths. A run also refuses to start if the
model server, the model, Claude Code or the task is missing, or if another run is active. Each refusal says what to fix.

### What it watches

- **Progress.** Start, then the first two progress messages every `early_progress_interval`, then one every
  `progress_interval`. Each says what the worker is doing right now, for how long, and how much it has changed.
- **Phase.** It reads the Claude Code event stream: thinking (thinking-token events), writing text, running a tool (a tool
  call with no result yet), or waiting for the model (a tool result went back and nothing has come out since).
- **Quiet streams.** When nothing arrives for `stuck_check_interval`, it reads the GPU load, where `nvidia-smi` exists.
  "Tool still running", "quiet but working" (GPU busy) and "may be stuck" (GPU idle) are told apart.
- **Loops.** The same tool call three times with no edit between, the same line of text four times, the tests three
  times in a row with the same failures, or `loop_idle_minutes` of waiting with no tool call. When one shows, the session
  is stopped. A helper model reads the task, the test output, the diff and the recent actions, and gives short advice
  (saved as `<id>.helper-<n>.md`). The worker restarts with that advice, up to `max_assists` times.
- **Review.** After the tests pass, a helper model checks the change against the task and the documents in
  `review_references`. It sorts its findings into major and minor issues. Round 1 sends the change back
  on major or minor issues, so the worker can tidy them. Rounds 2 and 3 send it back only on major issues.
  `max_review_rounds` (default 3) is the number of reviews in all. Each review is saved as `<id>-<n>.md`,
  and each phone message carries a count of the issues and the first major ones.
- **Rules.** Files changed outside the scope paths, and fewer tests than before (when `test_count_glob` is set), trigger
  an alert.
- **Time.** `max_minutes` ends a run that has gone on too long.
- **Finish.** The script runs the test command itself, and reports the real totals, not the worker's claim.
- **Inbox.** Every `inbox_poll_interval` seconds it reads the inbox. `STATUS` gets an immediate progress report, and `MAN`
  gets the command list. Other commands are left unread for the loop.

### What lands in the logs folder

Under `logs_dir`:

| File | Contents |
|---|---|
| `<id>.log` | The raw Claude Code stream, one JSON line per event. Thinking-token events are counted, not written. |
| `<id>.readable.log` | One line per event, with its time and source: `[worker]`, `[tool]`, `[claude-code]`, `[helper]` (the advice), `[script]`. |
| `<id>.helper-<n>.md` | The helper's advice for each loop. |
| `<id>.notify.log` | Every phone message sent, with its time and priority. |
| `worker-run.json` | The live status: task, attempt, session, process ids, state, phase, counts, loops, changed files, totals. |
| `revert-<time>/` | What `--revert` saved before it undid changes. |

### Why the runner is built this way

Each point below came from a real failure, and the code handles each one:
- **Prompts go in on standard input, not the command line.** Windows caps a command line at 32,767 characters, and a large
  diff passed that limit.
- **The run ends when the Claude process exits, not when its output pipe closes.** A child process that keeps the pipe
  open would otherwise hold the script, and the end-of-run messages would never go out.
- **Blocked tool calls are detected and reported.** A headless session cannot answer an approval prompt, so a blocked
  command looks like a silent failure. The `allowed_tools` list is the gate.
- **Thinking-token events are not logged one by one.** A 9-billion-parameter model produced over 40,000 of them on one
  task, and they filled the logs.
- **Process ids are checked the way the platform expects.** On Windows the runner uses `tasklist` and `taskkill`. Git Bash's
  `kill` uses different process ids and silently does nothing.
- **The model server is checked before a run starts,** and a missing model is reported by name, not as a confusing failure
  in the middle of the run.

## The loop template (`loop-template/LOOP.md`)

The template is the prompt for an orchestrating Claude loop that works through a task queue. The loop plans and verifies;
the worker writes the code. The template uses only the config's names, so it works with any `worker.json`.

Its steps, per iteration:
0. Read the phone inbox and act on the six commands, in any case: `STATUS`, `MAN`, `STOP`, `PAUSE`, `RESUME` and
   `ANSWER: <text>`. Anything else is noted and ignored.
1. Check whether a worker run is still going (`--status`). If so, wait.
2. Clear leftovers (`--revert`) rather than building on half-finished work.
3. Take the next open task from the queue.
4. Hand it to the worker in the background, and wait for its completion notification.
5. Verify from the status: `passed`, no out-of-scope changes, no removed tests. Then run the tests yourself.
6. Commit, or retry with the test output as feedback (three attempts), or mark the task blocked and revert.

Before a loop runs, check two things in the template: the `Co-Authored-By` trailer names your orchestrating model, and
the `<runner>` placeholder is the folder that holds `worker_task.py`.

## Security notes

- **Topic names are secrets.** Anyone who knows the alerts topic can read the alerts. Anyone who knows the inbox topic can
  send the six commands. Use names of at least 16 random characters, and never commit them.
- **The whitelist limits damage, not exposure.** A stranger who learns the inbox name can pause, stop or answer the loop,
  and can ask for a status report. Rotate the inbox name if you suspect a leak.
- **ntfy.sh is a third party.** Alert text, up to 500 characters, passes through it. Keep secrets out of alerts. The hook
  sends the last assistant message, so loops should not print secrets. Self-host ntfy with access control if alerts may
  carry sensitive detail.
- **The worker can run shell commands.** With the default `allowed_tools`, the worker can run any Bash command in the
  session. The limits are the scope check, the tool list and the test rules, not a command list. Narrow `allowed_tools`
  in the config if you want a tighter gate; a blocked command then shows up as an approval alert.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No alert from a `Stop` | The marker is not at the start of a line, or it is in prose | Put the marker on its own line. |
| False "Loop finished" alert | A message has `LOOP_DONE` at the start of a line | Reword so the marker does not begin a line. |
| Hook does nothing at all | Topic file missing, or the path in settings is wrong | Check that `ntfy-topic.txt` exists and that the path in `settings.json` points at `phone.ps1`. |
| "config ... is missing" | `scope`, `test_command` or `worker_model` is empty in the config | Fill in the keys the message names. |
| "not started: Claude Code not found" | `claude_exe` is wrong | Set `claude_exe`, or `CLAUDE_EXE`, to the full path. |
| "not started: the model server ..." | Model server not running, or the model is not built | Start the server, and build or pull the model. |
| "approval" alert | A tool call was blocked, usually a command outside `allowed_tools` | Widen `allowed_tools`, or change the prompt to use an allowed command. |
| Run stops with `looped` | The helper's advice did not help twice | Read `<id>.helper-<n>.md`, improve the task text, and retry with feedback. |
| Inbox message never handled | The loop is not running, or the message is not one of the commands | Check the progress file for the "ignored inbox message" note. |

Two bugs found while building this, so they are not reintroduced:
- In PowerShell double quotes, `"`r?`n"` does not mean CRLF, because backticks are escapes there. Use single quotes with a
  regex instead: `-split '\r?\n'`.
- Writing a script through a Python string containing `\b` or `\r` put control characters into the file. Edit files with a
  file tool or a quoted heredoc, and test after every change.

## Testing checklist

- [ ] A prose mention of `LOOP_DONE` produces no alert.
- [ ] A line-start `LOOP_DONE` produces a "Loop finished" alert.
- [ ] A line-start `NEED_INPUT:` produces a "Loop needs a decision" alert at high priority.
- [ ] A permission prompt produces a "Claude needs you" alert.
- [ ] A test message on the inbox topic is returned by the poll URL.
- [ ] `worker_task.py --status` runs with a valid config and prints JSON or "no hand-off has run yet".
- [ ] A first attempt refuses to start while a leftover change exists in the scope paths.
- [ ] A run with a fake worker sends the start and finish messages and writes the readable log.
