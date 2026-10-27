# ntfy-er: phone alerts and replies for Claude Code and local-model loops

This folder holds the parts of a phone-notification setup for Claude Code work that runs unattended. It has three
parts that work together, and none of them is tied to one project:

| Part | Folder | What it does |
|---|---|---|
| Claude Code hook | `hook/` | Sends a phone alert when Claude Code asks for permission, or when a loop ends or needs a decision. |
| Worker runner | `runner/` | Hands one task at a time to a local model, through Claude Code, and reports its progress, stalls, loops, test results and rule breaks by phone. Answers `STATUS` and `MAN` from the phone while it runs. |
| Loop driver | `runner/driver/` | Runs a whole task queue in code, one fresh Claude session (or worker run) per task, and commits only what passes its own checks. Answers the phone, restarts itself, and stays within a daily budget. |
| Loop template | `loop-template/` | The prompt for an orchestrating Claude loop that plans tasks, hands them to the worker, verifies the results and commits. |

It was built for Windows, with Claude Code running in VS Code, a local model served by Ollama, and the orchestrating loop
on Claude. The runner works with any Anthropic-compatible model server, and the hook works with any Claude Code session.

Nothing in this folder names a project, a model, a path or a topic. Those come from your own config and topic files.

## Phone topics and secrets

By default, alerts and commands use one long random topic name as a shared secret:

- **Alerts:** the hook and runner post here.
- **Commands:** you post here from the ntfy app, and the loop and runner poll it.

Subscribe to that topic in the ntfy app. Anyone who knows the name can read alerts and post commands, so treat it like a
password and keep it out of git. The only file that holds the topic is `~/.claude/hooks/ntfy-topic.txt`.

To separate alerts and commands, set `inbox_topic_suffix` to `"-inbox"` in `worker.json`. The hook and runner continue
to post alerts to the base topic; the loop and runner poll `<base-topic>-inbox`. Subscribe to both topics if you want to
receive alerts and send commands from the app. You can use another suffix if needed.

## Setup

1. **Install the ntfy app** on your phone and open it.
2. **Make a random topic name**, for example in PowerShell:
   `'worker-loop-' + -join ((1..16) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })`
   Use at least 16 random characters. For separate topics, set `inbox_topic_suffix` to `"-inbox"` in `worker.json`;
   the inbox topic is derived from this name.
3. **Subscribe** to the topic in the ntfy app. For separate topics, subscribe to the inbox too if you want to see replies.
4. **Create** `%USERPROFILE%\.claude\hooks\ntfy-topic.txt` with only the topic name on one line. The hook and the
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
8. **Test the inbox path.** Post `STATUS` to the topic from the ntfy app, then read it back (append the configured
   `inbox_topic_suffix`, or nothing for the default same-topic setup):
   ```
   TOPIC=$(cat ~/.claude/hooks/ntfy-topic.txt)
   INBOX_SUFFIX='' # set to -inbox when using separate topics
   curl -s "https://ntfy.sh/${TOPIC}${INBOX_SUFFIX}/json?poll=1&since=1h"
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
   - Driver keys, used by the driver (see its section below): `loop_file`, `progress_file`, `open_task_pattern`,
     `rules_file`, `project_brief`, `sync`, `task_model`, `task_turns`, `task_timeout`, `test_timeout`,
     `test_function_regex`, `any_function_regex`, `assert_regex`, `deny_tools`, `usage_log`, `token_file`.
3. `topic_file` points at the alerts topic file (default `~/.claude/hooks/ntfy-topic.txt`). If it is missing, nothing is sent.
   `inbox_topic_suffix` defaults to an empty string, so alerts and commands use the same topic. Set it to `-inbox` (or
   another suffix) to poll a separate commands topic.

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

### The driver (`runner/driver/`)

The driver runs the open tasks in the task file one at a time, in code, with no orchestrating session. It reads the same
`worker.json` as the worker, and it uses the worker's module for the config and the inbox. Run it from the folder
containing `worker.json`, or set `WORKER_CONFIG` to that file. `<runner>` below is this repo's `runner` folder.

```
python <runner>/driver             the supervisor: runs the driver, and starts it again after a crash
python <runner>/driver --worker    as above, but the worker does each task (worker_task.py) and Haiku reviews it
python <runner>/driver --run       the driver alone, with no supervisor
python <runner>/driver --shutdown  stops the supervisor, the driver and its session, from the machine they run on
python <runner>/driver --model sonnet [high]   who does the tasks from the next one on (the MODEL command)
python <runner>/driver --approve   approves every call waiting for approval (--deny denies them)
python <runner>/driver --install-autostart     starts the driver at each logon (Windows Startup folder; elsewhere it
                                               prints a crontab line), unless you ended it last; --remove-autostart
