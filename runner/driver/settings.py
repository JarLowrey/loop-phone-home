"""The driver's settings and limits. The driver-settings block of the loop file is read again at each pass (refresh), so
an edit there takes effect without a restart; read it as R.RUNTIME["settings"]. The rest are constants."""
import os
import re

import runtime as R

SETTING_DEFAULTS = {
    "poll_seconds": "60",
    "max_attempts": "3",
    "retry_states": "tests_failed, failed, looped, timed_out, tests_did_not_run",
    "escalate_model": "opus",
    "daily_budget_usd": "120",
    "approval_hours": "12",
    "approvals": "on",
}
# the models that may do the tasks, by the name the MODEL command takes, with the name their commits carry
MODEL_NAMES = {"haiku": "Claude Haiku 5.5", "sonnet": "Claude Sonnet 5.5", "opus": "Claude Opus 5.5",
               "fable": "Claude Fable 5.1"}
MODEL_RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 2}
# the most one task session may spend, by model. A session that reaches it ends, and the task counts as failed
MODEL_BUDGET_USD = {"haiku": 3.0, "sonnet": 8.0, "opus": 30.0, "fable": 20.0}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
TASK_TOOLS = "Bash,Read,Edit,Write,Glob,Grep"

OPUS_MODEL = os.environ.get("OPUS_MODEL", "opus")
OPUS_EFFORT = "high"
# a task whose Who line says Opus
OPUS_TASK_TURNS = 200
OPUS_TASK_TIMEOUT = 7200
OPUS_TASK_BUDGET_USD = 30.0
# the triage after a run of blocked tasks
TRIAGE_TURNS = 80
TRIAGE_TIMEOUT = 3600
TRIAGE_BUDGET_USD = 10.0
# blocks in a row that start a triage, and the blocks in a row after a triage that pause the driver
TRIAGE_AFTER_BLOCKS = 2
PAUSE_AFTER_TRIAGE_BLOCKS = 2

USAGE_RESUME_MARGIN = 240  # why: restart 4 minutes after usage reopens, so the first call is not refused again
USAGE_LIMIT = re.compile(r"session limit|usage limit|subscription access", re.I)
APPROVAL_POLL_SECONDS = 15
MAX_TASK_ERRORS = 3


def parse_settings(values):
    """The driver settings as the types they are used as. Raises ValueError for a value that cannot be read."""
    escalate = values["escalate_model"].strip().lower()
    if escalate not in MODEL_NAMES and escalate not in ("", "none"):
        raise ValueError(f"escalate_model {escalate!r} is not one of {', '.join(MODEL_NAMES)} or none")
    approvals = values["approvals"].strip().lower()
    if approvals not in ("on", "off"):
        raise ValueError(f"approvals {approvals!r} is not on or off")
    return {"poll_seconds": max(5, int(values["poll_seconds"])), "max_attempts": int(values["max_attempts"]),
            "retry_states": tuple(part.strip() for part in values["retry_states"].split(",") if part.strip()),
            "escalate_model": "" if escalate == "none" else escalate,
            "daily_budget_usd": float(values["daily_budget_usd"]), "approval_hours": float(values["approval_hours"]),
            "approvals": approvals == "on"}


R.RUNTIME["settings"] = parse_settings(SETTING_DEFAULTS)


def get(name):
    return R.RUNTIME["settings"][name]


def refresh():
    """Reads the driver-settings block in the loop file. A value that cannot be read puts every setting back to its
    default, and a change to the settings is logged."""
    values = dict(SETTING_DEFAULTS)
    try:
        text = R.PATHS["loop"].read_text(encoding="utf-8")
    except OSError as error:
        R.log(f"could not read {R.PATHS['loop'].name}: {error}; using the defaults")
        text = ""
    match = re.search(r"```driver-settings\n(.*?)```", text, re.S)
    for line in (match.group(1).splitlines() if match else []):
        key, separator, value = line.partition(":")
        if not separator or key.strip().startswith("#"):
            continue
        if key.strip() in values:
            values[key.strip()] = value.strip()
        else:
            R.log(f"unknown driver setting {key.strip()!r} in {R.PATHS['loop'].name} is ignored")
    try:
        settings = parse_settings(values)
    except ValueError as error:
        R.log(f"a driver setting in {R.PATHS['loop'].name} is not valid ({error}); using the defaults")
        settings = parse_settings(SETTING_DEFAULTS)
    if settings != R.RUNTIME["last"]:
        R.log(f"driver settings: {settings}")
        R.RUNTIME["last"] = settings
    R.RUNTIME["settings"] = settings
