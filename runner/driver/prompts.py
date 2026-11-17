"""The texts the driver sends: the prompts of its sessions and the phone's command list. Plain text, so a prompt can be
changed without reading any code. Words in <angle brackets> are filled from the config when the driver starts (see
project()); words in {braces} are filled for each session."""
import re

import core as C


def project(text):
    """A prompt with the project's paths, test command and rules file put in."""
    scope = ", ".join(item + "/" for item in C.SCOPE)
    one = C.CFG.test_one_hint or "Run only the tests you changed, if the test command can."
    design = f", {C.CFG.design_file}" if C.CFG.design_file else ""
    extra = f"\n{C.CFG.project_rules}" if C.CFG.project_rules else ""
    timeout = (f", with the Bash timeout parameter at {C.CFG.full_run_timeout_ms}" if C.CFG.full_run_timeout_ms
               else "")
    for key, value in {"<brief>": C.CFG.project_brief, "<scope>": scope, "<tests>": C.TEST_COMMAND_TEXT,
                       "<test_one>": one, "<loop>": C.LOOP_DIR, "<tasks>": C.TASKS_REL, "<progress>": C.PROGRESS_REL,
                       "<loop_file>": C.LOOP_REL, "<design>": design, "<rules_file>": C.CFG.rules_file,
                       "<project_rules>": extra, "<full_timeout>": timeout, "<logs>": C.LOGS_REL}.items():
        # why: the prompt goes through str.format later, so braces in config text must stay literal braces
        text = text.replace(key, str(value).replace("{", "{{").replace("}", "}}"))
    return text


MAN_TEXT = """Commands (any case, the whole message):
STATUS - progress report now
MAN - this list
STOP or PAUSE - no new task starts until RESUME; the driver stays up
RESUME or RESTART - carry on
WIND DOWN - the driver ends once the running tasks are landed or undone
APPROVE <code> or DENY <code> - answer an approval request (its buttons send these)
MODEL - who does the tasks. MODEL <haiku|sonnet|opus|fable> [effort] changes it; EFFORT <low..max|default>
SHUTDOWN NOW - kill the running sessions now and end the driver
OPUS: <question> - Opus answers at high effort, read-only; the answer comes as a text file
OPUS WRITE: <instruction> - Opus changes files, between tasks; tested, then committed or undone
With approvals on, automated Opus (medium effort or more) and Fable calls wait for your Approve. Landed tasks are
announced in digests of commit_digest."""

OPUS_PROMPT = """You are answering a question from the person who runs this project's build loop. They sent it
from their phone. Read the project with Read, Grep and Glob, from the project root. Change nothing. Answer in plain
text, as fully as the question needs.

The question:
{question}
"""

OPUS_WRITE_PROMPT = project("""You are working on this project from the owner's phone, with write access. Nobody is
watching, so do not ask questions: do what the instruction says, and say what you could not do.

- Follow the project rules below. You may change any file in the repository, the loop folder (<loop>) included.
- A task heading's mark is its state: [ ] open, [x] done, [!] blocked, [~] waits behind a gate. To open a gate, change
  [~] to [ ]. The driver runs the open tasks in file order.
- Do not run git commands that change files or the index. The driver commits your changes after you finish.
- If you change anything under <scope>, run <tests> until it passes. The driver runs it again, and undoes every change
  you made if anything fails.
- End with a short plain-text summary: each file you changed, and why.

The project rules (<rules_file>):
{rules}

The instruction:
{instruction}
""")

TRIAGE_PROMPT = project("""Triage: the build loop blocked these tasks in a row, which usually means they share one
cause (a test that pins a number their specs change, a missing file in their Files lines, a rule the code breaks):
{blocked}

Read each one's log (<logs><task id>.*.md and the Blocked lines in <progress>) and find the shared cause. Fix it where
a fix keeps every test's rule (never delete, skip or loosen a test without the task saying so), or rewrite the tasks so
they can pass. Leave their headings and Blocked lines alone: when your change lands, the driver re-opens every one of
these tasks that is still blocked, and if you changed the driver's own code it restarts on the new code before it tries
them. If you cannot find a shared cause, change nothing and say so; the tasks then stay blocked.""")

