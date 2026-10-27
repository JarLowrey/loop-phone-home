"""The driver's settings and limits. The ones in the driver-settings block of the loop file are read again at each pass,
so an edit there takes effect without a restart; the rest are constants. Read them as S.NAME, never copy them into
another module, since refresh() changes them in place."""
import os
import re

import loop_common as C
import runtime as R

LOOP_MD = C.LOOP_FILE
# the fallbacks for the driver-settings block of the loop file
SETTING_DEFAULTS = {
    "scope": ", ".join(C.SCOPE),
    "poll_seconds": "60",
    "inbox_window": "1h",
    "worker_turns": str(C.CFG.task_turns),
    "worker_timeout": str(C.CFG.task_timeout),
    "escalate_model": "opus",
    "daily_budget_usd": "120",
    "approval_hours": "12",
    "approvals": "on",
    "monitor_every": "5",
    "parallel_slots": "1",
    "parallel_window": "4",
    "shared_files": "",
    "max_requeues": "2",
    "commit_digest": "4",
}
SETTING_ALIASES = {"haiku_turns": "worker_turns", "haiku_timeout": "worker_timeout", "bot_every": "monitor_every"}
SCOPE = []
POLL_SECONDS = 60
INBOX_WINDOW = "1h"
WORKER_TURNS = 40
WORKER_TIMEOUT = 1800
ESCALATE_MODEL = "opus"
DAILY_BUDGET_USD = 120.0
APPROVAL_HOURS = 12.0
# why: "approvals: off" in the loop file lets every automated call run without asking the phone; budgets still apply
APPROVALS_ON = True
# the clean-checkout check and monitor_command run after this many landed tasks (monitor.py); 0 turns them off
MONITOR_EVERY = 5
# slots: how many tasks run side by side, each in its own worktree (1 runs them one at a time, still in a worktree)
PARALLEL_SLOTS = 1
# how far the scheduler looks ahead: a task starts only within this many open tasks of the oldest running one
PARALLEL_WINDOW = 4
# files two running tasks may both change; the rebase merges their added lines (slots.additive_merge)
SHARED_FILES = []
# a task that could not land this many times (a conflict, a duplicate, a failed re-test) next runs alone
MAX_REQUEUES = 2
# landed tasks per "Tasks landed" message on the phone (1 sends one per commit)
COMMIT_DIGEST = 4
LAST_SETTINGS = {}

# the models that may do the tasks, by the name the MODEL command takes, with the name their commits carry
WORKER_MODELS = {"haiku": "Claude Haiku 5.5", "sonnet": "Claude Sonnet 5.5", "opus": "Claude Opus 5.5",
                 "fable": "Claude Fable 5.1"}
MODEL_RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 2}
# the most one task session may spend, by model. A session that reaches it ends, and the task counts as failed
WORKER_BUDGET_USD = {"haiku": 3.0, "sonnet": 8.0, "opus": 30.0, "fable": 20.0}
WORKER_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_WORKER = C.CFG.task_model

OPUS_MODEL = os.environ.get("OPUS_MODEL", "opus")
OPUS_EFFORT = "high"
# a phone question: read-only tools, time and spend
OPUS_TOOLS = "Read,Grep,Glob"
OPUS_TIMEOUT = 1800
OPUS_BUDGET_USD = 5.0
OPUS_MAX_BYTES = 3_900_000   # the phone must take the file, and the limit is 4 MB, so stay well under it
# OPUS WRITE from the phone, and the triage
OPUS_WRITE_TURNS = 80
OPUS_WRITE_TIMEOUT = 3600
OPUS_WRITE_BUDGET_USD = 10.0
# a task whose Who line says Opus
OPUS_TASK_TURNS = 200
OPUS_TASK_TIMEOUT = 7200
OPUS_TASK_BUDGET_USD = 30.0

USAGE_RESUME_MARGIN = 240  # why: the driver restarts 4 minutes after usage reopens, so the first call is not refused again
APPROVAL_POLL_SECONDS = 15
# a session that fails within this many seconds having taken no turn never started: an account, network or install
# fault, not the task's. The driver pauses and tries again after NOT_STARTED_RETRY_SECONDS instead of blocking the task
NOT_STARTED_SECONDS = 30
NOT_STARTED_RETRY_SECONDS = 1800
MAX_TASK_ERRORS = 3
# the share of a day's finished tasks that were requeued, past which the phone is told (once a day, from REQUEUE_ALERT_MIN
# tasks on), since requeues cost money and land nothing: fewer slots or a smaller window is then the fix
REQUEUE_ALERT_SHARE = 0.2
REQUEUE_ALERT_MIN = 5
# blocks in a row that start an Opus triage, and the blocks in a row after a triage that pause the driver
TRIAGE_AFTER_BLOCKS = 2
PAUSE_AFTER_TRIAGE_BLOCKS = 2


