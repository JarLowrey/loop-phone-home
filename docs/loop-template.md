# The loop template (`loop-template/LOOP.md`)

The template is the prompt for an orchestrating Claude loop that works through a task queue. The loop plans and verifies;
the worker writes the code. The template uses only the config's names, so it works with any `worker.json`.

The driver (`runner/driver/`) runs the same steps in code. You need an orchestrating session only for the `ANSWER`, `APPROVED`, `REJECTED`
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
