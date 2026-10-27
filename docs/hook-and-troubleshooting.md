# The phone hook, step by step

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
| "Driver paused: a commit was made during a task" | A task's session committed inside its slot | Look at `git log` in the slot (the message names it), then send RESUME. The task starts again from a clean slot. |
| "Driver waiting for the main tree" | One of your uncommitted files is in the way of a landing, or the main tree is on another branch | Commit or stash the file it names, or check out the branch again; it lands within a minute. |
| "Many tasks requeued" | Work keeps conflicting with work that landed meanwhile | Lower `parallel_slots` or `parallel_window`, or add `**After.**` lines; `report.py` shows which tasks. |
| "Clean checkout fails" | The tests pass in the slots but not in a fresh copy | A file the tests need is not committed, or a cache in `cache_dirs` hid it. |
| "Driver paused: tasks keep failing" | Tasks keep being blocked, even after a triage | Read the Blocked lines in the progress file and the triage's log, fix the shared cause, then send RESUME. |
| "slot N is not clean after its reset" | A file in the slot could not be reset (a locked file, for example) | Close what holds it, or delete the slot's folder (`slots_dir`); the driver makes it again. |
| "Sync failed" | The push was rejected (the remote moved on) or timed out | Run `git pull --rebase` in your tree when no edit is open; the next push carries both. The driver never forces a push. |
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
- [ ] The driver, with a task that changes a file outside the scope, lands nothing, undoes the change in its slot, and marks the task `[!]`.
- [ ] The driver answers `STATUS` and `MAN` with a phone message, and a long message that is not a command is ignored.
- [ ] The driver, with `SHUTDOWN NOW` during a task session, kills the session, leaves the task open, and exits.
- [ ] The driver, with an uncommitted edit of yours to the task file, waits to land and tells the phone which file.
- [ ] `python -m unittest discover -s runner/driver/tests` passes: it lands work on real git repositories in temp
      folders, with two slots side by side.
- [ ] `python <runner>/driver --install-autostart`, then a logon, starts the driver; after `WIND DOWN` it does not.
- [ ] A worker alert whose title is not in `phone_titles` does not reach the phone.
