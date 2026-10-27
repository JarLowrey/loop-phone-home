# The worker runner (`runner/worker_task.py`)

It hands one task at a time to a local model through Claude Code, watches it, and reports by phone. The driver does not use it; run it by hand or from the loop template.

The runner gives one task at a time to a local model and watches it. Everything project-specific is in a JSON config.

## Configure it

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
   - The driver's keys are listed in `runner/driver/loop_common.py` (`CONFIG_DEFAULTS`).
3. `topic_file` points at the alerts topic file (default `~/.claude/hooks/ntfy-topic.txt`). If it is missing, nothing is sent.
   `inbox_topic_suffix` defaults to an empty string, so alerts and commands use the same topic. Set it to `-inbox` (or
   another suffix) to poll a separate commands topic.

## Run it

```
python worker_task.py <task-id>                    a first attempt
python worker_task.py <task-id> <feedback-file>    a retry, with the last test output
python worker_task.py --status                     the live or last run's state, as JSON
python worker_task.py --stop                       stops the worker's process tree
python worker_task.py --revert                     saves, then undoes, uncommitted changes in the scope paths
```

A first attempt refuses to start if there are leftover changes in the scope paths. A run also refuses to start if the
model server, the model, Claude Code or the task is missing, or if another run is active. Each refusal says what to fix.

## What it watches

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
  on major or minor issues, so the worker can tidy them. Later rounds send it back only on major issues.
  `max_review_rounds` (default 2) is the number of reviews in all. Each review is saved as `<id>-<n>.md`,
  and each phone message carries a count of the issues and the first major ones. A review can also list NOTES: gaps the
  task leaves on purpose. They go to the loop's open questions and are never sent back to the worker.
- **Loop help.** The helper advice in the first bullet applies to every worker session, review rounds included.
- **Working directory.** The worker's shell is told to keep the project root as its working directory, and the prompt
  says never to `cd`. A stray `cd` used to put files in a nested folder, such as `x/x/...`, which the scope check reports.
- **Rules.** Files changed outside the scope paths, and fewer tests than before (when `test_count_glob` is set), trigger
  an alert.
- **Time.** `max_minutes` ends a run that has gone on too long.
- **Finish.** The script runs the test command itself, and reports the real totals, not the worker's claim.
- **Phone alerts.** Only the titles in `phone_titles` reach the phone: the alerts that need a person (stuck, blocked, model
  error, hand-off error, run not started). `notify_all` sends every alert. All of them are written to `<id>.notify.log`.
- **Inbox.** When it runs alone, it reads the configured inbox topic every `inbox_poll_interval` seconds. `STATUS` gets an
  immediate progress report, and `MAN` gets the command list. A message is matched after its case and spaces are
  normalised, so `status` counts, but extra words do not. Other messages are left unread. Under the driver it
  answers the inbox instead, and the worker stays quiet on it (`WORKER_UNDER_DRIVER=1`).

## What lands in the logs folder

Under `logs_dir`:

| File | Contents |
|---|---|
| `<id>.log` | The raw Claude Code stream, one JSON line per event. Thinking-token events are counted, not written. |
| `<id>.readable.log` | One line per event, with its time and source: `[worker]`, `[tool]`, `[claude-code]`, `[helper]` (the advice), `[script]`. |
| `<id>.helper-<n>.md` | The helper's advice for each loop. |
| `<id>.notify.log` | Every phone message sent, with its time and priority. |
| `worker-run.json` | The live status: task, attempt, session, process ids, state, phase, counts, loops, changed files, totals. |
| `revert-<time>/` | What `--revert` saved before it undid changes. |

## Why the runner is built this way

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