python <runner>/driver/check_driver.py [lines] whether it runs, its task, its model, approvals, spend, the last log lines
python -m unittest discover -s driver/tests    the driver's own tests, from the runner folder
```

The folder is a package run through its `__main__.py`, one module per job:

| Module | Job |
|---|---|
| `mainloop.py` | the loop, the supervisor, the autostart |
| `runners.py` | one task: its snapshot, the session or worker run, the gate, then the commit, retry or block; the triage. `RUNNERS` lists the kinds of task, so a new kind is one more row |
| `commands.py` | the phone's and the machine's commands, approvals, shutdown |
| `tasks.py` | the task file, the progress file, commits and pushes, undoing a task, the run of blocks |
| `workspace.py` | the snapshot and every git check the gate makes against it |
| `sessions.py` | finding Claude Code, sessions, the test run with its timeout, answering the phone meanwhile |
| `budget.py` | usage limits, the daily budget, each session's cap |
| `settings.py`, `runtime.py`, `phone.py`, `prompts.py` | the settings block and limits; `driver.json`, the log and pid files; phone messages; every prompt text |

**Who does a task.** One session of `task_model` (default `sonnet`) does each task, with the normal Claude login, the
tools `Bash, Read, Edit, Write, Glob, Grep`, and the git commands in `deny_tools` refused (read-only git, `git stash list`
included, is allowed). It has `task_turns` turns, `task_timeout` seconds, and a budget by model ($3 Haiku, $8 Sonnet,
$30 Opus, $20 Fable). The phone's `MODEL` command changes the model and effort from the next task on, and the choice is
kept in `driver.json` across restarts. The old config names `haiku_turns`, `haiku_timeout` and `haiku_brief` are still
read. If a task fails, its changes are undone and `escalate_model` (default `opus`) tries it once more, with the failure
reason and the test output as feedback. A second failure blocks the task.

**Approvals.** With `approvals: on` (the default), every automated call to Opus at medium effort or more, and every call
to Fable, waits for the phone: an **Approve this call?** message (high) with Approve and Deny buttons. The buttons post
`APPROVE <code>` or `DENY <code>` to the inbox topic; the code is new for each request, so an old button cannot approve
a new call. No answer in `approval_hours` is a denial. A denied task stays open and the driver pauses; a denied retry
blocks the task with the first attempt's reason. What asks: the Opus tasks below, every task while `MODEL` is opus
(other than effort `low`) or fable, the retry of a failed task when `escalate_model` is opus or fable, and the triage.
With `approvals: off` nothing asks, and the budgets are the only limit.

**Opus tasks.** A task whose text contains a line `**Who.** Opus ...` runs as an Opus task in either mode, after its
approval. It uses the `OPUS_MODEL` environment variable (default `opus`), high effort, a $30 budget and a time limit. Its
work passes the same gate as any task, with the task and progress files also allowed, and it must have a
`**Done when.**` line with checks in backticks: a `file` path, a `test` name or an `output` text. Its output is saved as
`<id>.opus.md`.

Milestone reviews are explicit queue tasks, not automatic triggers at a milestone boundary. Add an Opus task after each
milestone's implementation tasks (for example, `M2-R`) that follows `loop-template/REVIEW.md`, then an Opus task (for
example, `M2-N`) that revises the next milestone's tasks against the code and fits in the fix tasks the review wrote.

**The snapshot.** Each task starts from a snapshot: the commit HEAD points at, and every path already changed. Every
check, revert and commit works from it, not from HEAD, so:
- your own uncommitted edits, the loop folder included, are left alone, and never swept into a task's commit;
- a commit made while a task runs (by hand, or by a session that got round `deny_tools`) pauses the driver with an
  urgent message, since the task's work may be in that commit, out of the checks' reach. The task stays open. Commit
  only while the driver is idle or paused.

**Checks before a commit (the gate).** The driver does not take a session's word for these:
- nothing changed outside `scope` and the loop folder;
- no file in the loop folder changed that the task's **Files.** or **Done when.** line does not name (Opus tasks may
  also change the task and progress files), so a session cannot edit the loop file or the task queue;
- no test was removed: by name (`test_function_regex`), and by count (`test_count_glob`, and the worker's own count);
- no test function makes fewer asserts than before (`assert_regex`, with `any_function_regex` marking where a test
  ends), unless the task names that function or its file: a task may rewrite the asserts it names, but no other test
  may be loosened to pass;
- the test command runs again now, once, within `test_timeout` seconds, and its totals line shows no failures. A run
  that hangs is killed with everything it started, and counts as failed;
- the task's `**Done when.**` line holds, when it has one (Opus tasks must have one). A `test` item is a function of that
  name in any file under the scope.

The three regexes default to Python, GDScript, JavaScript and Rust style (`def`, `func`, `function`, `fn`; `assert...`,
`expect(`, `fail(`). Set them in `worker.json` for another language.

A task that fails a check, or ends in a failing state, is reverted with a backup and marked `[!]`, and the reason goes in
the progress file's Blocked section. In `--worker` mode a state listed in `retry_states` is first retried with the test
output, up to `max_attempts`. A blocked task is not sent to the phone: the driver moves on, and the commit says why.

**A run of blocks.** Two tasks blocked in a row usually share a cause, such as a test that pins a number their specs
change. The driver then queues a **triage**: one Opus session ($10) reads the blocked tasks' logs, finds the shared
cause, and fixes it or rewrites the tasks, re-opening the ones it fixed. Its work passes the same gate and is committed,
or undone. If two more tasks are blocked after a triage, the driver pauses (urgent) instead of paying for more failures.
A commit ends the run.

**Crashes and restarts.** The supervisor starts the driver again after a crash: 1 minute after the first, doubling each
time. After 5 crashes that each came within 10 minutes of a start, it sends an urgent message, rests 2 hours, and tries
again. A task that raises an error the driver did not expect is blocked with the error and the loop goes on; 3 such tasks
in a row pause it. A clean end (`WIND DOWN`, `SHUTDOWN NOW`, `--shutdown`) ends the supervisor too, and marks the driver
as ended by you, so `--autostart` at the next logon leaves it stopped. After a reboot or a crash it starts again.

**Finding Claude Code.** A `claude_exe` that names a path is used as it is. The default `claude` is looked up on PATH,
and then in the newest VS Code extension folder, so an extension update does not break the driver.

**Spending.** Every session writes a line to `usage.jsonl`. No session may spend more than what is left of today's
`daily_budget_usd`; one that stops at that cut leaves its task open and pauses the driver until just after midnight,
instead of counting as a failure. When a day's recorded spending reaches the budget, the driver pauses until midnight
and sends an urgent message. If a session hits a Claude usage limit, the driver leaves the task open, sends an urgent
message, and resumes four minutes after the limit resets (or waits for `RESUME` when no reset time is given).

**Before a task starts** the scope must be clean: leftover changes in the scope are saved and undone. Changes outside
the scope become part of the snapshot and are left alone.

**Commit and sync.** After a pass, the driver commits the scope, the loop files the task names, the task file and the
progress file, with a `Co-Authored-By` line for the model that wrote it. If `sync` is true (the default is false) it
then runs `git push origin HEAD`. It never forces a push. If the push fails, the commit stays local and the phone gets a
"Sync failed" message.

**Loop settings.** The loop file (`loop_file`) holds a `driver-settings` block. The driver reads it at every pass, so a
change takes effect without a restart:

```driver-settings
poll_seconds: 60
max_attempts: 3
retry_states: tests_failed, failed, looped, timed_out, tests_did_not_run
escalate_model: opus
daily_budget_usd: 120
approval_hours: 12
approvals: on
```

`escalate_model: none` turns the retry off, `daily_budget_usd: 0` turns the budget off, and `approvals: off` lets every
automated call run without asking the phone.

**Phone messages.** The driver sends these whatever `phone_titles` says:

| Title | When |
|---|---|
| Committed | a task passed and was committed |
| Approve this call? | an automated Opus or Fable call waits for you (high, with buttons) |
| Loop paused: not approved | a call was denied, or not answered in time |
| Loop finished | no open task is left; send RESUME after you add some |
| Driver paused | a task could not start because its scope could not be cleaned |
| Driver paused: a commit was made during a task | HEAD moved while a task ran (urgent) |
| Triage queued, Triage committed, Triage: no change, Triage undone | a run of blocks, and what the triage did |
| Driver paused: tasks keep failing | more blocks after a triage (urgent) |
| Loop paused: daily budget reached, Loop paused: daily budget | today's spending reached `daily_budget_usd`, or cut a session short |
| Loop stopped: session usage exhausted | a Claude usage limit (urgent) |
| Sync failed | the push was rejected or timed out |
| Loop status, Commands, Loop task model, Approval | the answers to STATUS, MAN, MODEL and APPROVE or DENY |
| Loop paused, Loop resumed | STOP or PAUSE, RESUME or RESTART |
| Loop will exit, Loop exited | WIND DOWN |
| Loop shutting down | SHUTDOWN NOW |
| Loop driver crashed | the driver stopped on an error (urgent); the supervisor restarts it |
| Loop driver down | 5 quick crashes; the supervisor rests 2 hours, then tries again (urgent) |
| Driver paused: error, Driver paused: errors | a task error could not be blocked, or 3 came in a row (urgent) |

**Phone commands.** Each message is matched after its case and spaces are normalised.

| Command | Effect |
|---|---|
| `STATUS` | a progress report now, with the task model and today's spend |
| `MAN` | the command list |
| `STOP` or `PAUSE` | no new task starts until RESUME; the driver stays up |
| `RESUME` or `RESTART` | carry on |
| `WIND DOWN` | the driver ends once the current task is committed or undone (a running session is not cut short) |
| `SHUTDOWN NOW` | kills the running session at once, undoes the unfinished task, and ends the driver |
| `APPROVE <code>` or `DENY <code>` | the answer to an approval request |
| `MODEL` | reports who does the tasks; `MODEL <model> [effort]` changes it (haiku, sonnet, opus or fable) |
| `EFFORT <level>` | changes only the effort: low, medium, high, xhigh, max or default |

The commands are read every `poll_seconds`, including during a session, a worker run or a test run, where a thread
reads them, and every 15 seconds while an approval waits. A message that is not a command is ignored. A short one is
also logged in `driver.log`, in case it was a typo.

**Files** in `logs_dir`: `driver.log` (what the driver did, in order; one file a day, the older ones named
`driver-<date>.log`), `driver.json` (paused, busy, the current task and its snapshot, the task model, waiting
approvals, the run of blocks, heartbeat and any scheduled restart; written whole each time), `driver.pid` and
`driver-supervisor.pid`, `driver-request.txt` (commands from `--model`, `--approve` and `--deny` until the driver reads
them), `<id>.task.jsonl` and `<id>.<model>.md` (a task session's stream and final message), `<id>.opus.jsonl` and
`<id>.opus.md` (an Opus task's), `triage-<time>.triage.jsonl` (a triage's), `<id>.feedback.txt` (the test output for a
worker retry), `usage.jsonl` (one line per session), and `revert-<time>/` (what was undone).

**One driver per folder.** A second start refuses while the first one or its supervisor is still running.

**Limits.** The driver does not handle `ANSWER`, `APPROVED` or `REJECTED`, or the orchestrator's `[?]` tasks. Tasks done
by one model session are not peer-reviewed, so the gate above is their review.

## Driver or orchestrator?

There are two ways to run a task queue with this folder:

- **The driver** is a Python program. It starts one fresh Claude session per task and checks the result itself.
- **An orchestrator** is one long Claude session following `loop-template/LOOP.md`. It reads the queue, hands tasks
  out, judges the results and decides what to do next.

| | Driver | Orchestrator |
|---|---|---|
| **The checks before a commit** | Code, run the same way every time. A session cannot talk its way past them. | The model's judgement, which can drift, or be persuaded by its own earlier output. |
| **Cost between tasks** | None. Only the task sessions spend. | Every bookkeeping turn spends, and the context grows until it has to be compacted. |
| **Context** | Each task starts fresh, with only the rules and its own spec. | Earlier tasks' details, and their mistakes, carry forward. Compaction can drop a rule. |
| **When a session fails** | The task is undone and retried or blocked, and the next task starts. A supervisor restarts the driver itself. | The loop stops until someone restarts the session. |
| **Running unattended** | Answers the phone, restarts at logon, resumes after usage limits and the daily budget. | Needs a terminal kept open, and can stall on a permission prompt. |
| **Judgement between tasks** | Only what the code knows. An Opus task, or the triage after a run of blocks, adds judgement where it is needed. | It can spot a wrong spec before running it, reorder tasks, fix a broken test in passing, or stop and ask. |
| **Something unexpected** | It has code for the case, or the task is blocked. | It can reason about a case nobody planned for. |
| **Changing how it works** | A code change, with tests. | A prompt edit. |
| **What it handles** | Open tasks with a Done when line, `**Who.** Opus` tasks, and the phone commands above. | Everything the template has, `ANSWER`, `APPROVED`, `REJECTED` and `[?]` tasks included. |
| **Predictability** | Behaviour is code, with unit tests. | Behaviour shifts with the model, the prompt and the run. |

**Use the driver** when the tasks are well specified: worked examples, a **Files.** line and a **Done when.** line. Then
judgement between tasks adds little, and a gate that cannot be skipped catches what actually goes wrong: tests pinned to
a number the task changes, edits outside the scope, tests loosened to pass. Put the parts that need judgement into the
queue as Opus tasks, such as milestone reviews and task revisions. Then the queue gets judgement where it needs it, and
stays mechanical everywhere else.

**Use an orchestrator** when every step needs judgement. That covers writing a new milestone's tasks before their specs
are settled, debugging the loop itself, and any queue that uses the template's `ANSWER`/`APPROVED`/`REJECTED`
conversation with you.

## The loop template (`loop-template/LOOP.md`)

The template is the prompt for an orchestrating Claude loop that works through a task queue. The loop plans and verifies;
the worker writes the code. The template uses only the config's names, so it works with any `worker.json`.

The driver (`runner/driver/`) runs the same steps in code. See **Driver or orchestrator?** above for which to use. You need an orchestrating session only for the `ANSWER`, `APPROVED`, `REJECTED`
and `[?]` parts of the template. The template's driver section says which steps the driver follows.

Its steps, per iteration:
0. Read the configured phone inbox and act on the commands: fixed commands `STATUS`, `MAN`, `STOP`, `PAUSE` and
   `RESUME` must match exactly, ignoring case; `ANSWER:`, `APPROVED:` and `REJECTED:` require their exact prefix,
   ignoring case, followed by their text. Do not trim whitespace or accept extra text for fixed commands. A short
   unrecognised message is noted as a possible typo and answered on the phone; a long one is skipped.
   The loop also has a "Tasks waiting for you" step: a task whose tests pass but whose review still finds major issues
   is parked as `[?]` and waits for `APPROVED:` or `REJECTED:`.
1. Check whether a worker run is still going (`--status`). If so, wait.
2. Clear leftovers (`--revert`) rather than building on half-finished work.
3. Take the next open task from the queue.
4. Hand it to the worker in the background, and wait for its completion notification.
5. Verify from the status: `passed`, no out-of-scope changes, no removed tests. Then run the tests yourself.
6. Commit, or retry with the test output as feedback (three attempts), or mark the task blocked and revert.

Before a loop runs, check two things in the template: the `Co-Authored-By` trailer names your orchestrating model, and
the `<runner>` placeholder is the folder that holds `worker_task.py`.

## Security notes

- **Topic names are secrets.** With the default shared topic, anyone who knows its name can read alerts and send the commands. A separate inbox suffix splits the traffic, but is not an access-control boundary: on public `ntfy.sh`,
  anyone who knows the base topic can guess a predictable suffix and send commands. Use names of at least 16 random
  characters, and never commit them.
- **Lock the topic with a token.** Reserve the topic on an ntfy account and put an access token in the file
  `token_file` names (default `~/.claude/hooks/ntfy-token.txt`). The runner and the driver then send it on every
  call, and nobody without the token can read or post. Add the same token to the phone app's subscription.
- **The whitelist limits damage, not exposure.** A stranger who learns the topic used for commands can pause, stop or
  answer the loop, and can ask for a status report. Rotate the base topic if you suspect a leak.
- **ntfy.sh is a third party.** Alert text, up to 500 characters, passes through it. Keep secrets out of alerts. The hook
  sends the last assistant message, so loops should not print secrets. Self-host ntfy with access control if alerts may
  carry sensitive detail.
- **Task sessions can run shell commands too.** The driver gives each task session Bash. Its `deny_tools` list is
  the guard against git commands that change files or the index, because the driver does the commits. A prefix list
  can be got round (`bash -c "git commit ..."`), so the driver also pauses if a commit appears during a task, and
  undoes changes to the loop folder that the task does not name. It does not sandbox the shell: a session can still
  read or change files outside the repository.
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
| "a driver is already running" | A driver from this folder is alive, or `driver.pid` is stale | Send `SHUTDOWN NOW`, or `python <runner>/driver --shutdown`. If the pid in `driver.pid` is not a driver, delete the file. |
| Driver appears stuck or you need its current state | Check the driver's process and recent log output | Run `python <runner>/driver/check_driver.py` (or add `20` for more log lines). |
| "Driver paused: a commit was made during a task" | A commit landed while a task ran | Look at `git log`, then send RESUME. The task starts again; its leftovers in the scope are saved and undone first. |
| "Driver paused: tasks keep failing" | Tasks keep being blocked, even after a triage | Read the Blocked lines in the progress file and the triage's log, fix the shared cause, then send RESUME. |
| "Driver paused: ... changes are left over" | Changes in the scope could not be undone (a locked file, for example) | Look at the driver log, clear them by hand, then send RESUME. |
| "Sync failed" | The push was rejected (for example, the remote moved on) or timed out | Fix the remote and push by hand. The commit is already local, and the driver never forces a push. |
| "Loop stopped: session usage exhausted" | The Claude usage limit was reached during a Haiku or Opus task | Wait for the scheduled restart, or send RESUME once usage is available. If no restart time is shown, send RESUME manually. |
| Phone gets no alert from the worker | The title is not in `phone_titles` | Add it to `phone_titles`, or set `notify_all`. The alert is still in `<id>.notify.log`. |

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
- [ ] A command posted to the configured inbox topic is returned by the poll URL.
- [ ] The default empty `inbox_topic_suffix` polls the same topic used for alerts.
- [ ] A fixed command with extra text or whitespace is ignored; case-only changes are accepted.
- [ ] `worker_task.py --status` runs with a valid config and prints JSON or "no hand-off has run yet".
- [ ] A first attempt refuses to start while a leftover change exists in the scope paths.
- [ ] A run with a fake worker sends the start and finish messages and writes the readable log.
- [ ] The driver, with a task that changes a file outside the scope, commits nothing, reverts the change, and marks the task `[!]`.
- [ ] The driver answers `STATUS` and `MAN` with a phone message, and a long message that is not a command is ignored.
- [ ] The driver, with `SHUTDOWN NOW` during a task session, kills the session, leaves the task open, and exits.
- [ ] The driver, with a commit made by hand during a task, pauses and leaves the task open.
- [ ] `python <runner>/driver --install-autostart`, then a logon, starts the driver; after `WIND DOWN` it does not.
- [ ] A worker alert whose title is not in `phone_titles` does not reach the phone.
