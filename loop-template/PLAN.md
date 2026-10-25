# Loop plan

An autonomous loop builds a project from a task queue. An orchestrating Claude session plans, hands each task to a local
worker model, verifies the result and commits. Every finished task is one commit, so any of them can be reviewed or
reverted.

| Phase | Who | What | Ends when |
|---|---|---|---|
| 0 · Setup | You, with the orchestrator | The project, its test command, the task queue, the loop prompt (`LOOP.md`), the worker config (`worker.json`) | The first task runs |
| 1 · Build | Loop | Tasks in order. Each is handed to the worker, tested by the script, verified by the orchestrator, then committed | Queue empty, or three blocked tasks in a row, or a stop message |
| 2 · Backup queue | Loop | Tasks that do not depend on the open design questions. Keep the earlier tests passing | Queue empty or blocked |
| 3 · Review | You | Read the progress file and `git log`, fix blocked tasks, decide the open questions | Decisions made |

## How a task moves through it

1. **Hand off.** `worker_task.py <id>` starts the worker in the background. The script checks the model server, the model,
   Claude Code, the task and a clean scope before it starts.
2. **Watch.** The script reports progress every few minutes, flags quiet or stuck runs, answers `STATUS` and `MAN` from
   the phone, and stops a run that goes past `max_minutes`.
3. **Loop check.** When the worker repeats itself, the script stops that session, asks a helper model for short advice, and
   restarts the worker with it. This happens at most `max_assists` times per run.
4. **Verify.** The script runs the test command itself and checks the scope and the test count. The orchestrator checks
   the same status, runs the tests again, and reads the diff.
4a. **Review.** After the tests pass, a helper model checks the change against the task and the project's
   documents. It lists major and minor issues. Round 1 sends back on either; later rounds only on major
   issues. The worker gets up to `max_review_rounds` reviews in all, each with the review and the tests.
5. **Retry or commit.** A failure is retried with the test output as feedback, up to three attempts. A pass is committed.
   A task that still fails is reverted and marked blocked.

## Rules that keep it honest
- The worker's own claim of success is never trusted. The script's test run decides.
- Changes outside the scope paths, or fewer tests than before, are failures even when the tests pass.
- A first attempt starts from a clean scope. Leftovers are reverted, not built on.
- Only one run at a time. Stop a run with `--stop`, never by killing processes in another shell.
- Phone commands are limited to six, in any case. Anything else is ignored and recorded.

## Why it is built this way
- **Small tasks with tests.** Each task names its files, behaviour and a test. A local model is reliable on that, and
  unreliable on open design, so design work is done before the loop starts.
- **The test script is the judge.** Verification never depends on the worker's summary, and the loop never runs tests
  while the worker is still changing files.
- **Failures are visible.** Every stop, retry, loop and rule break reaches the phone, and every run leaves a readable log.