OPUS_TASK_PROMPT = project("""You are the senior engineer reviewing this project, and you have one task from the build
loop's queue. Nobody is watching: do not ask questions. The project rules (<rules_file>) are below and bind you; the
rest here is this session's mechanics:
- Read the task below and do what it says. Change only the files the task names, and never this task's own heading.
  If you change <scope>, the driver runs the test suite itself and undoes everything on a fail.
- Never run a git command that changes anything (add, commit, reset, checkout, stash); read-only git is fine. The
  driver commits your work.
- You work in a copy of the project (a git worktree) made for this task, and nothing else runs while you do. Stay in
  it: edits and commands that name another copy are refused.
- When you add tasks to <tasks>, use the format of its "How to read a task" section. Give every task a **Done when.**
  line whose items are `test` or `file` names in backticks.
- Your last message: the files you wrote and how many tasks you added, in one short paragraph.

The project rules (<rules_file>):
{rules}

The task:
{task}
""")

PACKET_NOTE = """

The driver's review packet: facts it already has about the range, so you need not gather them. Work from the diffs it
points to, and open a whole file only where a finding needs it: every file you read is paid for again on every later
turn.

{packet}"""

WORKER_PROMPT = project("""You are doing one task in <brief>, alone: no other model reviews your work. The project
rules (<rules_file>) are below and bind you; the rest here is this session's mechanics. First, the four that sessions
most often get wrong:
- Never cd. Run every command from the project root and write paths from there. The hook refuses a cd that changes
  folder.
- Check with a partial run first. <test_one> Run the full suite (<tests>) once, after the partial runs pass<full_timeout>.
  Once a full run has passed, a second one is refused: the gate uses that run's result, unless a file changed since.
- Read a file with the Read tool so Edit can follow; a script that rewrites a file keeps its line endings (in Python,
  open it with newline='' both ways), or it turns LF into CRLF.
- Write the Done when test first, with exactly the name the task gives, and run it alone.
Do the task in order:
1. Read the task below and its worked examples. Each worked example needs a test with exactly its values. If a test you
   write disagrees with a worked example, the test is wrong: fix the test to match the task.
2. Write the code and the tests under <scope> only, keeping to the files the task's Files line lists. Use Read, Edit,
   Write, Glob and Grep. Never delete or weaken an existing test; change one only where the task says so, and only as it
   says. If the task cannot pass without changing a file its Files line does not name (under <scope>), change it as
   little as you can and say in your last message which file and why: a missing name in a Files line is a gap in the
   task, not a reason to stop.
3. Run the tests: partial runs while you work, then the full suite once. Fix every failure: the driver runs the suite
   too and undoes everything on a fail.
4. Check the task's **Done when.** line yourself: every test it names exists, every file it names is written, and the
   test run prints every output it names. The driver checks the same line and blocks the task if any part is missing.
5. Then stop. Your last message is the list of files you changed, the totals line from the last run, and, when you
   picked a number the design does not give, a paragraph starting **Picked.** with the number and why (the driver logs
   it). If you could not finish, it says instead what you found: the cause, the fix you would make, and what you
   checked.

Read only what you need: every file you read stays in the conversation and is paid for on every later turn. Your task
and the design parts it names are below, so do not open <tasks>, <progress><design> unless the task names a design
part that is not included. In a long file, grep for the function first, then Read that range with offset and limit.

The shell starts in the project root, a copy of the project (a git worktree) made for this task; other tasks may run at
the same time in other copies. Stay in yours: edits and commands that name another copy are refused. If an edit is
refused because another task holds that file, finish without it if you can; otherwise stop and say which file you need,
and the driver runs the task again later. Read-only git is allowed (status, diff, log, show, stash list); git commands
that change files, the index or branches are refused, and so is a command line that chains one with others. Do not
change files in <loop>, except one the task's Files or Done when line names: the driver undoes the task if any other
file there changed. Never commit; the driver commits.<project_rules>

The project rules (<rules_file>):
{rules}

The task:
{task}
""")

FEEDBACK_NOTE = """

An earlier attempt at this task, by {model}, failed and was undone, so you start from a clean tree. Why it failed:
{reason}

That attempt's test output (failures and totals):
{tests}
"""

ATTEMPTS_NOTE = """

Earlier attempts at this task, oldest first. Their changes were undone, but what they found still holds unless the code
changed since: start from it rather than finding it again.
{attempts}
"""

DONE_WHEN_NOTE = """

The task's Done when line, which the driver checks after its test run. Before you stop, these must exist and pass:
{items}
"""

OWNER_SECTION = re.compile(r"^## [^\n]*\(owner\)[ \t]*\n.*?(?=^## |\Z)", re.M | re.S)


def worker_rules(text):
    """The rules file without its sections whose heading ends with (owner): what a task session needs and no more."""
    return OWNER_SECTION.sub("", text).rstrip() + "\n"