def read_settings(text):
    """The driver-settings block of the loop file as a dict over SETTING_DEFAULTS, with old names mapped to new ones. Unknown
    keys are logged and ignored."""
    values = dict(SETTING_DEFAULTS)
    match = re.search(r"```driver-settings\n(.*?)```", text, re.S)
    if not match:
        return values
    for line in match.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            key, value = (part.strip() for part in line.split(":", 1))
            key = SETTING_ALIASES.get(key, key)
            if key in values:
                values[key] = value
            else:
                R.log(f"{LOOP_MD.name}: unknown driver setting {key!r} is ignored")
    return values


def apply_settings(values):
    """Sets the driver's values from a settings dict. Raises ValueError when one cannot be read."""
    global SCOPE, POLL_SECONDS, INBOX_WINDOW, WORKER_TURNS, WORKER_TIMEOUT, ESCALATE_MODEL, DAILY_BUDGET_USD
    global APPROVAL_HOURS, APPROVALS_ON, MONITOR_EVERY, PARALLEL_SLOTS, PARALLEL_WINDOW, SHARED_FILES, MAX_REQUEUES
    global COMMIT_DIGEST
    escalate = values["escalate_model"].lower()
    if values["approvals"].lower() not in ("on", "off"):
        raise ValueError(f"approvals {values['approvals']!r} is not on or off")
    if escalate not in WORKER_MODELS and escalate not in ("", "none"):
        raise ValueError(f"escalate_model {escalate!r} is not one of {', '.join(WORKER_MODELS)} or none")
    scope = [item.strip() for item in values["scope"].split(",") if item.strip()]
    numbers = (int(values["poll_seconds"]), int(values["worker_turns"]), int(values["worker_timeout"]),
               float(values["daily_budget_usd"]), float(values["approval_hours"]), int(values["monitor_every"]))
    parallel = (int(values["parallel_slots"]), int(values["parallel_window"]), int(values["max_requeues"]))
    if parallel[0] < 1 or parallel[1] < 1 or parallel[2] < 0:
        raise ValueError(f"parallel_slots and parallel_window must be 1 or more, max_requeues 0 or more: {parallel}")
    SCOPE = scope
    PARALLEL_SLOTS, PARALLEL_WINDOW, MAX_REQUEUES = parallel
    COMMIT_DIGEST = max(1, int(values["commit_digest"]))
    SHARED_FILES = [item.strip() for item in values["shared_files"].split(",") if item.strip()]
    POLL_SECONDS, WORKER_TURNS, WORKER_TIMEOUT, DAILY_BUDGET_USD, APPROVAL_HOURS, MONITOR_EVERY = numbers
    INBOX_WINDOW = values["inbox_window"]
    ESCALATE_MODEL = "" if escalate == "none" else escalate
    APPROVALS_ON = values["approvals"].lower() == "on"


def refresh():
    """Reads the driver-settings block in the loop file and sets the driver's values from it. Called at each pass of the
    loop, so an edit there takes effect without a restart. A value it cannot read puts every setting back to its
    default, and each change is logged."""
    try:
        values = read_settings(LOOP_MD.read_text(encoding="utf-8"))
    except OSError as error:
        R.log(f"could not read {LOOP_MD.name}: {error}; using the defaults")
        values = dict(SETTING_DEFAULTS)
    try:
        apply_settings(values)
    except ValueError as error:
        R.log(f"{LOOP_MD.name}: a driver setting is not valid ({error}); using the defaults")
        values = dict(SETTING_DEFAULTS)
        apply_settings(values)
    if values != LAST_SETTINGS:
        if LAST_SETTINGS:
            changed = {k: v for k, v in values.items() if LAST_SETTINGS.get(k) != v}
            R.log(f"driver settings changed from {LOOP_MD.name}: {changed}")
            if R.EMIT:
                R.EMIT("settings", changed=changed)
        else:
            R.log(f"driver settings from {LOOP_MD.name}: {values}")
        LAST_SETTINGS.clear()
        LAST_SETTINGS.update(values)
